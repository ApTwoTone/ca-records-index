#!/usr/bin/env python3
"""Fail when a doc-page pull artifact has missing or capped pages."""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path


COMPLETE_DOC_STATUSES = {"done", "no_pages"}


def count_csv_rows(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open(newline="", encoding="utf-8") as fh:
        return sum(1 for _ in csv.DictReader(fh))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("outdir")
    parser.add_argument("--allow-partial", action="store_true")
    args = parser.parse_args()

    outdir = Path(args.outdir)
    failed_pages = count_csv_rows(outdir / "failed_pages.csv")
    status_rows = []
    status_path = outdir / "doc_status.csv"
    if status_path.exists():
        with status_path.open(newline="", encoding="utf-8") as fh:
            status_rows = list(csv.DictReader(fh))
    status_counts = Counter((row.get("status") or "").strip() for row in status_rows)
    incomplete_docs = [
        row for row in status_rows
        if (row.get("status") or "").strip() not in COMPLETE_DOC_STATUSES
    ]
    ok = failed_pages == 0 and not incomplete_docs
    payload = {
        "ok": ok,
        "outdir": str(outdir),
        "failed_pages": failed_pages,
        "doc_status_rows": len(status_rows),
        "doc_status_counts": dict(status_counts),
        "incomplete_docs": len(incomplete_docs),
        "sample_incomplete_docs": incomplete_docs[:20],
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    if not ok and not args.allow_partial:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
