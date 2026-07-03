#!/usr/bin/env python3
"""Fail when a doc-page pull artifact has missing or capped pages."""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path


# A valid recorder document must have at least one fetched page. NETR sometimes
# returns an upstream 500/end marker when a preview image fails to load, so
# `no_pages` stays retryable instead of being counted complete.
COMPLETE_DOC_STATUSES = {"done"}


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
    status_present = status_path.exists()
    if status_path.exists():
        with status_path.open(newline="", encoding="utf-8") as fh:
            status_rows = list(csv.DictReader(fh))
    status_counts = Counter((row.get("status") or "").strip() for row in status_rows)
    incomplete_docs = [
        row for row in status_rows
        if (row.get("status") or "").strip() not in COMPLETE_DOC_STATUSES
    ]
    has_status_proof = status_present and len(status_rows) > 0
    ok = has_status_proof and failed_pages == 0 and not incomplete_docs
    payload = {
        "ok": ok,
        "outdir": str(outdir),
        "failed_pages": failed_pages,
        "doc_status_present": status_present,
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
