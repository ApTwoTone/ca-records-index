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
import sys, os, csv, time, threading, queue, argparse, collections

# Reach the modules whether run from repo root or elsewhere.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import la_county_index as idx
import lead_class as lc

# Neutralize the home-IP polite throttle: on a fresh runner IP we want to ride
# the burst then back off only on actual 429s. We keep a tiny floor so we don't
# spin. la_county_index._throttle() reads module THROTTLE; set it small.
idx.THROTTLE = (0.0, 0.05)
idx.RETRIES = 5            # index client retries http_429 internally w/ backoff


def harvest(doc_start, doc_end, out_csv, conc):
    jobs = queue.Queue()
    for d in range(doc_start, doc_end + 1):
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
                res = idx.fetch(str(d), save_evidence=False)
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("doc_start", type=int)
    ap.add_argument("doc_end", type=int)
    ap.add_argument("out_csv")
    ap.add_argument("--conc", type=int, default=6)
    a = ap.parse_args()
    harvest(a.doc_start, a.doc_end, a.out_csv, a.conc)


if __name__ == "__main__":
    main()
