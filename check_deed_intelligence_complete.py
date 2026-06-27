#!/usr/bin/env python3
"""Fail when deed-body OCR intelligence silently produced blanks/errors."""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("intel_dir")
    parser.add_argument("--allow-empty", action="store_true")
    parser.add_argument("--allow-ocr-errors", action="store_true")
    args = parser.parse_args()

    intel_dir = Path(args.intel_dir)
    csv_path = intel_dir / "deed_body_intelligence.csv"
    summary_path = intel_dir / "deed_body_summary.json"
    if not csv_path.exists():
        print(json.dumps({"ok": False, "error": "missing_deed_body_intelligence_csv", "path": str(csv_path)}))
        return 1
    rows = list(csv.DictReader(csv_path.open(newline="", encoding="utf-8")))
    bad_ocr = [row for row in rows if not (row.get("ocr_status") or "").startswith("ok")]
    blank_text = [
        row for row in rows
        if int(row.get("ocr_chars") or 0) < 40 and (row.get("ocr_status") or "").startswith("ok")
    ]
    summary = {}
    if summary_path.exists():
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    ok = True
    if not rows and not args.allow_empty:
        ok = False
    if (bad_ocr or blank_text) and not args.allow_ocr_errors:
        ok = False
    payload = {
        "ok": ok,
        "intel_dir": str(intel_dir),
        "rows": len(rows),
        "bad_ocr_rows": len(bad_ocr),
        "blank_text_rows": len(blank_text),
        "sample_bad_ocr": bad_ocr[:10],
        "sample_blank_text": blank_text[:10],
        "summary": summary,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
