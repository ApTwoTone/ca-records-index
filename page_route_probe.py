#!/usr/bin/env python3
"""Bounded fleet probe: does datastore.netronline.com expose a non-thumb page image?

Runs on GitHub-hosted ubuntu-latest (fleet egress), NOT Spark/home.
Pace: >= 3.2s between origin requests (~18/min, under 20 req/min/IP).
Stops on 429/403/503 or challenge body. Documents every request.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urljoin, urlparse

ORIGIN = "https://datastore.netronline.com"
WORKER = "https://netr-thumb.kaiescobar09.workers.dev"
UA = "Zoar-Discovery-ReadOnly-PageRouteProbe/1.0"
MIN_GAP_S = 3.2
MAX_BODY = 2_000_000
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
STOP_STATUSES = {403, 429, 503}
CHALLENGE_MARKERS = (
    "too many searches",
    "attention required",
    "cf-mitigated",
    "error 1010",
    "checking your browser",
)

CTX = ssl.create_default_context()
LAST = 0.0
ROWS: list[dict] = []
STOP = None
OUT = Path(os.environ.get("PROBE_OUT", "out"))
SHARD = os.environ.get("PROBE_SHARD", "0")
DOC_LIVE = os.environ.get("PROBE_DOC_LIVE", "20260599849")
DOC_INDEX = os.environ.get("PROBE_DOC_INDEX", "20260448794")


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def pace() -> None:
    global LAST
    gap = time.time() - LAST
    if LAST and gap < MIN_GAP_S:
        time.sleep(MIN_GAP_S - gap)
    LAST = time.time()


def png_dims(body: bytes):
    if len(body) < 24 or not body.startswith(PNG_MAGIC):
        return None
    return int.from_bytes(body[16:20], "big"), int.from_bytes(body[20:24], "big")


def egress_ip():
    try:
        req = urllib.request.Request(
            "https://api.ipify.org?format=json",
            headers={"User-Agent": UA},
        )
        with urllib.request.urlopen(req, timeout=10, context=CTX) as resp:
            return json.loads(resp.read().decode()).get("ip")
    except Exception:
        return None


def looks_challenged(status: int, body: bytes, text: str):
    if status in STOP_STATUSES:
        return "status_%s" % status
    low = text.lower()
    for m in CHALLENGE_MARKERS:
        if m in low:
            return "body:%s" % m
    return None


def request(url: str, *, headers=None, note: str = ""):
    global STOP
    row = {
        "ts": now(),
        "shard": SHARD,
        "url": url,
        "note": note,
        "host": urlparse(url).netloc,
        "path": urlparse(url).path,
    }
    if STOP:
        row.update({"skipped": True, "stop_reason": STOP})
        ROWS.append(row)
        return row
    pace()
    t0 = time.time()
    hdrs = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        hdrs.update(headers)
    try:
        req = urllib.request.Request(url, headers=hdrs)
        with urllib.request.urlopen(req, timeout=30, context=CTX) as resp:
            body = resp.read(MAX_BODY)
            extra = resp.read(1)
            truncated = bool(extra)
            status = resp.status
            rh = {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as exc:
        status = exc.code
        try:
            body = exc.read(MAX_BODY)
        except Exception:
            body = b""
        truncated = False
        rh = {k.lower(): v for k, v in (exc.headers.items() if exc.headers else [])}
    except Exception as exc:
        row.update({
            "error": "%s:%s" % (type(exc).__name__, exc),
            "elapsed_ms": int((time.time() - t0) * 1000),
        })
        ROWS.append(row)
        return row
    try:
        text = body.decode("utf-8", "replace")
    except Exception:
        text = ""
    dims = png_dims(body)
    row.update({
        "status": status,
        "elapsed_ms": int((time.time() - t0) * 1000),
        "bytes": len(body),
        "truncated": truncated,
        "content_type": rh.get("content-type", ""),
        "upstream": rh.get("x-upstream-status", ""),
        "cf_ray": rh.get("cf-ray", ""),
        "png": bool(dims),
        "png_width": dims[0] if dims else None,
        "png_height": dims[1] if dims else None,
        "body_head_ascii": re.sub(r"[^\x20-\x7e]", ".", text[:240]),
    })
    challenge = looks_challenged(status, body, text)
    if challenge:
        STOP = challenge
        row["stop_reason"] = challenge
    ctype = row["content_type"]
    if "html" in ctype or "javascript" in ctype or "/preview/" in url or "/js/" in url:
        row["body_text"] = text[:120000]
    ROWS.append(row)
    return row


def extract_urls(html: str, base: str):
    found = []
    seen = set()
    patterns = [
        r"""(?:href|src|data-src|data-url)\s*=\s*['"]([^'"]+)['"]""",
        r"""url\(\s*['"]?([^'")\s]+)['"]?\s*\)""",
        r"""https?://datastore\.netronline\.com[^'"\s<>]+""",
        r"""/(?:thumb|preview|page|pages|image|img|full|pdf|view|viewer|scan|original|large|hires|download|doc|document|media)/[A-Za-z0-9_./-]+""",
    ]
    for pat in patterns:
        for m in re.findall(pat, html or "", re.I):
            u = m.strip()
            if u.startswith("#") or u.lower().startswith("javascript:"):
                continue
            absu = urljoin(base, u)
            if absu not in seen:
                seen.add(absu)
                found.append(absu)
    return found


def write_out():
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / ("requests_shard%s.jsonl" % SHARD)).write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in ROWS),
        encoding="utf-8",
    )
    summary = {
        "shard": SHARD,
        "stop": STOP,
        "requests": len(ROWS),
        "statuses": {},
        "pngs": [],
    }
    for r in ROWS:
        st = str(r.get("status") or r.get("error") or r.get("skipped"))
        summary["statuses"][st] = summary["statuses"].get(st, 0) + 1
        if r.get("png"):
            summary["pngs"].append({
                "url": r.get("url"),
                "status": r.get("status"),
                "bytes": r.get("bytes"),
                "width": r.get("png_width"),
                "height": r.get("png_height"),
            })
    (OUT / ("summary_shard%s.json" % SHARD)).write_text(
        json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8"
    )


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    ip = egress_ip()
    ident = {
        "shard": SHARD,
        "public_egress_ip": ip,
        "recorded_at": now(),
        "repository": os.environ.get("GITHUB_REPOSITORY"),
        "run_id": os.environ.get("GITHUB_RUN_ID"),
        "runner_name": os.environ.get("RUNNER_NAME"),
        "doc_live": DOC_LIVE,
        "doc_index": DOC_INDEX,
        "contains_secrets": False,
    }
    (OUT / ("runner_identity_%s.json" % SHARD)).write_text(
        json.dumps(ident, indent=2, sort_keys=True), encoding="utf-8"
    )
    print("EGRESS", ip, "SHARD", SHARD, flush=True)

    family = os.environ.get("PROBE_FAMILY", "canary")
    worker_key = os.environ.get("NETR_PROXY_KEY", "").strip()

    def origin(path, note=""):
        return request(ORIGIN + path, note=note)

    if family == "canary":
        origin("/thumb/%s/1" % DOC_LIVE, "baseline_thumb_live")
        html_row = origin("/preview/%s/1" % DOC_LIVE, "preview_html_live")
        html = html_row.get("body_text") or ""
        urls = extract_urls(html, ORIGIN + "/preview/%s/1" % DOC_LIVE)
        (OUT / "preview_html_extracted_urls.json").write_text(
            json.dumps({"count": len(urls), "urls": urls[:80]}, indent=2),
            encoding="utf-8",
        )
        (OUT / ("preview_%s_1.html" % DOC_LIVE)).write_text(html, encoding="utf-8")
        already = {
            ORIGIN + "/thumb/%s/1" % DOC_LIVE,
            ORIGIN + "/preview/%s/1" % DOC_LIVE,
        }
        n = 0
        for u in urls:
            if n >= 6:
                break
            if u in already:
                continue
            host = urlparse(u).netloc
            if host and host != "datastore.netronline.com":
                continue
            request(u, note="discovered_from_preview_html")
            n += 1
        origin("/js/ladocs.js", "public_js")
        origin("/js/common.js", "public_js")

    elif family == "alt_a":
        for path in (
            "/page/%s/1" % DOC_LIVE,
            "/pages/%s/1" % DOC_LIVE,
            "/image/%s/1" % DOC_LIVE,
            "/img/%s/1" % DOC_LIVE,
            "/full/%s/1" % DOC_LIVE,
            "/original/%s/1" % DOC_LIVE,
        ):
            origin(path, "alt_rest")

    elif family == "alt_b":
        for path in (
            "/pdf/%s" % DOC_LIVE,
            "/pdf/%s/1" % DOC_LIVE,
            "/view/%s/1" % DOC_LIVE,
            "/viewer/%s/1" % DOC_LIVE,
            "/download/%s/1" % DOC_LIVE,
            "/hires/%s/1" % DOC_LIVE,
            "/large/%s/1" % DOC_LIVE,
            "/scan/%s/1" % DOC_LIVE,
        ):
            origin(path, "alt_rest")

    elif family == "alt_c":
        for path in (
            "/preview/%s/1.png" % DOC_LIVE,
            "/preview/%s/1.jpg" % DOC_LIVE,
            "/preview/%s/1.pdf" % DOC_LIVE,
            "/losangeles/preview/%s/1" % DOC_LIVE,
            "/la/preview/%s/1" % DOC_LIVE,
            "/document/%s/1" % DOC_LIVE,
            "/doc/%s/1" % DOC_LIVE,
            "/media/%s/1" % DOC_LIVE,
        ):
            origin(path, "alt_rest")

    elif family == "second_doc":
        origin("/thumb/%s/1" % DOC_INDEX, "baseline_thumb_index_doc")
        origin("/preview/%s/1" % DOC_INDEX, "preview_html_index_doc")
        origin("/thumb/%s/2" % DOC_LIVE, "thumb_page2")
        origin("/preview/%s/2" % DOC_LIVE, "preview_page2")

    elif family == "worker":
        origin("/thumb/%s/1" % DOC_LIVE, "origin_thumb_repeat")
        if worker_key:
            request(
                "%s/thumb/%s/1" % (WORKER, DOC_LIVE),
                headers={"X-Auth": worker_key},
                note="worker_thumb_passthrough",
            )
            request(
                "%s/preview/%s/1" % (WORKER, DOC_LIVE),
                headers={"X-Auth": worker_key},
                note="worker_preview_expect_400",
            )
            request(
                "%s/page/%s/1" % (WORKER, DOC_LIVE),
                headers={"X-Auth": worker_key},
                note="worker_page_expect_400",
            )
        else:
            ROWS.append({"ts": now(), "note": "worker_skipped_no_key", "shard": SHARD})

    else:
        raise SystemExit("unknown family %s" % family)

    write_out()
    print("STOP", STOP, "N", len(ROWS), flush=True)
    return 2 if STOP else 0


if __name__ == "__main__":
    raise SystemExit(main())
