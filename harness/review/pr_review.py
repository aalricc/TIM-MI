"""Selective advisory LLM reviews for PRs and failed campaign reflections.

Usage:
    python -m harness.review.pr_review pr --files-file <paths.txt> > <markdown>
    python -m harness.review.pr_review reflect --campaign-json <campaign.json> > <markdown>
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx

_OPENAI_ENDPOINT = "https://api.openai.com/v1/responses"


def _extract_text(payload: dict[str, Any]) -> str:
    output = payload.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for row in content:
                if not isinstance(row, dict):
                    continue
                if row.get("type") in {"output_text", "text"}:
                    text = row.get("text")
                    if isinstance(text, str) and text.strip() != "":
                        return text
    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip() != "":
        return output_text
    raise ValueError("model response did not include output text")


def _openai_markdown(
    *,
    system_prompt: str,
    user_payload: dict[str, Any],
    model: str,
    heading: str,
) -> str:
    api_key = os.getenv("OPENAI_API_KEY", "")
    if api_key == "":
        return f"## {heading}\n\nSkipped: `OPENAI_API_KEY` is not set for this run.\n"

    body = {
        "model": model,
        "input": [
            {
                "role": "system",
                "content": [{"type": "input_text", "text": system_prompt}],
            },
            {
                "role": "user",
                "content": [{"type": "input_text", "text": json.dumps(user_payload)}],
            },
        ],
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.post(_OPENAI_ENDPOINT, headers=headers, json=body)
            response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("response was not a JSON object")
        return _extract_text(payload)
    except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
        return (
            f"## {heading}\n\n"
            "Model call failed; keeping PR non-blocking.\n\n"
            f"Error: `{type(exc).__name__}: {exc}`\n"
        )


def _read_changed_files(files_file: Path) -> list[str]:
    rows = files_file.read_text(encoding="utf-8").splitlines()
    return [row.strip() for row in rows if row.strip() != ""]


def _read_file_snippets(
    paths: list[str], *, max_files: int = 20, max_chars: int = 4000
) -> list[dict[str, str]]:
    snippets: list[dict[str, str]] = []
    for raw_path in paths[:max_files]:
        path = Path(raw_path)
        if not path.exists() or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        snippet = text[:max_chars]
        if len(text) > max_chars:
            snippet += "\n...<truncated>"
        snippets.append({"path": raw_path, "content": snippet})
    return snippets


def _pr_review_markdown(*, changed_paths: list[str], model: str) -> str:
    if not changed_paths:
        return "## Advisory LLM Review\n\nNo risky-path files changed.\n"

    snippets = _read_file_snippets(changed_paths)
    system_prompt = (
        "You are reviewing a PR for architecture and code quality. "
        "Focus only on concrete, high-signal findings tied to changed files. "
        "Keep it advisory and concise. Output markdown."
    )
    user_payload = {
        "task": "selective_pr_architecture_review",
        "checks": [
            "duplication",
            "over-engineering",
            "invariant risk",
            "security/approval boundary risk",
            "test coverage gaps",
        ],
        "changed_files": changed_paths,
        "snippets": snippets,
        "required_sections": ["Summary", "Findings", "Suggested Follow-ups"],
        "constraints": [
            "advisory only",
            "no merge-blocking language",
            "cite paths only from changed_files",
        ],
    }
    return _openai_markdown(
        system_prompt=system_prompt,
        user_payload=user_payload,
        model=model,
        heading="Advisory LLM Review",
    )


def _reflection_markdown(*, campaign_report: dict[str, Any], model: str) -> str:
    status = campaign_report.get("overall_status")
    if status == "pass":
        return "## Log-Quality Reflection\n\nCampaign passed; reflection skipped.\n"

    system_prompt = (
        "You are writing a post-failure log-quality reflection for a fault campaign. "
        "Focus on observability blind spots and actionable logging improvements. "
        "Output markdown with concise, concrete bullets."
    )
    user_payload = {
        "task": "cp7_failed_campaign_log_quality_reflection",
        "campaign_report": campaign_report,
        "required_sections": ["Failure Summary", "Log/Signal Gaps", "Concrete Improvements"],
    }
    return _openai_markdown(
        system_prompt=system_prompt,
        user_payload=user_payload,
        model=model,
        heading="Log-Quality Reflection",
    )


def _pr_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("pr", help="Generate advisory PR review markdown")
    parser.add_argument("--files-file", type=Path, required=True)
    parser.add_argument("--model", default="gpt-5.3")


def _reflect_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("reflect", help="Generate failed-campaign reflection markdown")
    parser.add_argument("--campaign-json", type=Path, required=True)
    parser.add_argument("--model", default="gpt-5.3")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m harness.review.pr_review")
    subparsers = parser.add_subparsers(dest="command", required=True)
    _pr_parser(subparsers)
    _reflect_parser(subparsers)
    return parser


def main(argv: list[str]) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv[1:])
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2

    if args.command == "pr":
        try:
            changed_paths = _read_changed_files(args.files_file)
        except OSError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        markdown = _pr_review_markdown(changed_paths=changed_paths, model=args.model)
    else:
        try:
            campaign_report = json.loads(args.campaign_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        if not isinstance(campaign_report, dict):
            print("campaign report must be a JSON object", file=sys.stderr)
            return 2
        markdown = _reflection_markdown(campaign_report=campaign_report, model=args.model)

    print(markdown.strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

