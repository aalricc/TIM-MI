"""Simulated user traffic: POST one widget to the target every TRAFFIC_INTERVAL_SECONDS.

Writes that land after the last backup are what a restore discards, so MI's data-loss
reward has something to measure. Failed writes (target down or broken) are printed and
skipped; the loop never stops.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

TARGET_URL = os.getenv("TRAFFIC_TARGET_URL", "http://target-app:8080").rstrip("/")
INTERVAL_SECONDS = float(os.getenv("TRAFFIC_INTERVAL_SECONDS", "2"))


def write_widget(sequence: int) -> None:
    body = json.dumps({"value": f"traffic-{sequence}"}).encode("utf-8")
    request = urllib.request.Request(  # noqa: S310 - fixed http URL from compose config
        f"{TARGET_URL}/widgets",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5):  # noqa: S310 - same fixed URL
        pass


def main() -> None:
    sequence = 0
    while True:
        sequence += 1
        try:
            write_widget(sequence)
        except (urllib.error.URLError, OSError) as exc:
            print(f"write {sequence} failed: {exc}", flush=True)
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
