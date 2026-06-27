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
import json
import os
import sys
import datetime
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lead_class as lc


DOC_FIELDS = ["ain", "doc_no", "record_date", "county_type", "grantors", "grantees", "lead_class"]
SCAN_FIELDS = ["ain", "doc_count", "status"]
UNFINISHED_FIELDS = ["ain", "doc_count", "status"]


def now_utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
    shard_summary_paths = sorted(glob.glob(os.path.join(artifacts_dir, "**", "*_summary.json"), recursive=True))

    docs = {}
    dropped_docs = Counter()
    for row in doc_rows:
        ain = (row.get("ain") or "").strip()
        doc = (row.get("doc_no") or "").strip()
        if not ain:
            dropped_docs["blank_ain"] += 1
            continue
        if not doc:
            dropped_docs["blank_doc_no"] += 1
            continue
        out = {field: (row.get(field) or "") for field in DOC_FIELDS}
        out["lead_class"] = lc.lead_class(out.get("county_type"))
        docs[(ain, doc)] = out

    scans = {}
    dropped_scans = Counter()
    duplicate_scan_rows = 0
    for row in scan_rows:
        ain = (row.get("ain") or "").strip()
        if not ain:
            dropped_scans["blank_ain"] += 1
            continue
        if ain in scans:
            duplicate_scan_rows += 1
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

    unfinished = [row for row in scans.values() if (row.get("status") or "") != "done"]
    unfinished_out = os.path.splitext(scan_out)[0] + "_unfinished_ains.csv"
    summary_out = os.path.splitext(scan_out)[0] + "_summary.json"
    with open(unfinished_out, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=UNFINISHED_FIELDS)
        writer.writeheader()
        for row in sorted(unfinished, key=lambda r: r.get("ain") or ""):
            writer.writerow({field: row.get(field, "") for field in UNFINISHED_FIELDS})

    status_counts = Counter(row.get("status") or "" for row in scans.values())
    class_counts = Counter(row.get("lead_class") or "" for row in docs.values())
    type_counts = Counter(row.get("county_type") or "" for row in docs.values())
    summary = {
        "generated_at_utc": now_utc(),
        "artifacts_dir": os.path.abspath(artifacts_dir),
        "docs_rows_in": len(doc_rows),
        "docs_rows_out": len(docs),
        "unique_doc_no": len({doc for _, doc in docs}),
        "scan_rows_in": len(scan_rows),
        "scan_rows_out": len(scans),
        "unfinished_ains": len(unfinished),
        "status_counts": dict(status_counts),
        "lead_class_counts": dict(class_counts),
        "county_type_counts": dict(type_counts),
        "dropped_docs": dict(dropped_docs),
        "dropped_scans": dict(dropped_scans),
        "duplicate_scan_rows_last_wins": duplicate_scan_rows,
        "sidecar_summary_files_seen": len(shard_summary_paths),
        "outputs": {
            "docs_csv": os.path.abspath(docs_out),
            "scan_csv": os.path.abspath(scan_out),
            "unfinished_ains_csv": os.path.abspath(unfinished_out),
            "summary_json": os.path.abspath(summary_out),
        },
    }
    with open(summary_out, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, sort_keys=True)

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
    print("unfinished_out: %s" % os.path.abspath(unfinished_out))
    print("summary_out: %s" % os.path.abspath(summary_out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
