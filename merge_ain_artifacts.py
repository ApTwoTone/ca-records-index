#!/usr/bin/env python3
"""Merge ain-fanout artifacts without losing parcel-history rows.

Inputs are the GitHub artifact directories containing:
- shard_<n>_docs.csv: ain,doc_no,record_date,county_type,grantors,grantees
- shard_<n>_scan.csv: ain,doc_count,status

Outputs preserve the AIN/document pair because the same document can be relevant
to more than one parcel. Classification is added from lead_class.py for operator
triage, but the raw county_type/grantors/grantees remain intact.
"""
from __future__ import annotations

import csv
import glob
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lead_class as lc


DOC_FIELDS = ["ain", "doc_no", "record_date", "county_type", "grantors", "grantees", "lead_class"]
SCAN_FIELDS = ["ain", "doc_count", "status"]


def read_rows(pattern: str) -> list[dict]:
    rows: list[dict] = []
    for path in sorted(glob.glob(pattern, recursive=True)):
        with open(path, newline="", encoding="utf-8") as fh:
            rows.extend(csv.DictReader(fh))
    return rows


def main() -> int:
    if len(sys.argv) != 4:
        print("Usage: merge_ain_artifacts.py <artifacts_dir> <docs_out.csv> <scan_out.csv>", file=sys.stderr)
        return 2
    artifacts_dir, docs_out, scan_out = sys.argv[1:]

    doc_rows = read_rows(os.path.join(artifacts_dir, "**", "*_docs.csv"))
    scan_rows = read_rows(os.path.join(artifacts_dir, "**", "*_scan.csv"))

    docs = {}
    for row in doc_rows:
        ain = (row.get("ain") or "").strip()
        doc = (row.get("doc_no") or "").strip()
        if not ain or not doc:
            continue
        out = {field: (row.get(field) or "") for field in DOC_FIELDS}
        out["lead_class"] = lc.lead_class(out.get("county_type"))
        docs[(ain, doc)] = out

    scans = {}
    for row in scan_rows:
        ain = (row.get("ain") or "").strip()
        if not ain:
            continue
        scans[ain] = {field: (row.get(field) or "") for field in SCAN_FIELDS}

    os.makedirs(os.path.dirname(os.path.abspath(docs_out)), exist_ok=True)
    os.makedirs(os.path.dirname(os.path.abspath(scan_out)), exist_ok=True)
    with open(docs_out, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=DOC_FIELDS)
        writer.writeheader()
        for _, row in sorted(docs.items(), key=lambda item: (item[0][0], int(item[0][1]) if item[0][1].isdigit() else item[0][1])):
            writer.writerow(row)

    with open(scan_out, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=SCAN_FIELDS)
        writer.writeheader()
        for _, row in sorted(scans.items()):
            writer.writerow(row)

    status_counts = Counter(row.get("status") or "" for row in scans.values())
    class_counts = Counter(row.get("lead_class") or "" for row in docs.values())
    type_counts = Counter(row.get("county_type") or "" for row in docs.values())
    print("AIN docs rows=%d unique_doc_no=%d scanned_ains=%d" % (
        len(docs),
        len({doc for _, doc in docs}),
        len(scans),
    ))
    print("=== SCAN STATUS ===")
    for status, count in status_counts.most_common():
        print("%8d  %s" % (count, status or "(blank)"))
    print("=== LEAD CLASS TOP 25 ===")
    for lead_class, count in class_counts.most_common(25):
        print("%8d  %s" % (count, lead_class or "(blank)"))
    print("=== COUNTY TYPE TOP 25 ===")
    for county_type, count in type_counts.most_common(25):
        print("%8d  %s" % (count, county_type or "(blank)"))
    print("docs_out: %s" % os.path.abspath(docs_out))
    print("scan_out: %s" % os.path.abspath(scan_out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
