#!/usr/bin/env python3
"""rabbit_pages.py DOCLIST OUTDIR: Rabbit's NETR page puller (Kai 2026-10-16; Nora's ANALYSIS.md §4 and §8).

Each page is asked once, through the NETR Worker, 5-7 s apart. A document stops at its first page that isn't a page:
NETR's end answer (Worker upstream 500) or a repeat of an earlier page ends it; a 0-byte PNG is NETR's "no scan yet"
and ends it as blank. Nothing that answered is asked again. A 403, a 429 or any other answer stops the whole shard,
and every document after it is not asked. OUTDIR gets <doc>_<page>.png and status.csv: doc_no, state, pages, requests.
"""
import csv
import hashlib
import os
import random
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

WORKER = "https://netr-thumb.kaiescobar09.workers.dev"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
MAX_PAGES, MIN_PNG = 9, 5000


def ask(key: str, doc: str, page: int) -> tuple[str, bytes]:
    req = urllib.request.Request(f"{WORKER}/thumb/{doc}/{page}", headers={"X-Auth": key, "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            upstream, body = resp.headers.get("X-Upstream-Status", str(resp.status)), resp.read()
            kind = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
    except urllib.error.HTTPError as e:
        upstream, body, kind = str(e.code), e.read(), ""
    except OSError as e:
        return f"error_{type(e).__name__}", b""
    if body[:4] == b"\x89PNG" and len(body) >= MIN_PNG:
        return "page", body
    if upstream == "500":
        return "end", body
    if upstream == "200" and kind == "image/png" and not body:
        return "blank", body
    return f"http_{upstream}", body


def main() -> int:
    doclist, out = Path(sys.argv[1]), Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    key = os.environ["NETR_PROXY_KEY"]
    docs = [d for d in doclist.read_text().split() if d.strip()]
    stopped = None
    with (out / "status.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["doc_no", "state", "pages", "requests"])
        for doc in docs:
            if stopped:
                w.writerow([doc, f"not_asked: {stopped}", 0, 0])
                continue
            seen, state, asked = set(), f"over_{MAX_PAGES - 1}_pages", 0
            for page in range(1, MAX_PAGES + 1):
                time.sleep(random.uniform(5.0, 7.0))
                answer, body = ask(key, doc, page)
                asked += 1
                sha = hashlib.sha256(body).hexdigest()
                if answer == "page" and sha not in seen:
                    seen.add(sha)
                    (out / f"{doc}_{page}.png").write_bytes(body)
                    continue
                if answer in ("page", "end"):
                    state = "done" if page > 1 else "no_pages"
                elif answer == "blank":
                    state = f"blank_page_{page}"
                else:
                    state = stopped = f"{answer} on page {page}"
                break
            w.writerow([doc, state, len(seen) if state == "done" else 0, asked])
            fh.flush()
            print(doc, state, asked, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
