#!/usr/bin/env python3
"""Pull all available PNG pages for a doc# list via the NETR Worker.

The script is deliberately resumable:
- Existing valid PNGs are skipped.
- Every requested/fetched page is written to pages_manifest.csv.
- Failed pages are written to failed_pages.csv for an explicit re-sweep.
- HTTP 500 from the upstream thumb endpoint is treated as end-of-document.
"""
from __future__ import annotations

import argparse
import csv
import os
import random
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path


WORKER = "https://netr-thumb.kaiescobar09.workers.dev"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
CTX = ssl.create_default_context()


def read_key() -> str:
    env_key = os.environ.get("NETR_PROXY_KEY", "").strip()
    if env_key:
        return env_key
    key_path = Path("/tmp/netr_proxy_key")
    if key_path.exists():
        return key_path.read_text(encoding="utf-8").strip()
    raise SystemExit("missing NETR_PROXY_KEY or /tmp/netr_proxy_key")


def is_valid_png(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            return fh.read(4) == b"\x89PNG" and path.stat().st_size > 5000
    except Exception:
        return False


def fetch_page(key: str, doc_no: str, page: int, attempts: int) -> tuple[str, bytes | None, str]:
    url = f"{WORKER}/thumb/{doc_no}/{page}"
    for attempt in range(1, attempts + 1):
        try:
            req = urllib.request.Request(url, headers={"X-Auth": key, "User-Agent": UA})
            with urllib.request.urlopen(req, timeout=45, context=CTX) as resp:
                upstream = resp.headers.get("X-Upstream-Status", str(resp.status))
                body = resp.read()
            if body[:4] == b"\x89PNG" and len(body) > 5000:
                return "ok", body, upstream
            if upstream == "500":
                return "end", None, upstream
            if upstream in {"403", "429", "502", "503", "504"}:
                time.sleep(min(12.0, 1.5 * attempt) + random.uniform(0, 1.5))
                continue
            return f"bad_payload_{upstream}", None, upstream
        except urllib.error.HTTPError as exc:
            if exc.code == 500:
                return "end", None, str(exc.code)
            if exc.code in {403, 429, 502, 503, 504}:
                time.sleep(min(12.0, 1.5 * attempt) + random.uniform(0, 1.5))
                continue
            return f"http_{exc.code}", None, str(exc.code)
        except Exception as exc:
            if attempt < attempts:
                time.sleep(min(12.0, 1.2 * attempt) + random.uniform(0, 1.0))
                continue
            return f"error_{type(exc).__name__}", None, ""
    return "retry_out", None, ""


def read_docs(path: Path) -> list[str]:
    docs = []
    seen = set()
    for raw in path.read_text(encoding="utf-8").replace(",", "\n").splitlines():
        doc = raw.strip()
        if not doc or doc in seen:
            continue
        seen.add(doc)
        docs.append(doc)
    return docs


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("doclist")
    parser.add_argument("outdir")
    parser.add_argument("--max-pages", type=int, default=25)
    parser.add_argument("--attempts", type=int, default=6)
    parser.add_argument("--delay-min", type=float, default=5.0)
    parser.add_argument("--delay-max", type=float, default=7.0)
    args = parser.parse_args()

    key = read_key()
    doclist = Path(args.doclist)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    docs = read_docs(doclist)

    manifest_path = outdir / "pages_manifest.csv"
    failed_path = outdir / "failed_pages.csv"
    doc_status_path = outdir / "doc_status.csv"
    manifest_exists = manifest_path.exists()
    failed_exists = failed_path.exists()
    status_exists = doc_status_path.exists()

    with manifest_path.open("a", newline="", encoding="utf-8") as mf, \
            failed_path.open("a", newline="", encoding="utf-8") as ff, \
            doc_status_path.open("a", newline="", encoding="utf-8") as sf:
        mw = csv.writer(mf)
        fw = csv.writer(ff)
        sw = csv.writer(sf)
        if not manifest_exists:
            mw.writerow(["doc_no", "page", "status", "upstream_status", "path", "bytes"])
        if not failed_exists:
            fw.writerow(["doc_no", "page", "status", "upstream_status"])
        if not status_exists:
            sw.writerow(["doc_no", "status", "pages_ok", "last_page_checked"])

        total_pages = 0
        failed_pages = 0
        for i, doc_no in enumerate(docs, 1):
            pages_ok = 0
            doc_status = "max_pages_reached"
            last_page = 0
            for page in range(1, args.max_pages + 1):
                last_page = page
                out = outdir / f"{doc_no}_{page}.png"
                if is_valid_png(out):
                    size = out.stat().st_size
                    mw.writerow([doc_no, page, "ok_existing", "", str(out.name), size])
                    pages_ok += 1
                    total_pages += 1
                    continue

                status, body, upstream = fetch_page(key, doc_no, page, args.attempts)
                if status == "ok" and body:
                    out.write_bytes(body)
                    mw.writerow([doc_no, page, "ok", upstream, str(out.name), len(body)])
                    pages_ok += 1
                    total_pages += 1
                elif status == "end":
                    mw.writerow([doc_no, page, "end", upstream, "", 0])
                    doc_status = "done"
                    break
                else:
                    mw.writerow([doc_no, page, status, upstream, "", 0])
                    fw.writerow([doc_no, page, status, upstream])
                    failed_pages += 1

                mf.flush()
                ff.flush()
                if page < args.max_pages:
                    time.sleep(random.uniform(args.delay_min, args.delay_max))

            if pages_ok == 0 and doc_status == "done":
                doc_status = "no_pages"
            sw.writerow([doc_no, doc_status, pages_ok, last_page])
            sf.flush()
            if i % 10 == 0 or i == len(docs):
                print(f"pulled {i}/{len(docs)} docs, ok_pages={total_pages}, failed_pages={failed_pages}", flush=True)

    print(f"DONE docs={len(docs)} ok_pages={total_pages} failed_pages={failed_pages} -> {outdir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
