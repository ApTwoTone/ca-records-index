#!/usr/bin/env python3
"""rabbit_watch.py DOC...: one look at NETR's LA index for Rabbit's watcher (netr_read/watch.py on the Spark).

GET /losangeles once for its "Database current to" date, then POST /lasearch once per number, no retry
(la_county_index.fetch with RETRIES 0). A wall or an answer that is neither a row nor "no documents" stops the look
there. Prints JSON: banner, status, wall, docs (doc_no, ok, county_type, record_date, reason), requests.
"""
import json
import re
import sys

import la_county_index as idx

WALLS = ("too many searches", "please wait a moment", "unusual activity", "unusual traffic", "access denied",
         "you have been blocked")


def look(docs: list[str]) -> dict:
    out = {"banner": None, "status": None, "wall": None, "docs": [], "requests": 1}
    session = idx.creq.Session(impersonate=idx.IMPERSONATE)
    try:
        page = session.get(idx.FORM_URL, timeout=idx.TIMEOUT)
    except Exception as e:
        out["wall"] = f"/losangeles: {type(e).__name__}"
        return out
    out["status"], text = page.status_code, page.text
    if m := re.search(r"Database current to\s*<strong>\s*(\d{4}-\d{2}-\d{2})\s*</strong>", text, re.I):
        out["banner"] = m[1]
    if page.status_code != 200 or any(w in text.lower() for w in WALLS):
        out["wall"] = f"/losangeles: http {page.status_code}"
        return out
    idx._SESSION, idx.RETRIES = session, 0
    for doc in docs:
        got = idx.fetch(doc, save_evidence=False)
        out["requests"] += 1
        out["docs"].append({k: got[k] for k in ("doc_no", "ok", "county_type", "record_date", "reason")})
        if not got["ok"] and got["reason"] != "not_found":
            out["wall"] = f"{doc}: {got['reason']}"
            break
    return out


def main(argv: list[str]) -> int:
    if not argv or not all(re.fullmatch(r"\d{11}", d) for d in argv):
        sys.exit(f"not LA document numbers: {argv}")
    print(json.dumps(look(argv)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
