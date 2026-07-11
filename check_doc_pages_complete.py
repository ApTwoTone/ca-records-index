#!/usr/bin/env python3
"""Fail when a doc-page artifact lacks cryptographic terminal proof."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path

from doc_page_terminal_proof import validate_artifact_dir


COMPLETE_DOC_STATUSES = {"done"}


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("outdir")
    parser.add_argument("--allow-partial", action="store_true")
    args = parser.parse_args()

    outdir = Path(args.outdir)
    failed_rows = read_csv(outdir / "failed_pages.csv")
    status_rows = read_csv(outdir / "doc_status.csv")
    status_counts = Counter((row.get("status") or "").strip() for row in status_rows)
    incomplete_docs = [
        row for row in status_rows
        if (row.get("status") or "").strip() not in COMPLETE_DOC_STATUSES
    ]
    proof = validate_artifact_dir(outdir)
    ok = (
        bool(status_rows)
        and not failed_rows
        and not incomplete_docs
        and proof["complete"] is True
    )
    payload = {
        "ok": ok,
        "outdir": str(outdir),
        "failed_pages": len(failed_rows),
        "doc_status_present": (outdir / "doc_status.csv").exists(),
        "doc_status_rows": len(status_rows),
        "doc_status_counts": dict(status_counts),
        "incomplete_docs": len(incomplete_docs),
        "sample_incomplete_docs": incomplete_docs[:20],
        "manifest_rows": proof["manifest_rows"],
        "terminal_evidence_rows": proof["terminal_evidence_rows"],
        "terminal_proven_docs": len(proof["proven_docs"]),
        "terminal_proof_errors": len(proof["errors"]),
        "sample_terminal_proof_errors": proof["errors"][:20],
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    if not ok and not args.allow_partial:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
