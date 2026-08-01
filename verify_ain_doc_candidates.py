#!/usr/bin/env python3
"""Fail-closed NETR AIN -> expected-document reverse proof.

Each process is intentionally single-threaded and is meant to run on one
GitHub-hosted runner/IP. HTTP/rate-wall failures remain unresolved and can
never become NOT_FOUND.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import random
import re
import time
from pathlib import Path

import la_county_index as idx


THROTTLE_MARKERS = ("too many searches", "please wait a moment")


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_ain(value: str) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) != 10:
        raise ValueError(f"AIN must contain exactly 10 digits: {value!r}")
    return digits


def load_candidates(path: Path) -> list[dict[str, str]]:
    rows = []
    seen = set()
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix.lower() == ".csv":
        parsed = csv.DictReader(text.splitlines())
    else:
        parsed = (json.loads(line) for line in text.splitlines() if line.strip())
    for row in parsed:
        ain = normalize_ain(row.get("ain") or row.get("apn") or row.get("apn_norm"))
        doc = re.sub(r"\D", "", str(row.get("doc_no") or ""))
        if not doc:
            raise ValueError("candidate is missing doc_no")
        key = (ain, doc)
        if key in seen:
            continue
        seen.add(key)
        rows.append({"ain": ain, "doc_no": doc})
    return rows


def parse_rows(results_html: str) -> tuple[list[dict], int]:
    rows, skipped = [], 0
    for match in idx._ROW_RE.finditer(results_html or ""):
        cells = idx._CELL_RE.findall(match.group(1))
        if len(cells) < 5:
            skipped += 1
            continue
        doc = re.sub(r"\D", "", idx._clean(cells[0]))
        date_match = idx._DATE_RE.search(idx._clean(cells[1]))
        rows.append({
            "doc_no": doc,
            "record_date": date_match.group(0) if date_match else None,
            "county_type": idx._clean(re.split(r"<a\b", cells[2], flags=re.I)[0]) or None,
        })
    return rows, skipped


def search_page(session, ain: str, page: int, evidence_dir: Path, candidate_key: str,
                attempt: int) -> dict:
    data = {
        "page": str(page), "g-recaptcha-response": "", "beg_dt": idx.EARLIEST,
        "end_dt": dt.date.today().isoformat(), "company": "", "first_name": "",
        "last_name": "", "signer": "R", "ain": ain, "doc_no": "",
    }
    headers = {
        "Referer": idx.FORM_URL, "Origin": idx.BASE,
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
    }
    fetched_at = now()
    try:
        response = session.post(idx.SEARCH_URL, data=data, headers=headers, timeout=idx.TIMEOUT)
    except Exception as exc:
        return {"outcome": "NETWORK_ERROR", "detail": type(exc).__name__, "fetched_at": fetched_at}
    body = response.text or ""
    body_bytes = body.encode("utf-8", errors="replace")
    receipt = {
        "http_status": int(response.status_code), "response_bytes": len(body_bytes),
        "body_sha256": hashlib.sha256(body_bytes).hexdigest(), "fetched_at": fetched_at,
    }
    raw_path = evidence_dir / f"{candidate_key}_a{attempt}_p{page}.json"
    raw_path.write_bytes(body_bytes)
    receipt["evidence_file"] = raw_path.name
    if response.status_code != 200:
        receipt.update(outcome="HTTP_ERROR", detail=f"http_{response.status_code}")
        return receipt
    try:
        payload = json.loads(body)
    except Exception:
        receipt.update(outcome="NON_JSON", detail="valid HTTP 200 but response was not JSON")
        return receipt
    count_html = payload[0] if isinstance(payload, list) and payload else ""
    results_html = payload[1] if isinstance(payload, list) and len(payload) > 1 else ""
    count_text = idx._clean(count_html)
    if any(marker in count_text.lower() for marker in THROTTLE_MARKERS):
        receipt.update(outcome="THROTTLED", detail=count_text[:160])
        return receipt
    rows, skipped = parse_rows(results_html)
    total_match = re.search(r"([\d,]+)\s+document", count_text)
    total = int(total_match.group(1).replace(",", "")) if total_match else None
    no_documents = "no documents" in count_text.lower()
    receipt.update(
        outcome="VALID_RESPONSE", rows=rows, parsed_rows=len(rows), parse_skipped=skipped,
        total=0 if no_documents else total,
        capped="only the most recent" in count_text.lower(),
    )
    return receipt


def prove_candidate(session, candidate: dict[str, str], evidence_dir: Path, attempts: int,
                    delay_min: float, delay_max: float, max_pages: int) -> dict:
    ain, expected = candidate["ain"], candidate["doc_no"]
    candidate_key = hashlib.sha256(f"{ain}|{expected}".encode()).hexdigest()[:20]
    request_receipts, chain_rows = [], []
    final_outcome, detail = "UNRESOLVED", "no valid response"
    for attempt in range(1, attempts + 1):
        page = 1
        chain_rows = []
        while page <= max_pages:
            if request_receipts:
                time.sleep(random.uniform(delay_min, delay_max))
            receipt = search_page(session, ain, page, evidence_dir, candidate_key, attempt)
            receipt.update(attempt=attempt, page=page)
            request_receipts.append(receipt)
            if receipt["outcome"] != "VALID_RESPONSE":
                final_outcome, detail = receipt["outcome"], receipt.get("detail")
                break
            chain_rows.extend(receipt["rows"])
            if any(row["doc_no"] == expected for row in chain_rows):
                final_outcome, detail = "PROVEN", "expected doc_no appears in AIN result chain"
                break
            total = receipt.get("total")
            if total == 0:
                final_outcome, detail = "NOT_FOUND", "valid response explicitly reported no documents for AIN"
                break
            if not receipt.get("capped") or (total is not None and len(chain_rows) >= total):
                final_outcome = "EXPECTED_DOC_NOT_IN_CHAIN"
                detail = "valid terminal chain did not contain expected doc_no"
                break
            page += 1
        else:
            final_outcome, detail = "PAGE_CAP_UNRESOLVED", f"reached max_pages={max_pages}"
        if final_outcome in {"PROVEN", "NOT_FOUND", "EXPECTED_DOC_NOT_IN_CHAIN"}:
            break
        if attempt < attempts:
            time.sleep(min(20.0, 3.0 * attempt) + random.uniform(0.5, 1.5))
    matched = next((row for row in chain_rows if row["doc_no"] == expected), None)
    return {
        "schema_version": 1, "candidate_key": candidate_key, "ain": ain,
        "expected_doc_no": expected, "outcome": final_outcome, "detail": detail,
        "proved": final_outcome == "PROVEN", "matched_row": matched,
        "chain_doc_nos": [row["doc_no"] for row in chain_rows],
        "request_receipts": request_receipts, "finished_at": now(),
        "research_only": True, "callable_now": "NO",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("candidate_file")
    parser.add_argument("out_dir")
    parser.add_argument("--attempts", type=int, default=4)
    parser.add_argument("--delay-min", type=float, default=3.0)
    parser.add_argument("--delay-max", type=float, default=4.0)
    parser.add_argument("--max-pages", type=int, default=25)
    args = parser.parse_args()
    if args.attempts < 1 or args.max_pages < 1 or args.delay_min < 0 or args.delay_max < args.delay_min:
        raise SystemExit("invalid bounded retry/pacing arguments")
    candidates = load_candidates(Path(args.candidate_file))
    if not candidates:
        raise SystemExit("candidate list is empty")
    out = Path(args.out_dir); evidence = out / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    session = idx._session()
    results = [prove_candidate(session, row, evidence, args.attempts,
                               args.delay_min, args.delay_max, args.max_pages)
               for row in candidates]
    with (out / "ain_doc_reverse_proof.jsonl").open("w", encoding="utf-8") as fh:
        for row in results:
            fh.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
    with (out / "ain_doc_reverse_proof.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["ain", "expected_doc_no", "outcome", "proved", "detail", "candidate_key"])
        writer.writeheader()
        for row in results:
            writer.writerow({key: row[key] for key in writer.fieldnames})
    counts = {}
    for row in results:
        counts[row["outcome"]] = counts.get(row["outcome"], 0) + 1
    summary = {
        "schema_version": 1, "generated_at": now(), "candidates": len(results),
        "outcome_counts": dict(sorted(counts.items())),
        "complete": all(row["outcome"] == "PROVEN" for row in results),
        "research_only": True, "callable_now": "NO",
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary, sort_keys=True))
    return 0 if summary["complete"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
