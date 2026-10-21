#!/usr/bin/env python3
"""rabbit_pages.py DOCLIST OUTDIR: Rabbit's NETR page puller (Kai 2026-10-16; Nora's ANALYSIS.md §4 and §8).

Each page is asked once, through the NETR Worker, 5-7 s apart. A document stops at its first page that isn't a page:
past the last page NETR serves the last page again (its PNG's Thumb::URI names the TIFF page it came from, ".tif[k]",
and k stops following the page asked), and an end answer (Worker upstream 500) or a byte repeat ends it too; a 0-byte
PNG is NETR's "no scan yet" and ends it as blank. A 200 with no page image that names no wall is that document's own
state (no_image_200), until more than NO_IMAGE_TRIP in one shard. Nothing that answered is asked again. A 403, a 429,
a 200 naming a Cloudflare challenge, a denial or a rate limit, or any other answer stops the whole shard, and every
document after it is not asked. A page is known by its bytes: PNG, JPEG, WebP, GIF, TIFF or PDF. OUTDIR gets
<doc>_<page>.<its extension>, status.csv (doc_no, state, pages, requests) and, for each answer that was not a page,
<doc>_<page>.body: its Content-Type and first HEAD_BYTES bytes.
"""
import csv
import hashlib
import os
import random
import re
import struct
import sys
import time
import urllib.error
import urllib.request
import zlib
from pathlib import Path

WORKER = "https://netr-thumb.kaiescobar09.workers.dev"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
# A PNG page under MIN_PNG is a cut-off head; other formats are smaller (a white JPEG page is about 1.9 KB), and a JPEG
# must also end with its end marker, since a progressive JPEG's head alone still opens as a blurry page.
MAX_PAGES, MIN_PNG, MIN_OTHER = 9, 5000, 1000
NO_IMAGE_TRIP, HEAD_BYTES = 3, 2048
JPEG = b"\xff\xd8\xff"   # NETR serves grey JPEG pages since 2026-10-21, PNG before
MAGIC = {b"\x89PNG": "png", JPEG: "jpg", b"GIF8": "gif", b"II*\0": "tif", b"MM\0*": "tif", b"%PDF-": "pdf"}
EXTENSIONS = ("png", "jpg", "webp", "gif", "tif", "pdf")
WALL = re.compile(rb"cloudflare|cf[-_]ray|cf_chl|challenge|captcha|just a moment|attention required|access denied"
                  rb"|forbidden|blocked|unauthori[sz]ed|rate[ -_]?limit|too many", re.I)


def extension(body: bytes) -> str | None:
    """The page file extension its first bytes name, or None if it is not a page image."""
    if body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return "webp"
    return next((ext for magic, ext in MAGIC.items() if body.startswith(magic)), None)


def whole(body: bytes) -> bool:
    ext = extension(body)
    return bool(ext) and len(body) >= (MIN_PNG if ext == "png" else MIN_OTHER) and (
        ext != "jpg" or body.endswith(b"\xff\xd9"))


def served(png: bytes) -> int | None:
    """The TIFF page index NETR rendered this PNG from (Thumb::URI ".../<doc>.tif[k]"), or None if it doesn't say."""
    at = 8 if png[:4] == b"\x89PNG" else len(png)
    while at + 8 <= len(png):
        size, kind = struct.unpack(">I4s", png[at:at + 8])
        data = png[at + 8:at + 8 + size]
        if kind in (b"tEXt", b"zTXt") and data.startswith(b"Thumb::URI\0"):
            text = data[11:] if kind == b"tEXt" else zlib.decompress(data[12:])
            m = re.search(rb"\.tif\[(\d+)\]$", text)
            return int(m[1]) if m else None
        at += 12 + size
    return None


def ask(key: str, doc: str, page: int) -> tuple[str, bytes, str]:
    """(answer, body, Content-Type)."""
    req = urllib.request.Request(f"{WORKER}/thumb/{doc}/{page}", headers={"X-Auth": key, "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            upstream, body = resp.headers.get("X-Upstream-Status", str(resp.status)), resp.read()
            kind = resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        upstream, body, kind = e.headers.get("X-Upstream-Status", str(e.code)), e.read(), e.headers.get("Content-Type", "")
        if "X-Upstream-Status" not in e.headers and e.code == 500:  # the Worker's own 500, not NETR's end of document
            return "error_500", body, kind
    except OSError as e:
        return f"error_{type(e).__name__}", b"", ""
    if whole(body):
        return "page", body, kind
    if upstream == "500":
        return "end", body, kind
    if upstream == "200" and kind.split(";")[0].strip().lower().startswith("image/") and not body:
        return "blank", body, kind
    if upstream == "200" and not WALL.search(body):
        return "no_image_200", body, kind
    return f"http_{upstream}", body, kind


def main() -> int:
    doclist, out = Path(sys.argv[1]), Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    key = os.environ["NETR_PROXY_KEY"]
    docs = [d for d in doclist.read_text().split() if d.strip()]
    stopped, odd = None, 0
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
                answer, body, kind = ask(key, doc, page)
                asked += 1
                sha = hashlib.sha256(body).hexdigest()
                if answer == "page" and sha not in seen and served(body) in (page - 1, None):
                    seen.add(sha)
                    (out / f"{doc}_{page}.{extension(body)}").write_bytes(body)
                    continue
                if answer not in ("page", "end", "blank"):
                    head = body.replace(key.encode(), b"[key]")[:HEAD_BYTES]
                    (out / f"{doc}_{page}.body").write_bytes(f"Content-Type: {kind}\n\n".encode() + head)
                if answer in ("page", "end"):
                    state = "done" if page > 1 else "no_pages"
                elif answer == "blank":
                    state = f"blank_page_{page}"
                elif answer == "no_image_200" and (odd := odd + 1) <= NO_IMAGE_TRIP:
                    state = f"no_image_200 on page {page}"
                elif answer == "no_image_200":
                    state = stopped = f"http_200 on page {page}: {odd} documents in this shard got a 200 with no page image"
                else:
                    state = stopped = f"{answer} on page {page}"
                break
            w.writerow([doc, state, len(seen) if state == "done" else 0, asked])
            fh.flush()
            print(doc, state, asked, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
