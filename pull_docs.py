#!/usr/bin/env python3
"""Pull all available PNG pages for a doc# list via the NETR Worker.

The script is deliberately resumable:
- Existing valid PNGs are skipped.
- Every requested/fetched page is written to pages_manifest.csv.
- Failed pages are written to failed_pages.csv for an explicit re-sweep.
- Only an explicit upstream 500 or an exact byte/pixel repeat is accepted as
  end-of-document. Coarse perceptual similarity is diagnostic only and can
  never truncate a recorder document.
- Every terminal candidate is preserved with cryptographic and image-content
  evidence so completion can be independently validated downstream.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
import datetime
import hashlib
import io
import json
import os
import random
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image


WORKER = "https://netr-thumb.kaiescobar09.workers.dev"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
CTX = ssl.create_default_context()


def now_utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def page_fingerprint_bytes(body: bytes) -> dict[str, int | str]:
    body_sha256 = hashlib.sha256(body).hexdigest()
    try:
        with Image.open(io.BytesIO(body)) as img:
            rgb = img.convert("RGB")
            gray = rgb.convert("L")
            pixel_sha256 = hashlib.sha256(
                f"{rgb.width}x{rgb.height}:RGB:".encode("ascii") + rgb.tobytes()
            ).hexdigest()
            ink_pixels = sum(1 for pixel in gray.getdata() if pixel < 245)
            small = gray.resize((16, 16))
            pixels = list(small.getdata())
            avg = sum(pixels) / len(pixels)
            bits = 0
            for pixel in pixels:
                bits = (bits << 1) | (1 if pixel >= avg else 0)
            return {
                "body_sha256": body_sha256,
                "pixel_sha256": pixel_sha256,
                "ahash": bits,
                "width": rgb.width,
                "height": rgb.height,
                "ink_pixels": ink_pixels,
                "total_pixels": rgb.width * rgb.height,
            }
    except Exception:
        return {
            "body_sha256": body_sha256,
            "pixel_sha256": "",
            "ahash": -1,
            "width": 0,
            "height": 0,
            "ink_pixels": 0,
            "total_pixels": 0,
        }


def page_fingerprint_file(path: Path):
    return page_fingerprint_bytes(path.read_bytes())


def exact_duplicate_match(
    fingerprint: dict[str, int | str],
    seen: list[tuple[int, dict[str, int | str]]],
) -> tuple[int, dict[str, int | str], str] | None:
    """Return a prior page only when its bytes or decoded pixels are exact."""
    for old_page, old in seen:
        if fingerprint["body_sha256"] == old["body_sha256"]:
            return old_page, old, "exact_byte_duplicate"
        if (
            fingerprint["pixel_sha256"]
            and fingerprint["pixel_sha256"] == old["pixel_sha256"]
        ):
            return old_page, old, "exact_pixel_duplicate"
    return None


def nearest_ahash_match(
    fingerprint: dict[str, int | str],
    seen: list[tuple[int, dict[str, int | str]]],
) -> tuple[int, dict[str, int | str], int] | None:
    """Return nearest perceptual match for telemetry, never termination."""
    current = int(fingerprint["ahash"])
    if current < 0:
        return None
    candidates = [
        (old_page, old, (current ^ int(old["ahash"])).bit_count())
        for old_page, old in seen
        if int(old["ahash"]) >= 0
    ]
    return min(candidates, key=lambda row: row[2]) if candidates else None


def is_substantive_page(fingerprint: dict[str, int | str]) -> bool:
    """Reject effectively blank PNGs without ever calling them document end."""
    total = int(fingerprint["total_pixels"])
    ink = int(fingerprint["ink_pixels"])
    return total > 0 and ink >= max(64, int(total * 0.00002))


def fetch_page(
    key: str, doc_no: str, page: int, attempts: int
) -> tuple[str, bytes | None, str, str]:
    url = f"{WORKER}/thumb/{doc_no}/{page}"
    for attempt in range(1, attempts + 1):
        try:
            req = urllib.request.Request(url, headers={"X-Auth": key, "User-Agent": UA})
            with urllib.request.urlopen(req, timeout=45, context=CTX) as resp:
                upstream = resp.headers.get("X-Upstream-Status", str(resp.status))
                content_type = resp.headers.get("Content-Type", "")
                body = resp.read()
            if body[:4] == b"\x89PNG" and len(body) > 5000:
                return "ok", body, upstream, content_type
            if upstream == "500":
                return "end", body, upstream, content_type
            if upstream in {"403", "429", "502", "503", "504"}:
                time.sleep(min(12.0, 1.5 * attempt) + random.uniform(0, 1.5))
                continue
            return f"bad_payload_{upstream}", body, upstream, content_type
        except urllib.error.HTTPError as exc:
            content_type = exc.headers.get("Content-Type", "") if exc.headers else ""
            try:
                body = exc.read()
            except Exception:
                body = b""
            if exc.code == 500:
                return "end", body, str(exc.code), content_type
            if exc.code in {403, 429, 502, 503, 504}:
                time.sleep(min(12.0, 1.5 * attempt) + random.uniform(0, 1.5))
                continue
            return f"http_{exc.code}", body, str(exc.code), content_type
        except Exception as exc:
            if attempt < attempts:
                time.sleep(min(12.0, 1.2 * attempt) + random.uniform(0, 1.0))
                continue
            return f"error_{type(exc).__name__}", None, "", ""
    return "retry_out", None, "", ""


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


EVIDENCE_FIELDS = [
    "doc_no", "page", "event", "terminal", "upstream_status", "content_type",
    "candidate_path", "candidate_bytes", "candidate_body_sha256",
    "candidate_pixel_sha256", "candidate_ink_pixels", "candidate_total_pixels",
    "matched_page", "matched_body_sha256", "matched_pixel_sha256",
    "ahash_distance", "recorded_at_utc",
]


def preserve_candidate(
    directory: Path,
    doc_no: str,
    page: int,
    event: str,
    body: bytes | None,
    content_type: str,
) -> str:
    if body is None:
        return ""
    directory.mkdir(parents=True, exist_ok=True)
    suffix = ".png" if body[:4] == b"\x89PNG" else ".bin"
    path = directory / f"{doc_no}_{page}_{event}{suffix}"
    path.write_bytes(body)
    return str(path.relative_to(directory.parent))


def evidence_row(
    doc_no: str,
    page: int,
    event: str,
    terminal: bool,
    upstream: str,
    content_type: str,
    candidate_path: str,
    body: bytes | None,
    fingerprint: dict[str, int | str] | None = None,
    match: tuple[int, dict[str, int | str], str] | None = None,
    ahash_distance: int | str = "",
) -> dict[str, int | str]:
    fingerprint = fingerprint or {}
    matched_page, matched, _kind = match or ("", {}, "")
    return {
        "doc_no": doc_no,
        "page": page,
        "event": event,
        "terminal": "true" if terminal else "false",
        "upstream_status": upstream,
        "content_type": content_type,
        "candidate_path": candidate_path,
        "candidate_bytes": len(body or b""),
        "candidate_body_sha256": fingerprint.get("body_sha256") or (
            hashlib.sha256(body).hexdigest() if body is not None else ""
        ),
        "candidate_pixel_sha256": fingerprint.get("pixel_sha256", ""),
        "candidate_ink_pixels": fingerprint.get("ink_pixels", ""),
        "candidate_total_pixels": fingerprint.get("total_pixels", ""),
        "matched_page": matched_page,
        "matched_body_sha256": matched.get("body_sha256", ""),
        "matched_pixel_sha256": matched.get("pixel_sha256", ""),
        "ahash_distance": ahash_distance,
        "recorded_at_utc": now_utc(),
    }


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
    terminal_evidence_path = outdir / "terminal_evidence.csv"
    candidate_dir = outdir / "terminal_candidates"
    summary_path = outdir / "pages_summary.json"
    manifest_exists = manifest_path.exists()
    failed_exists = failed_path.exists()
    status_exists = doc_status_path.exists()
    evidence_exists = terminal_evidence_path.exists()
    doc_status_counts = Counter()

    with manifest_path.open("a", newline="", encoding="utf-8") as mf, \
            failed_path.open("a", newline="", encoding="utf-8") as ff, \
            doc_status_path.open("a", newline="", encoding="utf-8") as sf, \
            terminal_evidence_path.open("a", newline="", encoding="utf-8") as ef:
        mw = csv.writer(mf)
        fw = csv.writer(ff)
        sw = csv.writer(sf)
        ew = csv.DictWriter(ef, fieldnames=EVIDENCE_FIELDS)
        if not manifest_exists:
            mw.writerow(["doc_no", "page", "status", "upstream_status", "path", "bytes", "fetched_at_utc"])
        if not failed_exists:
            fw.writerow(["doc_no", "page", "status", "upstream_status", "attempted_at_utc"])
        if not status_exists:
            sw.writerow(["doc_no", "status", "pages_ok", "last_page_checked", "finished_at_utc"])
        if not evidence_exists:
            ew.writeheader()

        total_pages = 0
        failed_pages = 0
        for i, doc_no in enumerate(docs, 1):
            pages_ok = 0
            doc_status = "max_pages_reached"
            last_page = 0
            seen_pages: list[tuple[int, dict[str, int | str]]] = []
            for page in range(1, args.max_pages + 1):
                last_page = page
                out = outdir / f"{doc_no}_{page}.png"
                if is_valid_png(out):
                    fingerprint = page_fingerprint_file(out)
                    if not is_substantive_page(fingerprint):
                        ew.writerow(evidence_row(
                            doc_no, page, "blank_existing_not_terminal", False,
                            "", "image/png", str(out.name), out.read_bytes(), fingerprint,
                        ))
                        mw.writerow([doc_no, page, "blank_existing", "", "", 0, now_utc()])
                        fw.writerow([doc_no, page, "blank_existing", "", now_utc()])
                        failed_pages += 1
                        mf.flush()
                        ff.flush()
                        ef.flush()
                        continue
                    match = exact_duplicate_match(fingerprint, seen_pages)
                    if pages_ok > 0 and match:
                        nearest = nearest_ahash_match(fingerprint, seen_pages)
                        candidate_path = preserve_candidate(
                            candidate_dir, doc_no, page, match[2], out.read_bytes(), "image/png"
                        )
                        ew.writerow(evidence_row(
                            doc_no, page, match[2], True, "", "image/png",
                            candidate_path, out.read_bytes(), fingerprint, match,
                            nearest[2] if nearest else "",
                        ))
                        mw.writerow([doc_no, page, "duplicate_end", "", "", 0, now_utc()])
                        doc_status = "done"
                        break
                    nearest = nearest_ahash_match(fingerprint, seen_pages)
                    if nearest and nearest[2] <= 4:
                        ew.writerow(evidence_row(
                            doc_no, page, "perceptual_near_match_not_terminal", False,
                            "", "image/png", str(out.name), out.read_bytes(),
                            fingerprint, None, nearest[2],
                        ))
                    seen_pages.append((page, fingerprint))
                    size = out.stat().st_size
                    mw.writerow([doc_no, page, "ok_existing", "", str(out.name), size, now_utc()])
                    pages_ok += 1
                    total_pages += 1
                    continue

                status, body, upstream, content_type = fetch_page(key, doc_no, page, args.attempts)
                if status == "ok" and body:
                    fingerprint = page_fingerprint_bytes(body)
                    if not is_substantive_page(fingerprint):
                        candidate_path = preserve_candidate(
                            candidate_dir, doc_no, page, "blank_payload", body, content_type
                        )
                        ew.writerow(evidence_row(
                            doc_no, page, "blank_payload_not_terminal", False,
                            upstream, content_type, candidate_path, body, fingerprint,
                        ))
                        mw.writerow([doc_no, page, "blank_payload", upstream, "", 0, now_utc()])
                        fw.writerow([doc_no, page, "blank_payload", upstream, now_utc()])
                        failed_pages += 1
                        mf.flush()
                        ff.flush()
                        ef.flush()
                        if page < args.max_pages:
                            time.sleep(random.uniform(args.delay_min, args.delay_max))
                        continue
                    match = exact_duplicate_match(fingerprint, seen_pages)
                    if pages_ok > 0 and match:
                        nearest = nearest_ahash_match(fingerprint, seen_pages)
                        candidate_path = preserve_candidate(
                            candidate_dir, doc_no, page, match[2], body, content_type
                        )
                        ew.writerow(evidence_row(
                            doc_no, page, match[2], True, upstream, content_type,
                            candidate_path, body, fingerprint, match,
                            nearest[2] if nearest else "",
                        ))
                        mw.writerow([doc_no, page, "duplicate_end", upstream, "", 0, now_utc()])
                        doc_status = "done"
                        break
                    nearest = nearest_ahash_match(fingerprint, seen_pages)
                    if nearest and nearest[2] <= 4:
                        ew.writerow(evidence_row(
                            doc_no, page, "perceptual_near_match_not_terminal", False,
                            upstream, content_type, str(out.name), body, fingerprint,
                            None, nearest[2],
                        ))
                    seen_pages.append((page, fingerprint))
                    out.write_bytes(body)
                    mw.writerow([doc_no, page, "ok", upstream, str(out.name), len(body), now_utc()])
                    pages_ok += 1
                    total_pages += 1
                elif status == "end":
                    candidate_path = preserve_candidate(
                        candidate_dir, doc_no, page, "upstream_500_end", body, content_type
                    )
                    ew.writerow(evidence_row(
                        doc_no, page, "upstream_500_end", True, upstream,
                        content_type, candidate_path, body,
                    ))
                    mw.writerow([doc_no, page, "end", upstream, "", 0, now_utc()])
                    doc_status = "done"
                    break
                else:
                    candidate_path = preserve_candidate(
                        candidate_dir, doc_no, page, status, body, content_type
                    )
                    ew.writerow(evidence_row(
                        doc_no, page, f"{status}_not_terminal", False, upstream,
                        content_type, candidate_path, body,
                    ))
                    mw.writerow([doc_no, page, status, upstream, "", 0, now_utc()])
                    fw.writerow([doc_no, page, status, upstream, now_utc()])
                    failed_pages += 1

                mf.flush()
                ff.flush()
                ef.flush()
                if page < args.max_pages:
                    time.sleep(random.uniform(args.delay_min, args.delay_max))

            if pages_ok == 0 and doc_status == "done":
                doc_status = "no_pages"
            if doc_status in {"max_pages_reached", "no_pages"}:
                fw.writerow([doc_no, last_page, doc_status, "", now_utc()])
                failed_pages += 1
            doc_status_counts[doc_status] += 1
            sw.writerow([doc_no, doc_status, pages_ok, last_page, now_utc()])
            sf.flush()
            ef.flush()
            if i % 10 == 0 or i == len(docs):
                print(f"pulled {i}/{len(docs)} docs, ok_pages={total_pages}, failed_pages={failed_pages}", flush=True)

    summary_path.write_text(json.dumps({
        "generated_at_utc": now_utc(),
        "doclist": str(doclist),
        "outdir": str(outdir),
        "docs_requested": len(docs),
        "ok_pages": total_pages,
        "failed_pages": failed_pages,
        "doc_status_counts": dict(doc_status_counts),
        "max_pages": args.max_pages,
        "attempts": args.attempts,
        "delay_min": args.delay_min,
        "delay_max": args.delay_max,
        "outputs": {
            "pages_manifest_csv": str(manifest_path),
            "failed_pages_csv": str(failed_path),
            "doc_status_csv": str(doc_status_path),
            "terminal_evidence_csv": str(terminal_evidence_path),
            "terminal_candidates_dir": str(candidate_dir),
        },
    }, indent=2, sort_keys=True), encoding="utf-8")
    print(f"DONE docs={len(docs)} ok_pages={total_pages} failed_pages={failed_pages} -> {outdir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
