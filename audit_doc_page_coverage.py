#!/usr/bin/env python3
"""Audit NETR document-page artifacts for page, OCR, and field completeness.

This is intentionally artifact-first: a failed Actions run can still contain
useful PNG/OCR/index data, but every capped/failed/blank condition must become a
retry row instead of a quiet "no signal" conclusion.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import contextmanager
import csv
import glob
import json
from pathlib import Path
import signal
import sys


COMPLETE_DOC_STATUSES = {"done", "no_pages"}
OK_PAGE_STATUSES = {"ok", "ok_existing"}
LOW_OCR_CHAR_THRESHOLD = 40
INPUT_READ_TIMEOUT_SECONDS = 20

PAGE_PATTERNS = ("**/pages_manifest.csv", "**/pages_manifest_merged.csv")
FAILED_PATTERNS = ("**/failed_pages.csv", "**/failed_pages_merged.csv")
STATUS_PATTERNS = ("**/doc_status.csv", "**/doc_status_merged.csv")
INTEL_PATTERNS = ("**/deed_body_intelligence.csv", "**/deed_body_intelligence_merged.csv")

INDEX_FIELDS = [
    "index_ains",
    "index_record_dates",
    "index_county_types",
    "index_grantors",
    "index_grantees",
]
BODY_FIELDS = [
    "apns_all",
    "recording_requested_by_raw",
    "mail_to_raw",
    "mail_to_country",
    "address_blocks_raw",
    "body_grantee_raw",
    "entity_domicile_phrases",
    "foreign_entity_jurisdictions",
    "company_numbers",
    "document_date_lines_raw",
    "transfer_tax_raw",
    "estimated_consideration_from_county_tax",
    "mineral_terms",
    "buyer_seller_intel_tags",
]
FLAG_FIELDS = [
    "foreign_entity_flag",
    "mail_to_international_flag",
    "mineral_rights_signal",
    "trustee_trust_signal",
    "corporate_party_from_index_flag",
]


class InputReadTimeout(RuntimeError):
    pass


@contextmanager
def read_deadline(seconds: int):
    if not hasattr(signal, "SIGALRM"):
        yield
        return

    def _raise_timeout(_signum, _frame):
        raise InputReadTimeout(f"input read exceeded {seconds}s")

    old_handler = signal.signal(signal.SIGALRM, _raise_timeout)
    old_timer = signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)
        if old_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, old_timer[0], old_timer[1])


def read_csvs(root: Path, patterns: tuple[str, ...], warnings: list[dict[str, str]]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen_files: set[Path] = set()
    for pattern in patterns:
        for raw_path in sorted(glob.glob(str(root / pattern), recursive=True)):
            path = Path(raw_path)
            if path in seen_files:
                continue
            seen_files.add(path)
            try:
                with read_deadline(INPUT_READ_TIMEOUT_SECONDS):
                    with path.open(newline="", encoding="utf-8") as fh:
                        for row in csv.DictReader(fh):
                            row["_source_file"] = str(path)
                            rows.append(row)
            except Exception as exc:
                warnings.append({
                    "path": str(path),
                    "error": type(exc).__name__,
                    "message": str(exc),
                    "impact": "file_skipped_audit_is_partial",
                })
    return rows


def read_doclist(path: Path | None, warnings: list[dict[str, str]]) -> list[str]:
    if not path:
        return []
    docs: list[str] = []
    seen: set[str] = set()
    try:
        with read_deadline(INPUT_READ_TIMEOUT_SECONDS):
            text = path.read_text(encoding="utf-8")
    except Exception as exc:
        warnings.append({
            "path": str(path),
            "error": type(exc).__name__,
            "message": str(exc),
            "impact": "expected_doclist_skipped_audit_is_partial",
        })
        return []
    for raw in text.replace(",", "\n").splitlines():
        doc = raw.strip()
        if doc and doc not in seen:
            seen.add(doc)
            docs.append(doc)
    return docs


def parse_int(value: str | None, default: int = 0) -> int:
    try:
        return int(str(value or "").strip())
    except Exception:
        return default


def parse_jsonish(value: str | None) -> object:
    text = (value or "").strip()
    if not text:
        return ""
    try:
        return json.loads(text)
    except Exception:
        return text


def has_value(value: str | None) -> bool:
    parsed = parse_jsonish(value)
    if parsed in ("", None, [], {}):
        return False
    if isinstance(parsed, list):
        return any(str(v).strip() for v in parsed)
    return bool(str(parsed).strip())


def latest_by_time(rows: list[dict[str, str]], time_field: str) -> dict[str, str] | None:
    if not rows:
        return None
    return max(rows, key=lambda r: (r.get(time_field) or "", parse_int(r.get("last_page_checked") or r.get("pages_ocrd"))))


def doc_sort_key(doc: str) -> tuple[int, str]:
    return (0, f"{int(doc):020d}") if doc.isdigit() else (1, doc)


def join_status_counts(counter: Counter[str]) -> str:
    return json.dumps(dict(sorted(counter.items())), sort_keys=True)


def write_csv(path: Path, rows: list[dict[str, object]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_doclist(path: Path, docs: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(docs) + ("\n" if docs else ""), encoding="utf-8")


def load_source_index(path: Path | None, warnings: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    if not path:
        return {}
    out: dict[str, dict[str, str]] = {}
    try:
        with read_deadline(INPUT_READ_TIMEOUT_SECONDS):
            with path.open(newline="", encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    doc = (row.get("doc_no") or "").strip()
                    if doc:
                        out.setdefault(doc, row)
    except Exception as exc:
        warnings.append({
            "path": str(path),
            "error": type(exc).__name__,
            "message": str(exc),
            "impact": "source_index_skipped_audit_is_partial",
        })
    return out


def audit(root: Path, out_dir: Path, expected_doclist: Path | None = None, source_index_csv: Path | None = None) -> dict[str, object]:
    out_dir.mkdir(parents=True, exist_ok=True)

    read_warnings: list[dict[str, str]] = []
    expected_docs = read_doclist(expected_doclist, read_warnings)
    source_index = load_source_index(source_index_csv, read_warnings)
    pages = read_csvs(root, PAGE_PATTERNS, read_warnings)
    failed = read_csvs(root, FAILED_PATTERNS, read_warnings)
    statuses = read_csvs(root, STATUS_PATTERNS, read_warnings)
    intel = read_csvs(root, INTEL_PATTERNS, read_warnings)

    docs: set[str] = set(expected_docs) | set(source_index)
    pages_by_doc: dict[str, list[dict[str, str]]] = defaultdict(list)
    failed_by_doc: dict[str, list[dict[str, str]]] = defaultdict(list)
    statuses_by_doc: dict[str, list[dict[str, str]]] = defaultdict(list)
    intel_by_doc: dict[str, list[dict[str, str]]] = defaultdict(list)

    for row in pages:
        doc = (row.get("doc_no") or "").strip()
        if doc:
            docs.add(doc)
            pages_by_doc[doc].append(row)
    for row in failed:
        doc = (row.get("doc_no") or "").strip()
        if doc:
            docs.add(doc)
            failed_by_doc[doc].append(row)
    for row in statuses:
        doc = (row.get("doc_no") or "").strip()
        if doc:
            docs.add(doc)
            statuses_by_doc[doc].append(row)
    for row in intel:
        doc = (row.get("doc_no") or "").strip()
        if doc:
            docs.add(doc)
            intel_by_doc[doc].append(row)

    coverage_rows: list[dict[str, object]] = []
    field_rows: list[dict[str, object]] = []
    retry_all: list[str] = []
    retry_capped: list[str] = []
    retry_failed_pages: list[str] = []
    retry_ocr: list[str] = []

    summary_status_counts: Counter[str] = Counter()
    retry_reason_counts: Counter[str] = Counter()
    field_presence_counts: Counter[str] = Counter()
    signal_counts: Counter[str] = Counter()

    for doc in sorted(docs, key=doc_sort_key):
        doc_pages = pages_by_doc.get(doc, [])
        doc_failed = failed_by_doc.get(doc, [])
        status_row = latest_by_time(statuses_by_doc.get(doc, []), "finished_at_utc") or {}
        intel_row = latest_by_time(intel_by_doc.get(doc, []), "") or {}

        ok_pages = {
            parse_int(row.get("page"))
            for row in doc_pages
            if (row.get("status") or "").strip() in OK_PAGE_STATUSES and parse_int(row.get("page")) > 0
        }
        page_status_counts = Counter((row.get("status") or "").strip() for row in doc_pages)
        failed_status_counts = Counter((row.get("status") or "").strip() for row in doc_failed)
        latest_status = (status_row.get("status") or "").strip()
        summary_status_counts[latest_status or "missing_doc_status"] += 1

        pages_ok_count = len(ok_pages)
        highest_ok_page = max(ok_pages) if ok_pages else 0
        last_page_checked = max(parse_int(status_row.get("last_page_checked")), highest_ok_page)
        ocr_status = (intel_row.get("ocr_status") or "").strip()
        ocr_chars = parse_int(intel_row.get("ocr_chars"))

        reasons: list[str] = []
        if not latest_status:
            reasons.append("missing_doc_status")
        elif latest_status not in COMPLETE_DOC_STATUSES:
            reasons.append(latest_status)
        non_cap_failures = [
            row for row in doc_failed
            if (row.get("status") or "").strip() and (row.get("status") or "").strip() != "max_pages_reached"
        ]
        if non_cap_failures:
            reasons.append("failed_pages")
        if not intel_row:
            reasons.append("missing_deed_body_intelligence")
        elif not ocr_status.startswith("ok"):
            reasons.append("ocr_error")
        elif ocr_chars < LOW_OCR_CHAR_THRESHOLD and latest_status != "no_pages":
            reasons.append("ocr_low_text")

        deduped_reasons = []
        seen_reasons = set()
        for reason in reasons:
            if reason not in seen_reasons:
                deduped_reasons.append(reason)
                seen_reasons.add(reason)
                retry_reason_counts[reason] += 1

        needs_retry = bool(deduped_reasons)
        if needs_retry:
            retry_all.append(doc)
            if latest_status not in COMPLETE_DOC_STATUSES:
                retry_capped.append(doc)
            if non_cap_failures:
                retry_failed_pages.append(doc)
            if any(reason.startswith("ocr") or reason.startswith("missing_deed") for reason in deduped_reasons):
                retry_ocr.append(doc)

        next_page = highest_ok_page + 1 if latest_status not in COMPLETE_DOC_STATUSES else ""
        coverage_rows.append({
            "doc_no": doc,
            "coverage_status": "needs_retry" if needs_retry else "complete",
            "retry_reasons": json.dumps(deduped_reasons),
            "latest_doc_status": latest_status,
            "pages_ok_count": pages_ok_count,
            "highest_ok_page": highest_ok_page,
            "last_page_checked": last_page_checked,
            "next_page_to_try": next_page,
            "page_status_counts": join_status_counts(page_status_counts),
            "failed_status_counts": join_status_counts(failed_status_counts),
            "ocr_status": ocr_status,
            "ocr_chars": ocr_chars,
            "pages_ocrd": intel_row.get("pages_ocrd", ""),
            "foreign_entity_flag": intel_row.get("foreign_entity_flag", ""),
            "mail_to_international_flag": intel_row.get("mail_to_international_flag", ""),
            "mineral_rights_signal": intel_row.get("mineral_rights_signal", ""),
            "phone_like_count": intel_row.get("phone_like_count", ""),
            "email_like_count": intel_row.get("email_like_count", ""),
            "raw_contacts_exported": 0,
        })

        field_row: dict[str, object] = {
            "doc_no": doc,
            "has_source_index_row": "true" if doc in source_index else "false",
            "has_deed_body_intelligence_row": "true" if bool(intel_row) else "false",
            "coverage_status": "needs_retry" if needs_retry else "complete",
            "retry_reasons": json.dumps(deduped_reasons),
        }
        for field in INDEX_FIELDS + BODY_FIELDS + FLAG_FIELDS:
            present = has_value(intel_row.get(field))
            field_row[f"has_{field}"] = "true" if present else "false"
            if present:
                field_presence_counts[field] += 1
        for flag in FLAG_FIELDS:
            if (intel_row.get(flag) or "").strip().lower() == "true":
                signal_counts[flag] += 1
        field_rows.append(field_row)

    coverage_fields = [
        "doc_no", "coverage_status", "retry_reasons", "latest_doc_status",
        "pages_ok_count", "highest_ok_page", "last_page_checked", "next_page_to_try",
        "page_status_counts", "failed_status_counts", "ocr_status", "ocr_chars",
        "pages_ocrd", "foreign_entity_flag", "mail_to_international_flag",
        "mineral_rights_signal", "phone_like_count", "email_like_count",
        "raw_contacts_exported",
    ]
    field_fields = [
        "doc_no", "has_source_index_row", "has_deed_body_intelligence_row",
        "coverage_status", "retry_reasons",
    ] + [f"has_{field}" for field in INDEX_FIELDS + BODY_FIELDS + FLAG_FIELDS]

    write_csv(out_dir / "doc_page_coverage_matrix.csv", coverage_rows, coverage_fields)
    write_csv(out_dir / "field_presence_matrix.csv", field_rows, field_fields)
    write_doclist(out_dir / "retry_doclist_all.txt", retry_all)
    write_doclist(out_dir / "retry_doclist_capped.txt", retry_capped)
    write_doclist(out_dir / "retry_doclist_failed_pages.txt", retry_failed_pages)
    write_doclist(out_dir / "retry_doclist_ocr.txt", retry_ocr)

    summary: dict[str, object] = {
        "root": str(root),
        "out_dir": str(out_dir),
        "expected_doclist": str(expected_doclist) if expected_doclist else "",
        "source_index_csv": str(source_index_csv) if source_index_csv else "",
        "expected_docs": len(expected_docs),
        "docs_seen": len(docs),
        "pages_manifest_rows": len(pages),
        "failed_page_rows": len(failed),
        "doc_status_rows": len(statuses),
        "deed_body_intelligence_rows": len(intel),
        "complete_docs": sum(1 for row in coverage_rows if row["coverage_status"] == "complete"),
        "retry_docs": len(retry_all),
        "retry_capped_docs": len(retry_capped),
        "retry_failed_page_docs": len(retry_failed_pages),
        "retry_ocr_docs": len(retry_ocr),
        "doc_status_counts": dict(summary_status_counts),
        "retry_reason_counts": dict(retry_reason_counts),
        "field_presence_counts": dict(field_presence_counts),
        "signal_counts": dict(signal_counts),
        "raw_contacts_exported": 0,
        "input_read_warnings": read_warnings,
        "audit_trust": "partial_input_read_warnings" if read_warnings else "complete_input_reads",
        "outputs": {
            "doc_page_coverage_matrix_csv": str(out_dir / "doc_page_coverage_matrix.csv"),
            "field_presence_matrix_csv": str(out_dir / "field_presence_matrix.csv"),
            "retry_doclist_all_txt": str(out_dir / "retry_doclist_all.txt"),
            "retry_doclist_capped_txt": str(out_dir / "retry_doclist_capped.txt"),
            "retry_doclist_failed_pages_txt": str(out_dir / "retry_doclist_failed_pages.txt"),
            "retry_doclist_ocr_txt": str(out_dir / "retry_doclist_ocr.txt"),
        },
    }
    (out_dir / "audit_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", help="Merged output dir or raw artifact dir to audit")
    parser.add_argument("out_dir", help="Directory for coverage matrices and retry doclists")
    parser.add_argument("--expected-doclist")
    parser.add_argument("--source-index-csv")
    parser.add_argument("--fail-on-incomplete", action="store_true")
    args = parser.parse_args()

    summary = audit(
        Path(args.root),
        Path(args.out_dir),
        Path(args.expected_doclist) if args.expected_doclist else None,
        Path(args.source_index_csv) if args.source_index_csv else None,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    if args.fail_on_incomplete and int(summary["retry_docs"]) > 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
