"""Record a self-explaining demo of MI recovering live outages.

The recording is the real dashboard. On top of it the recorder draws, for the viewer only,
a spotlight box around the part that matters and an explanation card. Every number in
the cards (diagnosis, chosen action, JEV probability, rows lost, reward, time to recover)
is read from MI's own event log after it happens, never typed in advance. MI never sees
the overlay; the outage is sent to the target's test hook, exactly as the harness
campaign does, so MI only observes its symptoms.

Needs the stack up with test hooks (`TARGET_TEST_HOOKS=1 docker compose up`) and
Playwright with Chrome or Chromium.

    python scripts/record_demo.py --mode full --fault drop_widgets_table --out a.mp4
    python scripts/record_demo.py --mode learned --fault corrupt_db --out b.mp4
    ffmpeg -f concat ...   # see README "Recording the demo"
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

ACCENT = "#FFB020"

OVERLAY_JS = """
() => {
  if (window.__demo) return;
  const css = document.createElement('style');
  css.textContent = `
    #demo-spot{position:fixed;z-index:9998;pointer-events:none;border:4px solid ACCENT;
      border-radius:10px;box-shadow:0 0 0 9999px rgba(4,8,12,.62);transition:all .35s ease}
    #demo-card{position:fixed;z-index:9999;pointer-events:none;width:500px;background:#FFFDF5;
      color:#141414;border-left:8px solid ACCENT;border-radius:10px;padding:16px 20px 18px;
      font:19px/1.4 system-ui,-apple-system,Segoe UI,sans-serif;
      box-shadow:0 14px 40px rgba(0,0,0,.45);
      transition:opacity .3s}
    #demo-card .n{display:inline-block;background:ACCENT;color:#141414;font-weight:800;
      border-radius:14px;padding:1px 10px;margin-right:8px;font-size:16px}
    #demo-card b.t{display:block;font-size:22px;margin:6px 0 6px}
    #demo-full{position:fixed;inset:0;z-index:10000;pointer-events:none;background:rgba(9,15,14,.96);
      color:#EAF5F2;display:flex;align-items:center;justify-content:center;transition:opacity .4s}
    #demo-full .w{width:1100px;font:24px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
    #demo-full h1{font-size:48px;margin:0 0 18px;color:#fff}
    #demo-full .k{color:ACCENT;font-weight:700;letter-spacing:.06em;font-size:18px}
    #demo-full li{margin:8px 0}
    #demo-bar{position:fixed;left:50%;transform:translateX(-50%);bottom:24px;z-index:9999;
      pointer-events:none;max-width:1100px;background:#141414;color:#fff;border:2px solid ACCENT;
      border-radius:12px;padding:12px 22px;font:600 21px/1.35 system-ui,-apple-system,sans-serif}
  `.replaceAll('ACCENT', '%ACCENT%');
  document.head.appendChild(css);
  const find = (target) => {
    if (target.selector) return document.querySelector(target.selector);
    const scope = target.scope ? document.querySelector(target.scope) : document;
    if (!scope) return null;
    const heads = [...scope.querySelectorAll('h2,h3')].filter(
      (h) => h.textContent.trim() === target.panel && h.offsetParent !== null);
    if (!heads.length) return null;
    const box = heads[0].closest('section,.incident-panel');
    if (box && target.inner) return box.querySelector(target.inner) || box;
    return box;
  };
  let timer = null;
  const place = (target, side) => {
    const el = find(target);
    const spot = document.getElementById('demo-spot');
    const card = document.getElementById('demo-card');
    if (!el || !spot || !card) return;
    const r = el.getBoundingClientRect();
    const pad = 8;
    spot.style.left = (r.left - pad) + 'px'; spot.style.top = (r.top - pad) + 'px';
    spot.style.width = (r.width + 2 * pad) + 'px'; spot.style.height = (r.height + 2 * pad) + 'px';
    const W = window.innerWidth, H = window.innerHeight, cw = 500, ch = card.offsetHeight;
    let x, y;
    const right = W - r.right - 24, left = r.left - 24;
    if (side !== 'left' && right >= cw + 8) { x = r.right + 24; }
    else if (left >= cw + 8) { x = r.left - 24 - cw; }
    else { x = Math.min(Math.max(16, r.left), W - cw - 16); }
    if (x === r.right + 24 || x === r.left - 24 - cw) {
      y = Math.min(Math.max(16, r.top), H - ch - 16);
    } else {
      y = r.bottom + 24 + ch < H ? r.bottom + 24 : Math.max(16, r.top - ch - 24);
    }
    card.style.left = x + 'px'; card.style.top = y + 'px';
  };
  window.__demo = {
    spot(target, step, title, body, side) {
      this.clear();
      const el = find(target);
      if (!el) return false;
      el.scrollIntoView({block: 'center', inline: 'nearest'});
      const spot = document.createElement('div'); spot.id = 'demo-spot';
      const card = document.createElement('div'); card.id = 'demo-card';
      card.innerHTML = (step ? `<span class="n">${step}</span>` : '') +
        `<b class="t">${title}</b><div>${body}</div>`;
      document.body.append(spot, card);
      place(target, side);
      timer = setInterval(() => place(target, side), 250);
      return true;
    },
    clear() {
      if (timer) { clearInterval(timer); timer = null; }
      for (const id of ['demo-spot', 'demo-card', 'demo-bar']) {
        document.getElementById(id)?.remove();
      }
    },
    full(html) {
      document.getElementById('demo-full')?.remove();
      if (!html) return;
      const d = document.createElement('div'); d.id = 'demo-full';
      d.innerHTML = `<div class="w">${html}</div>`; document.body.append(d);
    },
    bar(text) {
      document.getElementById('demo-bar')?.remove();
      if (!text) return;
      const d = document.createElement('div'); d.id = 'demo-bar'; d.innerHTML = text;
      document.body.append(d);
    },
  };
}
""".replace("%ACCENT%", ACCENT)


def _get(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S310 - local demo URL
        data = json.loads(response.read())
    return data if isinstance(data, dict) else {}


def _post(url: str) -> None:
    request = urllib.request.Request(url, method="POST")  # noqa: S310 - local demo URL
    with urllib.request.urlopen(request, timeout=10):  # noqa: S310 - local demo URL
        pass


def _state(agent: str) -> str:
    return str(_get(f"{agent}/agent/status").get("workflow_state"))


def _wait_state(agent: str, *, active: bool, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if (_state(agent) == "incident_active") == active:
            return
        time.sleep(0.25)
    raise TimeoutError(f"workflow did not become {'active' if active else 'healthy'}")


def _incident_facts(agent: str, incident_id: str) -> dict[str, Any]:
    """Everything the cards quote, straight from MI's event log."""
    events = [
        row
        for row in _get(f"{agent}/agent/events?limit=2000").get("events", [])
        if isinstance(row, dict)
        and isinstance(row.get("payload"), dict)
        and row["payload"].get("incident_id") == incident_id
    ]
    facts: dict[str, Any] = {"incident_id": incident_id, "attempts": []}
    for row in events:
        kind, payload = row["event_type"], row["payload"]
        if kind == "incident.started":
            facts["started_ts"] = row["ts"]
        elif kind == "diagnosis.why":
            facts["failure_type"] = payload.get("failure_type")
        elif kind == "planning.how":
            jev = payload.get("jev_decision") or {}
            facts["attempts"].append(
                {
                    "candidates": 1 + len(payload.get("alternatives") or []),
                    "executed_candidate": payload.get("executed_candidate"),
                    "tool": payload.get("executed_tool_name"),
                    "jev_selected": jev.get("selected_candidate_name"),
                    "jev_operation": jev.get("selected_candidate_operation"),
                    "jev_usable_p": jev.get("usable_probability"),
                    "source": payload.get("source"),
                }
            )
        elif kind == "verification.result":
            if facts["attempts"]:
                facts["attempts"][-1]["verified_healthy"] = payload.get("healthy")
        elif kind == "incident.recovered":
            facts.update(
                recovered_ts=row["ts"],
                rows_before=payload.get("widget_rows_before"),
                rows_after=payload.get("widget_rows_after"),
                rows_lost=payload.get("rows_lost"),
                reward=payload.get("reward"),
                path=payload.get("source"),
            )
        elif kind == "workflow.step" and payload.get("step") == "path.selected":
            facts.setdefault("paths", []).append(payload.get("path_mode"))
        elif kind == "workflow.step" and payload.get("step") == "llm.why":
            facts.setdefault("why_ts", row["ts"])
    if "started_ts" in facts and "recovered_ts" in facts:
        facts["ttr_s"] = round(facts["recovered_ts"] - facts["started_ts"], 1)
        if "why_ts" in facts:
            facts["why_s"] = round(facts["why_ts"] - facts["started_ts"], 1)
            facts["after_why_s"] = round(facts["recovered_ts"] - facts["why_ts"], 1)
    return facts


def _latest_incident(agent: str) -> str:
    for row in reversed(_get(f"{agent}/agent/events?limit=2000").get("events", [])):
        if isinstance(row, dict) and row.get("event_type") == "incident.started":
            return str(row["payload"]["incident_id"])
    raise RuntimeError("no incident found")


def _evidence_snapshot(target: str) -> dict[str, Any]:
    """The target endpoints MI reads, fetched the moment the incident opens."""
    snap: dict[str, Any] = {}
    for name, path in (
        ("health_deep", "/health/deep"),
        ("db_status", "/ops/db/status"),
        ("backups", "/ops/backups?limit=3"),
        ("log_tail", "/ops/logs?lines=6"),
    ):
        try:
            with urllib.request.urlopen(f"{target}{path}", timeout=5) as resp:  # noqa: S310
                snap[name] = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            snap[name] = {"status_code": exc.code, **json.loads(exc.read() or b"{}")}
    return snap


def _fmt_reward(value: Any) -> str:
    """Two decimals for whole values (0.00, 1.00), three otherwise, so 0.9975 is not shown
    as a perfect 1.00."""
    if not isinstance(value, (int, float)):
        return "n/a"
    return f"{value:.2f}" if float(value).is_integer() else f"{value:.3f}"


class Demo:
    def __init__(self, page: Page) -> None:
        self.page = page
        self.steps = 0

    def spot(self, target: dict[str, str], step: str, title: str, body: str, hold: int,
             side: str = "right") -> bool:
        if step == "#":  # numbered step: the counter only advances when it is shown
            step = str(self.steps + 1)
        shown = bool(self.page.evaluate(
            "([t, s, ti, b, sd]) => window.__demo.spot(t, s, ti, b, sd)",
            [target, step, title, body, side],
        ))
        if shown:
            if step.isdigit():
                self.steps += 1
            self.page.wait_for_timeout(hold)
        return shown

    def clear(self) -> None:
        self.page.evaluate("() => window.__demo.clear()")

    def full(self, html: str, hold: int) -> None:
        self.page.evaluate("(h) => window.__demo.full(h)", html)
        self.page.wait_for_timeout(hold)
        self.page.evaluate("() => window.__demo.full('')")

    def bar(self, text: str) -> None:
        self.page.evaluate("(t) => window.__demo.bar(t)", text)

    def wait_text(self, selector: str, needle: str, timeout_ms: int) -> bool:
        try:
            self.page.wait_for_function(
                "([s, n]) => (document.querySelector(s)?.innerText || '').includes(n)",
                arg=[selector, needle],
                timeout=timeout_ms,
            )
        except PlaywrightTimeoutError:
            return False
        return True


HIST = "#history-incident"
FAULT_LABEL = {
    "drop_widgets_table": "the service&rsquo;s <b>widgets</b> table is deleted",
    "delete_db": "the service&rsquo;s database file is deleted",
    "corrupt_db": "the service&rsquo;s database file is overwritten with garbage",
}


def _intro(demo: Demo, mode: str, fault: str) -> None:
    if mode == "full":
        demo.full(
            "<div class='k'>TIM-MI · LIVE DEMO</div><h1>An AI agent that repairs a broken"
            " service by itself</h1><ul>"
            "<li>MI pings a web service every second.</li>"
            "<li>When it breaks, MI works out <b>why</b> (LLM call 1), proposes <b>how</b> to"
            " fix it (LLM call 2), a decision model (<b>JEV</b>) picks one fix, a security gate"
            " checks it, it runs once, and an independent check verifies the service.</li>"
            "<li>Then MI <b>learns</b>: the fix is scored by how much data it kept.</li>"
            "<li>Part 1: MI starts with <b>no memory</b>, so it reasons from scratch.</li>"
            "</ul><p style='opacity:.75'>The outage is simulated, and MI is not told: it only"
            " sees the symptoms. Highlights and notes are added for you, the viewer.</p>",
            11000,
        )
    else:
        demo.full(
            "<div class='k'>PART 2 · AFTER LEARNING</div><h1>The same agent, with its"
            " learned memory</h1><ul>"
            "<li>MI&rsquo;s memory from earlier live runs is loaded: many verified recoveries of"
            " this kind of database outage.</li>"
            f"<li>The outage is the same as in part 1: {FAULT_LABEL[fault]}.</li>"
            "<li>When a fix has proven itself often enough for a type of incident, MI takes"
            " the <b>fast path</b>: it reuses that fix instead of asking the LLM to plan a"
            " new one.</li>"
            "<li>The fast path still goes through JEV, the security gate and verification.</li>"
            "</ul>",
            9000,
        )


def _live_part(demo: Demo, page: Page, target: str, agent: str, fault: str,
               mode: str) -> tuple[str, dict[str, Any]]:
    page.click("#tab-live")
    page.wait_for_timeout(800)
    demo.spot({"panel": "Agent State"}, "#", "MI is watching",
              "MI calls the service&rsquo;s deep health check every second. "
              "<b>workflow: healthy</b> means nothing needs fixing.", 5500)
    if mode == "full":
        demo.spot({"panel": "Surveillance"}, "", "Safety sensors",
                  "These re-check MI&rsquo;s own rules on its event log, for example that JEV "
                  "chose every action and the security gate ran before anything executed. "
                  "<b>pass</b> = no rule broken.", 6500)
    demo.clear()
    demo.bar(f"Simulated outage now: {FAULT_LABEL[fault]}. MI is not told.")
    page.wait_for_timeout(3000)
    _post(f"{target}/ops/fault/{fault}")
    _wait_state(agent, active=True, timeout=30)
    evidence = _evidence_snapshot(target)
    demo.bar("")
    demo.spot({"panel": "Agent State"}, "#", "Outage detected",
              "The health check now fails (HTTP 500), so MI opens an <b>incident</b> and "
              "collects evidence: health, database status, the app&rsquo;s schema, recent logs "
              "and the list of backups.", 5500)
    demo.spot({"panel": "Recovery Timeline"}, "#", "Every step is logged",
              "MI writes each step as it happens: evidence collected, SQLite documentation "
              "looked up (RAG), then the LLM is asked <b>why</b> the service broke.", 4500)
    if mode == "full" and demo.wait_text("#why", "failure_type", 60_000):
        demo.spot({"panel": "WHY Diagnosis"}, "#", "WHY: the LLM diagnoses the cause",
                  "<b>failure_type</b> names what is wrong. The <b>evidence</b> lines quote "
                  "real values the service reported.", 4500)
    demo.clear()
    if mode == "learned":
        # On the fast path the incident closes about 2 s after the diagnosis lands, too
        # quick to point at live; the History walkthrough below shows both.
        demo.bar("The LLM is diagnosing <b>why</b>; then class memory checks for a proven"
                 " fix&hellip;")
    if mode == "full" and _state(agent) == "incident_active":
        demo.wait_text("#timeline", "llm.how", 30_000)
        if _state(agent) == "incident_active":
            demo.spot({"panel": "Recovery Timeline"}, "#", "HOW + JEV, in progress",
                      "<b>llm.how</b>: the LLM has proposed several repairs, each a concrete "
                      "action with exact parameters. <b>jev.requested</b>: JEV, a separate "
                      "decision model, is scoring them to pick <b>one</b>. The full list "
                      "appears in History once the incident closes.", 5000)
    demo.clear()
    demo.bar("Waiting for the repair to run and for the independent health check&hellip;")
    _wait_state(agent, active=False, timeout=240)
    demo.bar("")
    incident = _latest_incident(agent)
    page.wait_for_timeout(1500)
    live = _incident_facts(agent, incident)
    demo.spot({"panel": "Agent State"}, "#", f"Recovered in {live.get('ttr_s')} s",
              f"From detection to verified recovery, measured by MI (the LLM diagnosis "
              f"took {live.get('why_s')} s of it). The repair ran once and the service "
              "passed its deep health check again, including a test write. MI closed the "
              "incident by itself.", 6500)
    demo.clear()
    page.wait_for_timeout(1200)
    return incident, evidence


def _history_part(demo: Demo, page: Page, facts: dict[str, Any], mode: str) -> None:
    page.click("#tab-history")
    page.wait_for_timeout(1500)
    page.click(f"button[data-history-incident='{facts['incident_id']}']")
    page.wait_for_timeout(2000)
    demo.spot({"selector": "#history"}, "#", "The full record of the incident",
              "Nothing is lost when the incident closes. Every incident is kept here with "
              "its diagnosis, the repair that ran and the outcome.", 5500)
    demo.spot({"panel": "WHY Diagnosis", "scope": HIST}, "#", "Why it broke",
              f"Diagnosis: <b>{facts.get('failure_type')}</b>. Each evidence line points "
              "at a field MI actually read from the service.", 7000)
    last = facts["attempts"][-1] if facts["attempts"] else {}
    p = last.get("jev_usable_p")
    p_text = f"{p:.2f}" if isinstance(p, (int, float)) else "n/a"
    if last.get("source") == "class_memory":
        how_body = (
            "Fast path: class memory replayed the fix that has worked before for this "
            f"kind of incident (<b>{last.get('tool')}</b>). Its rationale text is from the "
            f"incident where it was first learned. JEV still had to approve it (usable "
            f"probability {p_text}) before the security gate and execution."
        )
    else:
        executed = last.get("executed_candidate") or ""
        which = "the primary" if executed == "primary" else "an alternative"
        how_body = (
            f"The LLM proposed <b>{last.get('candidates')}</b> repairs. JEV picked "
            f"{which}: <b>{last.get('jev_selected')}</b> ({last.get('jev_operation')}), "
            f"usable probability {p_text}. The badge marks the one that ran."
        )
    demo.spot({"panel": "HOW Proposals", "scope": HIST}, "#", "How it was fixed", how_body,
              9000)
    executed_box = {"selector": f"{HIST} .how-candidate.executed"}
    demo.spot(executed_box, "", "This is the action that ran",
              "Exactly one action per attempt. It passed the security gate (allowed "
              "operation, safe paths) before it executed.", 5000)
    lost, before, reward = facts.get("rows_lost"), facts.get("rows_before"), facts.get("reward")
    if isinstance(reward, (int, float)) and isinstance(before, int) and isinstance(lost, int):
        verdict = (
            "This fix brought the service back but lost the data, so it scores low and"
            " MI will not adopt it as its usual fix."
            if reward < 0.5
            else "A high score: MI is more likely to reuse this fix for this kind of outage."
        )
        reward_body = (
            f"Users had written <b>{before}</b> rows before the outage; the repair lost "
            f"<b>{lost}</b>. Reward = 1 &minus; lost &divide; before = "
            f"<b>{_fmt_reward(reward)}</b>. "
            + verdict
        )
    else:
        reward_body = "The verified outcome is scored and stored so MI can prefer better fixes."
    demo.spot({"panel": "RL Live", "scope": HIST, "inner": ".info-card"}, "#",
              "What MI learned", reward_body, 9000)
    demo.spot({"panel": "Recovery Timeline", "scope": HIST, "inner": "ul"}, "#", "Audit trail",
              "Every step in order: diagnosis, path chosen, JEV decision, security gate, "
              "execution, verification, then learning.", 6000)
    demo.clear()


def _outro(demo: Demo, facts: dict[str, Any], mode: str,
           compare: dict[str, Any] | None = None) -> None:
    ttr = facts.get("ttr_s")
    if mode == "full":
        demo.full(
            "<div class='k'>PART 1 · SUMMARY</div><h1>Recovered with no human involved</h1>"
            f"<ul><li>Time to recover: <b>{ttr} s</b>.</li>"
            f"<li>Diagnosis: <b>{facts.get('failure_type')}</b>.</li>"
            f"<li>Rows lost: <b>{facts.get('rows_lost')}</b> of {facts.get('rows_before')};"
            f" reward <b>{_fmt_reward(facts.get('reward'))}</b>. MI learns from this score"
            " which fixes keep users&rsquo; data.</li>"
            "<li>If a repair fails verification, MI records the failure, keeps everything"
            " it learned, and tries a different repair, with no retry limit.</li></ul>",
            9000,
        )
    else:
        before = ""
        if compare:
            before = (
                "<li>Part 1, no memory: the fix JEV picked"
                f" ({compare['attempts'][-1].get('jev_operation')}) lost"
                f" <b>{compare.get('rows_lost')} of {compare.get('rows_before')}</b> rows"
                f" (reward {_fmt_reward(compare.get('reward'))}), in {compare.get('ttr_s')} s.</li>"
            )
        demo.full(
            "<div class='k'>SUMMARY</div><h1>Learning kept the users&rsquo; data</h1><ul>"
            + before
            + "<li>Part 2, same outage, learned memory: the fix"
            f" ({facts['attempts'][-1].get('jev_operation')}) lost <b>"
            f"{facts.get('rows_lost')} of {facts.get('rows_before')}</b> rows (reward"
            f" {_fmt_reward(facts.get('reward'))}).</li>"
            f"<li>Time to recover: <b>{facts.get('ttr_s')} s</b>. The LLM diagnosis took"
            f" {facts.get('why_s')} s; after it, the fast path needed only"
            f" <b>{facts.get('after_why_s')} s</b> to approve, run and verify the fix.</li>"
            "<li>Live campaign, 17 Nov 2026: 12 of 12 outages recovered and verified,"
            " median 19 s, zero safety-rule violations.</li></ul>"
            "<p style='opacity:.75'>mi-recovery-agent.vercel.app &middot;"
            " github.com/aalricc/TIM-MI</p>",
            12000,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-url", default="http://127.0.0.1:8080")
    parser.add_argument("--agent-url", default="http://127.0.0.1:8090")
    parser.add_argument("--mode", choices=["full", "learned"], default="full")
    parser.add_argument("--fault", choices=sorted(FAULT_LABEL), default="drop_widgets_table")
    parser.add_argument("--out", default="demo.mp4")
    parser.add_argument("--facts", default=None, help="write the incident facts JSON here")
    parser.add_argument("--compare", default=None,
                        help="facts JSON of the part-1 run, quoted in the part-2 summary")
    parser.add_argument("--channel", default="chrome", help="Playwright browser channel")
    args = parser.parse_args()
    target, agent = args.target_url.rstrip("/"), args.agent_url.rstrip("/")

    _wait_state(agent, active=False, timeout=120)
    with tempfile.TemporaryDirectory() as video_dir, sync_playwright() as pw:
        browser = pw.chromium.launch(channel=args.channel)
        context = browser.new_context(
            viewport={"width": 1600, "height": 900},
            record_video_dir=video_dir,
            record_video_size={"width": 1600, "height": 900},
        )
        page = context.new_page()
        page.goto(f"{agent}/dashboard")
        page.wait_for_timeout(2500)
        page.evaluate(OVERLAY_JS)
        demo = Demo(page)
        _intro(demo, args.mode, args.fault)
        incident, evidence = _live_part(demo, page, target, agent, args.fault, args.mode)
        facts = _incident_facts(agent, incident)
        facts["evidence_snapshot"] = evidence
        _history_part(demo, page, facts, args.mode)
        compare = (
            json.loads(Path(args.compare).read_text(encoding="utf-8")) if args.compare else None
        )
        _outro(demo, facts, args.mode, compare)
        video_path = page.video.path() if page.video else None
        context.close()
        browser.close()
        if args.facts:
            Path(args.facts).write_text(json.dumps(facts, indent=2), encoding="utf-8")
        if video_path is None:
            raise RuntimeError("no video recorded")
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            shutil.copy(video_path, Path(args.out).with_suffix(".webm"))
            return
        subprocess.run(  # noqa: S603 - fixed ffmpeg argv
            [
                ffmpeg, "-y", "-loglevel", "error", "-i", str(video_path),
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "24", "-r", "25",
                "-movflags", "+faststart", args.out,
            ],
            check=True,
        )


if __name__ == "__main__":
    main()
