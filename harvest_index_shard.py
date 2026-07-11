#!/usr/bin/env python3
"""harvest_index_shard.py -- harvest a contiguous LA recorder doc# range via the
AUTHORITATIVE county index endpoint (POST /lasearch), classify each row, write a
small CSV. RESEARCH over PUBLIC records only.

Designed to run on a GitHub-hosted runner (its own Azure IP = its own NETR
per-IP budget). Concurrency + adaptive 429 backoff tuned to the measured
limiter: burst ~20 then 429, refill ~0.3 req/s, 429 recovers in ~1.5s (no
penalty box). We disable la_county_index's polite multi-second jitter (that is
for the home IP) and let the limiter itself pace us.

Usage:
  python3 harvest_index_shard.py <doc_start> <doc_end> <out_csv> [--conc 6]

CSV columns: doc_no,ok,county_type,record_date,grantors,grantees,ain,lead_class,reason
"""
import sys, os, csv, time, threading, queue, argparse, collections, random

# Reach the modules whether run from repo root or elsewhere.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import la_county_index as idx
import lead_class as lc

# Pace within the measured per-IP limiter. NETR's soft rate-wall comes back as
# HTTP 200 with a "Too many searches. Please wait a moment" body (NOT a 429),
# so la_county_index parses it as parse_no_row and does NOT retry. We detect
# that here and re-issue with backoff. Keep a small inter-call floor so a single
# worker rides the ~0.3 req/s refill instead of blowing the ~20 burst instantly.
# Measured 2026-06-27: conc=6 + (0.55,0.85) floor SATURATES NETR's ~20 req/min
# per-IP limit -> ~44% of REAL docs come back as the soft "Too many searches"
# wall and the shallow budget drops them. Pace one serial worker at ~3s (≈18/min,
# just under the limit) for a clean ~100% capture. Env-tunable for future runs.
idx.THROTTLE = (
    float(os.environ.get("NETR_THROTTLE_MIN", "2.8")),
    float(os.environ.get("NETR_THROTTLE_MAX", "3.4")),
)
idx.RETRIES = 3

THROTTLE_MARKERS = ("too many searches", "please wait a moment")
RATE_LOCK = threading.Lock()
NEXT_REQUEST_AT = 0.0


def _pace_request():
    """Enforce one aggregate request cadence per runner/IP.

    The old delay lived inside each worker.  With ``--conc 6`` that multiplied
    a nominal 18 requests/minute into roughly 108 requests/minute and caused
    NETR's HTTP-200 soft rate wall.  Threads may still overlap parsing and
    backoff, but request starts share this single per-process clock.
    """
    global NEXT_REQUEST_AT
    low = float(os.environ.get("NETR_REQUEST_FLOOR_MIN", "2.9"))
    high = float(os.environ.get("NETR_REQUEST_FLOOR_MAX", "3.5"))
    with RATE_LOCK:
        now = time.monotonic()
        wait = max(0.0, NEXT_REQUEST_AT - now)
        NEXT_REQUEST_AT = max(now, NEXT_REQUEST_AT) + random.uniform(low, high)
    if wait:
        time.sleep(wait)


def _is_throttled(res):
    rsn = (res.get("reason") or "").lower()
    ct = (res.get("county_type") or "").lower()
    return any(m in rsn or m in ct for m in THROTTLE_MARKERS)


def fetch_resilient(doc, max_wall_retries=6, max_wall_budget=40.0):
    """idx.fetch + detection of the soft 'Too many searches' rate-wall (HTTP 200
    body). SHALLOW backoff: cap total per-doc backoff so one persistently-walled
    doc can't stall the whole shard (the 9-deep version did exactly that)."""
    delay = 1.5
    spent = 0.0
    for attempt in range(max_wall_retries):
        _pace_request()
        res = idx.fetch(str(doc), save_evidence=False)
        if res.get("ok") or not _is_throttled(res):
            return res
        if spent >= max_wall_budget:
            return res
        s = min(delay + random.uniform(0, delay), max_wall_budget - spent)
        time.sleep(max(s, 0.2)); spent += s
        delay = min(delay * 1.6, 8.0)
    return res


def harvest_docs(docs, out_csv, conc):
    jobs = queue.Queue()
    for d in dict.fromkeys(int(x) for x in docs):
        jobs.put(d)
    total = jobs.qsize()
    stats = collections.Counter()
    lock = threading.Lock()
    fh = open(out_csv, "w", newline="", encoding="utf-8")
    w = csv.writer(fh)
    w.writerow(["doc_no", "ok", "county_type", "record_date",
                "grantors", "grantees", "ain", "lead_class", "reason"])
    t0 = time.time()

    def write_row(res):
        cls = lc.lead_class(res.get("county_type")) if res.get("ok") else ""
        with lock:
            w.writerow([
                res["doc_no"], res["ok"], res.get("county_type") or "",
                res.get("record_date") or "",
                "; ".join(res.get("grantors") or []),
                "; ".join(res.get("grantees") or []),
                res.get("ain") or "", cls, res.get("reason") or "",
            ])
            fh.flush()
            stats["ok" if res.get("ok") else (res.get("reason") or "fail")[:18]] += 1
            done = sum(stats.values())
            if done % 100 == 0 or done == total:
                el = time.time() - t0
                print("%d/%d  %.0f req/min  %s" %
                      (done, total, done / el * 60, dict(stats)), flush=True)

    def worker():
        while True:
            try:
                d = jobs.get_nowait()
            except queue.Empty:
                return
            try:
                # save_evidence False: runners are ephemeral, CSV is the product
                res = fetch_resilient(d)
            except Exception as e:
                res = {"doc_no": str(d), "ok": False,
                       "reason": "exc:%s" % type(e).__name__}
            write_row(res)
            jobs.task_done()

    ts = [threading.Thread(target=worker, daemon=True) for _ in range(conc)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    fh.close()
    el = time.time() - t0
    print("\nSHARD DONE %d docs in %.1f min (%.0f req/min) stats=%s" %
          (total, el / 60, total / el * 60, dict(stats)), flush=True)


def harvest(doc_start, doc_end, out_csv, conc):
    harvest_docs(range(doc_start, doc_end + 1), out_csv, conc)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("doc_start", type=int)
    ap.add_argument("doc_end", type=int)
    ap.add_argument("out_csv")
    ap.add_argument("--conc", type=int, default=2)
    ap.add_argument("--doc-file", help="newline-delimited exact document IDs")
    a = ap.parse_args()
    if a.doc_file:
        with open(a.doc_file, encoding="utf-8") as fh:
            docs = [line.strip() for line in fh if line.strip()]
        if not docs:
            ap.error("--doc-file is empty")
        harvest_docs(docs, a.out_csv, a.conc)
    else:
        harvest(a.doc_start, a.doc_end, a.out_csv, a.conc)


if __name__ == "__main__":
    main()
