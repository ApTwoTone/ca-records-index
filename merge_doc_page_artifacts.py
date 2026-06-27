#!/usr/bin/env python3
"""Merge doc-page pull artifacts, including deed-body intelligence sidecars."""
from __future__ import annotations

from collections import Counter
import csv
import glob
import json
import os
from pathlib import Path
import sys

import analyze_deed_body_patterns


def read_csvs(pattern: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for path in sorted(glob.glob(pattern, recursive=True)):
        with open(path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                row["_source_file"] = path
                rows.append(row)
    return rows


def write_union_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields: list[str] = []
    seen = set()
    for row in rows:
        for field in row.keys():
            if field not in seen:
                seen.add(field)
                fields.append(field)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    if len(sys.argv) != 3:
        print("Usage: merge_doc_page_artifacts.py <artifacts_dir> <out_dir>", file=sys.stderr)
        return 2
    artifacts_dir = Path(sys.argv[1])
    out_dir = Path(sys.argv[2])
    out_dir.mkdir(parents=True, exist_ok=True)

    pages = read_csvs(str(artifacts_dir / "**" / "pages_manifest.csv"))
    failed = read_csvs(str(artifacts_dir / "**" / "failed_pages.csv"))
    statuses = read_csvs(str(artifacts_dir / "**" / "doc_status.csv"))
    deed_intel = read_csvs(str(artifacts_dir / "**" / "deed_body_intelligence.csv"))

    write_union_csv(out_dir / "pages_manifest_merged.csv", pages)
    write_union_csv(out_dir / "failed_pages_merged.csv", failed)
    write_union_csv(out_dir / "doc_status_merged.csv", statuses)
    deed_intel_out = out_dir / "deed_body_intelligence_merged.csv"
    write_union_csv(deed_intel_out, deed_intel)
    pattern_summary = {}
    if deed_intel:
        pattern_summary = analyze_deed_body_patterns.analyze(deed_intel_out, out_dir / "patterns")

    status_counts = Counter(row.get("status", "") for row in statuses)
    ocr_counts = Counter(row.get("ocr_status", "") for row in deed_intel)
    tags = Counter()
    for row in deed_intel:
        try:
            values = json.loads(row.get("buyer_seller_intel_tags") or "[]")
        except Exception:
            values = []
        for value in values:
            tags[str(value)] += 1

    summary = {
        "artifacts_dir": str(artifacts_dir),
        "out_dir": str(out_dir),
        "pages_manifest_rows": len(pages),
        "failed_pages_rows": len(failed),
        "doc_status_rows": len(statuses),
        "deed_body_intelligence_rows": len(deed_intel),
        "foreign_entity_docs": sum(1 for row in deed_intel if row.get("foreign_entity_flag") == "true"),
        "international_mail_docs": sum(1 for row in deed_intel if row.get("mail_to_international_flag") == "true"),
        "mineral_signal_docs": sum(1 for row in deed_intel if row.get("mineral_rights_signal") == "true"),
        "doc_status_counts": dict(status_counts),
        "ocr_status_counts": dict(ocr_counts),
        "buyer_seller_intel_tag_counts": dict(tags),
        "outputs": {
            "pages_manifest_merged_csv": str(out_dir / "pages_manifest_merged.csv"),
            "failed_pages_merged_csv": str(out_dir / "failed_pages_merged.csv"),
            "doc_status_merged_csv": str(out_dir / "doc_status_merged.csv"),
            "deed_body_intelligence_merged_csv": str(deed_intel_out),
            "pattern_summary_json": pattern_summary.get("outputs", {}),
        },
    }
    (out_dir / "doc_page_artifacts_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
