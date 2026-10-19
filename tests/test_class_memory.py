from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from mi_agent.class_memory import (
    ClassMatch,
    ClassMemory,
    ClassMemoryConfig,
    canonical_action_key,
    dedupe_candidates_by_action_key,
)
from mi_agent.workflow.models import Diagnosis, DynamicToolSpec


def _config(tmp_path: Path) -> ClassMemoryConfig:
    return ClassMemoryConfig(
        store_path=tmp_path / "class_store.json",
        join_threshold=0.82,
        margin_delta=0.08,
        tau_conf=0.80,
        n_min=3,
        cooldown_seconds=5.0,
        embedding_dim=64,
        min_similarity_for_id=0.2,
    )


def _diagnosis() -> Diagnosis:
    return Diagnosis(
        failure_type="generic_failure",
        hypotheses=["hypothesis-a"],
        evidence=["evidence-a"],
    )


def _diagnostics_with_log(line: str) -> dict[str, object]:
    return {
        "health_deep": {"ok": False, "error": "generic_failure"},
        "db_status": {"ok": False, "error": "generic_failure"},
        "log_tail": [line],
    }


def _run_sql_spec(name: str, sql: str) -> DynamicToolSpec:
    return DynamicToolSpec(
        name=name,
        rationale="r",
        operation="run_sql",
        params={"db_path": "data/app.db", "sql": sql},
    )


def test_template_mining_collapses_variants_and_separates_families(tmp_path: Path) -> None:
    mem = ClassMemory(_config(tmp_path))

    sig_a = mem.build_signature(
        diagnostics=_diagnostics_with_log("table alpha_001 missing at /tmp/a/1.log"),
        diagnosis=_diagnosis(),
        attempts=0,
    )
    sig_b = mem.build_signature(
        diagnostics=_diagnostics_with_log("table alpha_889 missing at /var/log/app/2.log"),
        diagnosis=_diagnosis(),
        attempts=0,
    )
    sig_c = mem.build_signature(
        diagnostics=_diagnostics_with_log("network timeout while reading upstream response"),
        diagnosis=_diagnosis(),
        attempts=0,
    )

    assert sig_a.template_ids == sig_b.template_ids
    assert sig_a.template_ids != sig_c.template_ids


def test_clustering_class_ids_stable_across_ingestion_order(tmp_path: Path) -> None:
    cases = [
        _diagnostics_with_log("worker 1001 failed on path /opt/a/one"),
        _diagnostics_with_log("worker 2002 failed on path /opt/a/two"),
        _diagnostics_with_log("remote endpoint refused socket handshake"),
    ]

    cfg_one = _config(tmp_path / "one")
    cfg_one.join_threshold = 0.98
    mem_one = ClassMemory(cfg_one)
    forward: dict[tuple[str, ...], str] = {}
    for payload in cases:
        sig = mem_one.build_signature(diagnostics=payload, diagnosis=_diagnosis(), attempts=0)
        match = mem_one.assign_class(sig)
        forward[tuple(sig.template_ids)] = match.class_id

    cfg_two = _config(tmp_path / "two")
    cfg_two.join_threshold = 0.98
    mem_two = ClassMemory(cfg_two)
    backward: dict[tuple[str, ...], str] = {}
    for payload in reversed(cases):
        sig = mem_two.build_signature(diagnostics=payload, diagnosis=_diagnosis(), attempts=0)
        match = mem_two.assign_class(sig)
        backward[tuple(sig.template_ids)] = match.class_id

    assert forward == backward


def test_canonical_action_key_ignores_tool_name_for_same_effect() -> None:
    one = _run_sql_spec("name-a", "CREATE TABLE IF NOT EXISTS t(id INTEGER PRIMARY KEY);")
    two = _run_sql_spec("name-b", "CREATE TABLE IF NOT EXISTS t(id INTEGER PRIMARY KEY);")

    assert canonical_action_key(one) == canonical_action_key(two)


def test_dedupe_and_tie_break_deterministic() -> None:
    base = [
        _run_sql_spec("a", "CREATE TABLE IF NOT EXISTS t(id INTEGER PRIMARY KEY);"),
        _run_sql_spec("b", "CREATE TABLE IF NOT EXISTS t(id INTEGER PRIMARY KEY);"),
        _run_sql_spec("c", "CREATE TABLE IF NOT EXISTS t(id INTEGER PRIMARY KEY, value TEXT);"),
    ]

    deduped = dedupe_candidates_by_action_key(base)
    expected = {
        canonical_action_key(base[0]): "a",
        canonical_action_key(base[2]): "c",
    }
    expected_order = [expected[key] for key in sorted(expected)]
    assert [item.name for item in deduped] == expected_order
    assert [canonical_action_key(item) for item in deduped] == sorted(expected)

    deduped_repeat = dedupe_candidates_by_action_key(base)
    assert [item.name for item in deduped_repeat] == expected_order


def test_fast_path_gate_conditions_block_independently(tmp_path: Path) -> None:
    mem = ClassMemory(_config(tmp_path))
    sig = mem.build_signature(
        diagnostics=_diagnostics_with_log("state token 111 path /x/y"),
        diagnosis=_diagnosis(),
        attempts=0,
    )
    match = mem.assign_class(sig)

    spec = _run_sql_spec("winner", "CREATE TABLE IF NOT EXISTS t(id INTEGER PRIMARY KEY);")
    for _ in range(4):
        mem.record_outcome(match=match, spec=spec, latency_ms=5.0, success=True, source="full_loop")

    rejected = mem.choose_fast_path(
        match=ClassMatch(
            class_id=match.class_id,
            signature=sig,
            s1=0.10,
            s2=0.05,
            margin=0.05,
            created_new=False,
        ),
        failed_action_keys=set(),
    )
    assert rejected.gate_reason == "s1_below_threshold"

    rejected = mem.choose_fast_path(
        match=ClassMatch(
            class_id=match.class_id,
            signature=sig,
            s1=0.95,
            s2=0.90,
            margin=0.05,
            created_new=False,
        ),
        failed_action_keys=set(),
    )
    assert rejected.gate_reason == "margin_below_delta"

    key = canonical_action_key(spec)
    rejected = mem.choose_fast_path(match=match, failed_action_keys={key})
    assert rejected.gate_reason == "no_confident_action"

    low_attempt_cfg = _config(tmp_path / "low-attempt")
    low_attempt_mem = ClassMemory(low_attempt_cfg)
    low_attempt_sig = low_attempt_mem.build_signature(
        diagnostics=_diagnostics_with_log("state token 222 path /x/y"),
        diagnosis=_diagnosis(),
        attempts=0,
    )
    low_attempt_match = low_attempt_mem.assign_class(low_attempt_sig)
    for _ in range(2):
        low_attempt_mem.record_outcome(
            match=low_attempt_match,
            spec=spec,
            latency_ms=5.0,
            success=True,
            source="full_loop",
        )
    rejected = low_attempt_mem.choose_fast_path(
        match=low_attempt_match,
        failed_action_keys=set(),
    )
    assert rejected.gate_reason == "no_confident_action"

    high_tau_cfg = _config(tmp_path / "high-tau")
    high_tau_cfg.tau_conf = 0.95
    high_tau_mem = ClassMemory(high_tau_cfg)
    high_tau_sig = high_tau_mem.build_signature(
        diagnostics=_diagnostics_with_log("state token 333 path /x/y"),
        diagnosis=_diagnosis(),
        attempts=0,
    )
    high_tau_match = high_tau_mem.assign_class(high_tau_sig)
    for _ in range(4):
        high_tau_mem.record_outcome(
            match=high_tau_match,
            spec=spec,
            latency_ms=5.0,
            success=True,
            source="full_loop",
        )
    rejected = high_tau_mem.choose_fast_path(
        match=high_tau_match,
        failed_action_keys=set(),
    )
    assert rejected.gate_reason == "lcb_below_tau"

    cooldown_cfg = _config(tmp_path / "cooldown")
    cooldown_cfg.n_min = 1
    cooldown_cfg.tau_conf = 0.0
    cooldown_mem = ClassMemory(cooldown_cfg)
    cooldown_sig = cooldown_mem.build_signature(
        diagnostics=_diagnostics_with_log("state token 444 path /x/y"),
        diagnosis=_diagnosis(),
        attempts=0,
    )
    cooldown_match = cooldown_mem.assign_class(cooldown_sig)
    cooldown_mem.record_outcome(
        match=cooldown_match,
        spec=spec,
        latency_ms=5.0,
        success=False,
        source="full_loop",
    )
    rejected = cooldown_mem.choose_fast_path(
        match=cooldown_match,
        failed_action_keys=set(),
    )
    assert rejected.gate_reason == "no_confident_action"


def test_skills_md_upsert_preserves_external_text_and_single_section(tmp_path: Path) -> None:
    skills_path = tmp_path / "skills.md"
    prefix = "Legacy Header\n\n"
    suffix = "\nLegacy Footer\n"
    skills_path.write_text(prefix + suffix, encoding="utf-8")

    ClassMemory.upsert_skills_section(
        skills_path,
        class_id="abc123",
        section_body="# Class abc123\n- a: 1",
    )
    ClassMemory.upsert_skills_section(
        skills_path,
        class_id="abc123",
        section_body="# Class abc123\n- a: 2",
    )

    content = skills_path.read_text(encoding="utf-8")
    assert content.startswith(prefix)
    assert suffix in content
    assert content.count("<!-- class:abc123 begin -->") == 1
    assert "- a: 2" in content


def test_convergence_to_one_action_key_after_n_min(tmp_path: Path) -> None:
    mem = ClassMemory(_config(tmp_path))
    sig = mem.build_signature(
        diagnostics=_diagnostics_with_log("engine code 1001 on /tmp/path"),
        diagnosis=_diagnosis(),
        attempts=0,
    )
    match = mem.assign_class(sig)

    variants = [
        _run_sql_spec("n1", "CREATE TABLE IF NOT EXISTS t(id INTEGER PRIMARY KEY, value TEXT);"),
        _run_sql_spec("n2", "CREATE TABLE IF NOT EXISTS t(id INTEGER PRIMARY KEY, value TEXT);"),
        _run_sql_spec("n3", "CREATE TABLE IF NOT EXISTS t(id INTEGER PRIMARY KEY, value TEXT);"),
    ]
    for index in range(6):
        mem.record_outcome(
            match=match,
            spec=variants[index % len(variants)],
            latency_ms=2.0,
            success=True,
            source="full_loop",
        )

    decision = mem.choose_fast_path(match=match, failed_action_keys=set())
    assert decision.eligible is True
    assert decision.action_key == canonical_action_key(variants[0])


def test_persistence_reload_corrupt_backup_and_schema_version_handling(tmp_path: Path) -> None:
    store_path = tmp_path / "class_store.json"
    store_path.write_text("{not-json", encoding="utf-8")

    mem = ClassMemory(
        ClassMemoryConfig(
            store_path=store_path,
            join_threshold=0.82,
            margin_delta=0.08,
            tau_conf=0.80,
            n_min=3,
            cooldown_seconds=5.0,
            embedding_dim=64,
            min_similarity_for_id=0.2,
        )
    )
    assert mem.class_counts() == 0
    assert len(list(tmp_path.glob("class_store.json.corrupt-*"))) == 1

    store_path.write_text(
        json.dumps({"schema_version": 999, "classes": {"a": {}}}),
        encoding="utf-8",
    )
    mem2 = ClassMemory(
        ClassMemoryConfig(
            store_path=store_path,
            join_threshold=0.82,
            margin_delta=0.08,
            tau_conf=0.80,
            n_min=3,
            cooldown_seconds=5.0,
            embedding_dim=64,
            min_similarity_for_id=0.2,
        )
    )
    assert mem2.class_counts() == 0


def test_concurrent_writers_do_not_corrupt_store_or_skills(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    skills_path = tmp_path / "skills.md"

    def _writer(seed: int) -> None:
        mem = ClassMemory(cfg)
        diagnosis = Diagnosis(
            failure_type=f"failure_{seed}",
            hypotheses=[f"hyp_{seed}"],
            evidence=[f"ev_{seed}"],
        )
        sig = mem.build_signature(
            diagnostics=_diagnostics_with_log(f"line {seed} path /tmp/{seed}"),
            diagnosis=diagnosis,
            attempts=0,
        )
        match = mem.assign_class(sig)
        spec = _run_sql_spec(
            f"tool-{seed}",
            "CREATE TABLE IF NOT EXISTS t(id INTEGER PRIMARY KEY);",
        )
        mem.record_outcome(
            match=match,
            spec=spec,
            latency_ms=1.0,
            success=True,
            source="full_loop",
        )
        section = mem.render_skills_section(class_id=match.class_id)
        assert section is not None
        ClassMemory.upsert_skills_section(
            skills_path,
            class_id=match.class_id,
            section_body=section,
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(_writer, range(8)))

    parsed = json.loads((tmp_path / "class_store.json").read_text(encoding="utf-8"))
    assert isinstance(parsed.get("classes"), dict)
    content = skills_path.read_text(encoding="utf-8")
    assert "<!-- class:" in content
