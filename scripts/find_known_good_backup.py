"""Print the newest backup that predates the incident and passes the target's health checks.

The backup job keeps only copies that pass `--check`, but backups written before that change
(or copied by other means) can contain a fault. This script picks the newest backup taken at
or before the last healthy target heartbeat (or --before) and validates it read-only:
PRAGMA integrity_check is ok, and the widgets table has the columns the target reads
(id, name, created_at).

Runs with the standard library only. Inside the compose stack (defaults match the backup-job
container mounts):

    docker compose exec -T backup-job python /scripts/find_known_good_backup.py

Exit codes: 0 backup printed, 1 no valid backup, 2 no cutoff time found.

`--check FILE` validates a single file instead (exit 0 valid, 1 invalid); the backup job
uses it to keep only copies that pass these checks.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

REQUIRED_COLUMNS = {"id", "name", "created_at"}
_BACKUP_NAME = re.compile(r"^app-(\d+)\.db$")


def last_healthy_heartbeat(log_path: Path) -> float | None:
    """Epoch seconds of the last heartbeat logged while the target was healthy."""
    cutoff: float | None = None
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        if row.get("message") == "heartbeat" and row.get("level") == "info":
            try:
                cutoff = datetime.fromisoformat(str(row["ts"])).timestamp()
            except (KeyError, ValueError):
                continue
    return cutoff


def validate_backup(path: Path) -> str | None:
    """None if the backup is usable, otherwise the reason it is not."""
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            integrity = [str(row[0]) for row in conn.execute("PRAGMA integrity_check;")]
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(widgets);")}
    except sqlite3.Error as exc:
        return f"unreadable: {exc}"
    if integrity != ["ok"]:
        return f"integrity_check: {integrity[:3]}"
    if not columns:
        return "widgets table missing"
    missing = REQUIRED_COLUMNS - columns
    if missing:
        return f"widgets missing columns: {sorted(missing)}"
    return None


def find_backup(backups_dir: Path, cutoff: float) -> tuple[Path | None, list[str]]:
    candidates: list[tuple[int, Path]] = []
    for path in backups_dir.iterdir():
        match = _BACKUP_NAME.match(path.name)
        if match and int(match.group(1)) <= cutoff:
            candidates.append((int(match.group(1)), path))
    skipped: list[str] = []
    for _, path in sorted(candidates, reverse=True):
        reason = validate_backup(path)
        if reason is None:
            return path, skipped
        skipped.append(f"{path.name}: {reason}")
    return None, skipped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--log", type=Path, default=Path("/target/app/app.log"))
    parser.add_argument("--backups", type=Path, default=Path("/target/backups"))
    parser.add_argument(
        "--before",
        help="cutoff as ISO-8601 time with offset, or epoch seconds "
        "(default: last healthy heartbeat)",
    )
    parser.add_argument("--check", type=Path, help="validate one database file and exit")
    args = parser.parse_args(argv)

    if args.check is not None:
        reason = validate_backup(args.check)
        if reason is not None:
            print(f"{args.check}: {reason}", file=sys.stderr)
            return 1
        return 0

    if args.before is not None:
        text = str(args.before)
        cutoff: float | None = (
            float(text) if text.replace(".", "", 1).isdigit()
            else datetime.fromisoformat(text).timestamp()
        )
    else:
        cutoff = last_healthy_heartbeat(args.log)
    if cutoff is None:
        print(f"no healthy heartbeat in {args.log}; pass --before", file=sys.stderr)
        return 2

    backup, skipped = find_backup(args.backups, cutoff)
    for line in skipped:
        print(f"skipped {line}", file=sys.stderr)
    if backup is None:
        print(f"no valid backup at or before {cutoff:.0f} in {args.backups}", file=sys.stderr)
        return 1
    print(backup)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
