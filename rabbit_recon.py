#!/usr/bin/env python3
"""rabbit_recon.py PLAN OUTDIR: a read-only, one-off look at NETR for Rabbit's 2026-11-04 recon (not used by the watcher).

PLAN is a JSON list of steps {id, method, path, data?, via: netr|worker|ext, headers?, discover?}. netr steps go to
datastore.netronline.com in one curl_cffi chrome124 session (as rabbit_watch.py does); worker steps go to the netr-thumb
Worker with NETR_PROXY_KEY (as rabbit_pages.py does); ext steps may only reach netr.foreclosure.com. Each step is sent
once, 3-5 s apart, never retried. A step with discover also fetches the page's own /js/*.js (vendor libraries skipped).
The run stops at the first 429, Cloudflare challenge or block page, "too many searches" style wall, or a 403 from
datastore.netronline.com itself; a Worker 403 with no body is NETR's end-of-document answer and does not stop it.
OUTDIR gets <id>.head (status, URL, every response header) and <id>.body (the whole body, key scrubbed), summary.json.
The workflow encrypts OUTDIR before upload (public repo).
"""
import hashlib
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from curl_cffi import requests as creq

NETR = "https://datastore.netronline.com"
WORKER = "https://netr-thumb.kaiescobar09.workers.dev"
EXT = ("https://netr.foreclosure.com/",)
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
AJAX = {"Referer": NETR + "/losangeles", "Origin": NETR, "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01"}
STOP = re.compile(rb"cf_chl_opt|<title>just a moment|attention required! \| cloudflare|you have been blocked"
                  rb"|error code: 10(?:06|07|08|10|12|15|20)|too many searches|please wait a moment|unusual (?:activity|traffic)"
                  rb"|rate[ -_]?limit", re.I)


def worker(path: str, key: str):
    req = urllib.request.Request(WORKER + path, headers={"X-Auth": key, "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return r.status, r.url, list(r.headers.items()), r.read()
    except urllib.error.HTTPError as e:
        return e.code, WORKER + path, list(e.headers.items()), e.read()


def main() -> int:
    plan, out = json.loads(Path(sys.argv[1]).read_text()), Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    key = os.environ.get("NETR_PROXY_KEY", "")
    s = creq.Session(impersonate="chrome124")
    summary, stopped, first = [], None, True
    steps = list(plan)
    while steps:
        step = steps.pop(0)
        if stopped:
            summary.append({"id": step["id"], "state": f"not_asked: {stopped}"})
            continue
        if not first:
            time.sleep(random.uniform(3.0, 5.0))
        first = False
        via, method, path = step.get("via", "netr"), step.get("method", "GET"), step["path"]
        try:
            if via == "worker":
                status, url, headers, body = worker(path, key)
            else:
                url = path if via == "ext" else NETR + path
                if via == "ext" and not url.startswith(EXT):
                    raise ValueError(f"ext host not allowed: {url}")
                hdr = {**(AJAX if method == "POST" else {}), **step.get("headers", {})}
                r = s.request(method, url, data=step.get("data"), headers=hdr, timeout=40, allow_redirects=False)
                status, url, headers, body = r.status_code, r.url, list(r.headers.items()), r.content
        except Exception as e:
            summary.append({"id": step["id"], "state": f"error_{type(e).__name__}: {str(e)[:200]}"})
            continue
        if key:
            body = body.replace(key.encode(), b"[key]")
        (out / f"{step['id']}.head").write_text(f"{method} {url}\nstatus {status}\n" +
                                                "".join(f"{k}: {v}\n" for k, v in headers))
        (out / f"{step['id']}.body").write_bytes(body)
        ctype = next((v for k, v in headers if k.lower() == "content-type"), "")
        summary.append({"id": step["id"], "status": status, "type": ctype, "bytes": len(body),
                        "sha256": hashlib.sha256(body).hexdigest()})
        print(step["id"], status, ctype, len(body), flush=True)
        if status == 429 or (STOP.search(body[:200000]) and not ctype.startswith(("image/", "application/javascript",
                                                                                 "text/javascript"))):
            stopped = f"{step['id']}: http {status} wall"
        elif status == 403 and via != "worker":
            stopped = f"{step['id']}: http 403 from {via}"
        elif status == 403 and body:
            stopped = f"{step['id']}: worker 403 with a body"
        if step.get("discover") and status == 200:
            for src in dict.fromkeys(re.findall(rb'<script[^>]+src="(/js/[^"]+)"', body)):
                if b"vendor" not in src and b"parsley" not in src:
                    js = src.decode()
                    steps.insert(0, {"id": step["id"] + "_js_" + re.sub(r"\W+", "_", js.strip("/")), "path": js})
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
