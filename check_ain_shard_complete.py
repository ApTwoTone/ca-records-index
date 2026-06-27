#!/usr/bin/env python3
"""Fail when an AIN shard artifact is partial.

The harvester intentionally uploads partial CSVs so retries can recover them.
This checker runs after upload to keep GitHub's green check from meaning
"some rows finished". A clean shard means every scanned AIN is status=done.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("scan_csv")
    parser.add_argument("--allow-partial", action="store_true")
    args = parser.parse_args()

    path = Path(args.scan_csv)
    if not path.exists():
        print(json.dumps({"ok": False, "error": "missing_scan_csv", "path": str(path)}))
        return 1

    rows = list(csv.DictReader(path.open(newline="", encoding="utf-8")))
    counts = Counter((row.get("status") or "").strip() for row in rows)
    unfinished = [row for row in rows if (row.get("status") or "").strip() != "done"]
    payload = {
        "ok": not unfinished,
        "scan_csv": str(path),
        "scan_rows": len(rows),
        "status_counts": dict(counts),
        "unfinished_ains": len(unfinished),
        "sample_unfinished": unfinished[:20],
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    if unfinished and not args.allow_partial:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
