#!/usr/bin/env python3
"""rabbit_ain.py PAIRLIST OUTDIR: prove an LA document's parcel for Rabbit's netr_read (Kai 2026-10-21).

PAIRLIST holds whitespace-separated AIN:DOCNO pairs. For each pair NETR's LA index is searched by AIN (POST /lasearch,
10 rows a page) and that parcel's recorder history is walked page by page until the document shows up. One request
clock for the whole process, 2.9-3.5 s between any two POSTs. Each POST is sent once; only NETR's soft wall (an HTTP
200 "too many searches" / "please wait a moment") is re-asked, after 6, 10 and 14 s. States, one per pair:
  proven            the document is in the parcel's history (the walk stops there)
  not_in_chain      every page up to the stated total was read and the document is not in it
  no_docs           page 1 says no documents
  incomplete: why   a later page says no documents, a page only repeats rows, the total changes, a page is short
                    before the total, a page states no total, or MAX_PAGES
  walled            still walled after 3 re-asks
  error_<x>         a network error, a non-200 other than 403/429, or a non-JSON answer
  http_403/http_429 NETR refused: the shard stops
  not_asked: why    the shard stopped earlier (first 403/429, or MAX_WALLED walled pairs); nothing was sent
OUTDIR gets status.csv (ain, doc_no, state, pages, requests) and nothing else: no bodies, no names (public repo).
"""
import csv
import datetime
import json
import re
import sys
import time
from pathlib import Path

import la_county_index as idx
from harvest_index_shard import THROTTLE_MARKERS, _pace_request

ROWS, MAX_PAGES, MAX_WALLED, WALL_RETRIES = 10, 40, 3, 3
PAIR = re.compile(r"(\d{10}):(2\d{10})")
HEADERS = {"Referer": idx.FORM_URL, "Origin": idx.BASE, "X-Requested-With": "XMLHttpRequest",
           "Accept": "application/json, text/javascript, */*; q=0.01"}


def ask(session, ain: str, page: int) -> tuple[str, int | None, list[str]]:
    """(answer, stated total, doc numbers on this page); answer is rows, no_docs, wall, http_403/429 or error_<x>."""
    data = {"page": str(page), "g-recaptcha-response": "", "beg_dt": idx.EARLIEST,
            "end_dt": datetime.date.today().isoformat(), "company": "", "first_name": "", "last_name": "",
            "signer": "R", "ain": ain, "doc_no": ""}
    _pace_request()
    try:
        r = session.post(idx.SEARCH_URL, data=data, headers=HEADERS, timeout=idx.TIMEOUT)
    except Exception as e:
        return f"error_{type(e).__name__}", None, []
    if r.status_code in (403, 429):
        return f"http_{r.status_code}", None, []
    if r.status_code != 200:
        return f"error_http_{r.status_code}", None, []
    try:
        j = json.loads(r.text)
        count, results = idx._clean(j[0]).lower(), j[1] if len(j) > 1 else ""
    except Exception:
        return "error_non_json", None, []
    if any(m in count or m in idx._clean(results).lower() for m in THROTTLE_MARKERS):
        return "wall", None, []
    if "no documents" in count:
        return "no_docs", 0, []
    total = re.search(r"(\d[\d,]*) documents?", count)
    docs = [re.sub(r"\D", "", idx._clean(cells[0])) for m in idx._ROW_RE.finditer(results)
            if len(cells := idx._CELL_RE.findall(m[1])) >= 5]
    return "rows", int(total[1].replace(",", "")) if total else None, docs


def walk(session, ain: str, doc: str) -> tuple[str, int, int]:
    """(state, pages read, requests) for one pair."""
    seen, total, asked = set(), None, 0
    for page in range(1, MAX_PAGES + 1):
        for attempt in range(WALL_RETRIES + 1):
            if attempt:
                time.sleep(6 + 4 * (attempt - 1))
            answer, count, docs = ask(session, ain, page)
            asked += 1
            if answer != "wall":
                break
        if answer == "wall":
            return "walled", page - 1, asked
        if answer == "no_docs":
            return ("no_docs" if page == 1 else f"incomplete: page {page} says no documents"), page - 1, asked
        if answer != "rows":
            return answer, page - 1, asked
        if count is None:
            return f"incomplete: page {page} states no total", page, asked
        if total is not None and count != total:
            return f"incomplete: the total changed from {total} to {count} on page {page}", page, asked
        total = count
        if doc in docs:
            return "proven", page, asked
        if not set(docs) - seen:
            return f"incomplete: page {page} only repeats rows", page, asked
        seen.update(docs)
        if len(seen) >= total:
            return "not_in_chain", page, asked
        if len(docs) < ROWS:
            return f"incomplete: page {page} is short ({len(seen)} of {total} rows)", page, asked
    return f"incomplete: {MAX_PAGES}-page cap ({len(seen)} of {total} rows)", MAX_PAGES, asked


def main() -> int:
    words, out = Path(sys.argv[1]).read_text().split(), Path(sys.argv[2])
    if bad := [w for w in words if not PAIR.fullmatch(w)]:
        sys.exit(f"not AIN:DOCNO pairs: {bad[:5]}")
    out.mkdir(parents=True, exist_ok=True)
    session = idx._session()
    stopped, walled = None, 0
    with (out / "status.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ain", "doc_no", "state", "pages", "requests"])
        for ain, doc in (PAIR.fullmatch(p).groups() for p in dict.fromkeys(words)):
            if stopped:
                w.writerow([ain, doc, f"not_asked: {stopped}", 0, 0])
                continue
            state, pages, asked = walk(session, ain, doc)
            if state in ("http_403", "http_429"):
                stopped = f"{state} on {ain}:{doc}"
            elif state == "walled" and (walled := walled + 1) >= MAX_WALLED:
                stopped = f"{walled} pairs walled, the last {ain}:{doc}"
            w.writerow([ain, doc, state, pages, asked])
            fh.flush()
            print(ain, doc, state, pages, asked, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
