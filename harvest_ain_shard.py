#!/usr/bin/env python3
# =============================================================================
# harvest_ain_shard.py -- distributed AIN/parcel-history harvester for ONE shard
# =============================================================================
# The 10-year-backlog analog of harvest_index_shard.py. Instead of walking a
# doc# RANGE, this walks a slice of an AIN LIST: one POST /lasearch ain=<APN>
# returns that parcel's ENTIRE recorded-document history (Doc#|Date|Type|
# Grantors|Grantees), AUTHORITATIVE and pre-anchored to the searched AIN (kills
# the wrong-property problem). RESEARCH over PUBLIC records only.
#
# Runs on a GitHub-hosted runner (its own Azure IP = its own NETR per-IP budget).
# Output is RAW: classification (canonical_type/is_target/is_neg_trap) is
# deferred to the Spark-side merge (la_ain_harvest.classify) so the taxonomy
# never forks. Two small CSVs per shard, plus sidecar proof/retry files:
#   <prefix>_docs.csv : ain,doc_no,record_date,county_type,grantors,grantees
#   <prefix>_scan.csv : ain,doc_count,status            (done/throttled_defer/error/empty)
#   <prefix>_retry_ains.csv/txt : non-done AINs for backfill
#   <prefix>_summary.json : exact status counts, parse warnings, artifact contract
#
# Usage:
#   python3 harvest_ain_shard.py <ain_list_file> <start_line> <end_line> <out_prefix> [--conc 5]
#   ( start_line inclusive, end_line exclusive, 0-based -- matches the planner )
# =============================================================================
import sys, os, re, csv, json, time, random, threading, queue, argparse, collections, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import la_county_index as idx   # proven curl_cffi session + HTML parse helpers

# Pace to the MEASURED per-IP limiter: ~20 req/min (~0.33 req/s) refill, burst
# ~20. The distribution multiplier is 20 IPs, NOT intra-IP concurrency -- a
# 5-canary at conc=5 blew the burst and threw 38% "Too many searches" defers.
# So default to ONE worker per shard, paced ~2.8-3.4s/req (~19/min/IP), which
# the single-thread idx._throttle enforces cleanly (it shares _LAST_CALL and is
# NOT thread-safe, so >1 worker under-paces). NETR's soft rate-wall is an HTTP
# 200 body "Too many searches. Please wait a moment" (NOT a 429) -- string-match
# + back off. With clean pacing, throttles are rare and recover in ~1.5s.
idx.THROTTLE = (2.8, 3.4)
idx.RETRIES = 3

THROTTLE_MARKERS = ("too many searches", "please wait a moment")


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_rows(results_html):
    """All rows for the parcel: (doc_no, record_date, county_type, grantors[], grantees[])."""
    out = []
    skipped = 0
    for m in idx._ROW_RE.finditer(results_html or ""):
        cells = idx._CELL_RE.findall(m.group(1))
        if len(cells) < 5:
            skipped += 1
            continue
        doc = idx._clean(cells[0])
        dm = idx._DATE_RE.search(idx._clean(cells[1]))
        rec = dm.group(0) if dm else None
        ctype = idx._clean(re.split(r"<a\b", cells[2], flags=re.I)[0]) or None
        gtor = idx._split_names(cells[3])
        gtee = idx._split_names(cells[4])
        out.append((doc, rec, ctype, gtor, gtee))
    return out, skipped


def search_ain_page(session, ain, page=1):
    """One POST /lasearch for ain=<APN>, page N. Mirrors la_ain_harvest.search_ain
    but uses la_county_index's session/parse. Returns a dict:
      {throttled:True} | {err:str} | {count:int|None, rows:[...], capped:bool}"""
    data = {
        "page": str(page), "g-recaptcha-response": "", "beg_dt": idx.EARLIEST,
        "end_dt": datetime.date.today().isoformat(), "company": "", "first_name": "",
        "last_name": "", "signer": "R", "ain": ain, "doc_no": "",
    }
    headers = {
        "Referer": idx.FORM_URL, "Origin": idx.BASE, "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
    }
    idx._throttle()
    r = session.post(idx.SEARCH_URL, data=data, headers=headers, timeout=idx.TIMEOUT)
    if r.status_code != 200:
        return {"err": "http_%d" % r.status_code}
    try:
        j = json.loads(r.text)
    except Exception:
        return {"err": "non_json"}
    cnt = idx._clean(j[0] if isinstance(j, list) and j else "")
    rh = j[1] if isinstance(j, list) and len(j) > 1 else ""
    low = cnt.lower()
    if any(m in low for m in THROTTLE_MARKERS):
        return {"throttled": True}
    if "No documents" in cnt:
        return {"count": 0, "rows": []}
    mtot = re.search(r"([\d,]+)\s+document", cnt)
    total = int(mtot.group(1).replace(",", "")) if mtot else None
    rows, parse_skipped = _parse_rows(rh)
    return {"count": total, "rows": rows, "capped": "only the most recent" in cnt, "parse_skipped": parse_skipped}


def harvest_ain(session, ain, spacing=0.0, max_wall_budget=15.0, max_pages=25):
    """Full parcel history (paginated) with shallow soft-throttle backoff.
    Returns (rows, status, parse_skipped_rows) where status in done/throttled_defer/error/empty."""
    page = 1
    rows = []
    total = None
    parse_skipped = 0
    spent = 0.0
    delay = 1.5
    throttle_hits = 0
    page_cap_hit = False
    while True:
        try:
            res = search_ain_page(session, ain, page)
        except Exception as e:
            return rows, "error:%s" % type(e).__name__, parse_skipped
        if res.get("throttled"):
            throttle_hits += 1
            if spent >= max_wall_budget or throttle_hits > 4:
                return rows, "throttled_defer", parse_skipped
            s = min(delay + random.uniform(0, delay), max_wall_budget - spent)
            time.sleep(max(s, 0.2)); spent += s
            delay = min(delay * 1.6, 8.0)
            continue
        if res.get("err"):
            return rows, "error:%s" % res["err"], parse_skipped
        rows += res["rows"]
        parse_skipped += int(res.get("parse_skipped") or 0)
        total = res.get("count")
        page += 1
        if page > max_pages:
            page_cap_hit = bool(res.get("capped")) and (total is None or len(rows) < total)
            break
        if not res.get("capped") or len(rows) >= (total or 0):
            break
        if spacing:
            time.sleep(random.uniform(spacing * 0.8, spacing * 1.2))
    if page_cap_hit:
        return rows, "page_cap_reached", parse_skipped
    return rows, ("done" if rows or total == 0 else "empty"), parse_skipped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ain_file")
    ap.add_argument("start_line", type=int)   # inclusive, 0-based
    ap.add_argument("end_line", type=int)     # exclusive
    ap.add_argument("out_prefix")
    ap.add_argument("--conc", type=int, default=1)
    ap.add_argument("--max-minutes", type=float, default=300.0,
                    help="stop pulling new AINs past this wall-time so the runner "
                         "self-limits well under GitHub's 6h job cap and still uploads")
    ap.add_argument("--max-pages", type=int, default=25,
                    help="maximum result pages to walk per AIN before marking page_cap_reached")
    a = ap.parse_args()

    with open(a.ain_file, encoding="utf-8") as fh:
        all_ains = [x.strip() for x in fh if x.strip()]
    ains = all_ains[a.start_line:a.end_line]
    total = len(ains)
    print("shard: %d AINs (lines %d..%d of %d) conc=%d"
          % (total, a.start_line, a.end_line, len(all_ains), a.conc), flush=True)

    session = idx._session()
    jobs = queue.Queue()
    for x in ains:
        jobs.put(x)
    stats = collections.Counter()
    status_counts = collections.Counter()
    retry_rows = []
    parse_issue_rows = []
    docs_rows_written = 0
    lock = threading.Lock()
    docs_fh = open(a.out_prefix + "_docs.csv", "w", newline="", encoding="utf-8")
    scan_fh = open(a.out_prefix + "_scan.csv", "w", newline="", encoding="utf-8")
    progress_path = a.out_prefix + "_progress.json"
    dw = csv.writer(docs_fh); sw = csv.writer(scan_fh)
    dw.writerow(["ain", "doc_no", "record_date", "county_type", "grantors", "grantees"])
    sw.writerow(["ain", "doc_count", "status"])
    t0 = time.time()

    deadline = t0 + a.max_minutes * 60.0

    def write_progress(extra=None):
        payload = {
            "generated_at": _now(),
            "start_line": a.start_line,
            "end_line": a.end_line,
            "total_ains": total,
            "processed_ains": sum(stats.values()),
            "remaining_queue": jobs.qsize(),
            "stats": dict(stats),
            "status_counts": dict(status_counts),
            "deadline_utc": datetime.datetime.fromtimestamp(deadline, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        if extra:
            payload.update(extra)
        try:
            with open(progress_path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, sort_keys=True)
        except Exception:
            pass

    def worker():
        nonlocal docs_rows_written
        while True:
            if time.time() >= deadline:   # self-limit; unscanned AINs stay 'remaining'
                return
            try:
                ain = jobs.get_nowait()
            except queue.Empty:
                return
            try:
                rows, status, parse_skipped = harvest_ain(session, ain, max_pages=a.max_pages)
            except Exception as e:
                rows, status, parse_skipped = [], "error:%s" % type(e).__name__, 0
            with lock:
                for (doc, rec, ctype, gtor, gtee) in rows:
                    dw.writerow([ain, doc, rec or "", ctype or "",
                                 json.dumps(gtor), json.dumps(gtee)])
                docs_rows_written += len(rows)
                sw.writerow([ain, len(rows), status])
                docs_fh.flush(); scan_fh.flush()
                stats[status.split(":")[0]] += 1
                status_counts[status] += 1
                if status != "done":
                    retry_rows.append({
                        "ain": ain,
                        "doc_count": len(rows),
                        "status": status,
                        "shard_start_line": a.start_line,
                        "shard_end_line": a.end_line,
                        "finished_at_utc": _now(),
                    })
                if parse_skipped:
                    parse_issue_rows.append({
                        "ain": ain,
                        "issue": "malformed_result_rows_skipped",
                        "count": parse_skipped,
                        "finished_at_utc": _now(),
                    })
                done = sum(stats.values())
                if done % 25 == 0 or done == total:
                    write_progress()
                if done % 200 == 0 or done == total:
                    el = time.time() - t0
                    print("%d/%d  %.0f ain/min  %s"
                          % (done, total, done / el * 60 if el else 0, dict(stats)), flush=True)
            jobs.task_done()

    ts = [threading.Thread(target=worker, daemon=True) for _ in range(a.conc)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    leftover = []
    while True:
        try:
            leftover.append(jobs.get_nowait())
            jobs.task_done()
        except queue.Empty:
            break
    if leftover:
        with lock:
            for ain in leftover:
                sw.writerow([ain, 0, "not_started_deadline"])
                retry_rows.append({
                    "ain": ain,
                    "doc_count": 0,
                    "status": "not_started_deadline",
                    "shard_start_line": a.start_line,
                    "shard_end_line": a.end_line,
                    "finished_at_utc": _now(),
                })
            scan_fh.flush()
            stats["not_started_deadline"] += len(leftover)
            status_counts["not_started_deadline"] += len(leftover)
            write_progress({"not_started_deadline": len(leftover)})
            print("marked %d unscanned AINs as not_started_deadline" % len(leftover), flush=True)
    docs_fh.close(); scan_fh.close()

    retry_csv = a.out_prefix + "_retry_ains.csv"
    retry_txt = a.out_prefix + "_retry_ains.txt"
    parse_issues_csv = a.out_prefix + "_parse_issues.csv"
    summary_json = a.out_prefix + "_summary.json"

    with open(retry_csv, "w", newline="", encoding="utf-8") as fh:
        fields = ["ain", "doc_count", "status", "shard_start_line", "shard_end_line", "finished_at_utc"]
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for row in retry_rows:
            writer.writerow(row)
    with open(retry_txt, "w", encoding="utf-8") as fh:
        for row in retry_rows:
            fh.write(str(row["ain"]) + "\n")
    with open(parse_issues_csv, "w", newline="", encoding="utf-8") as fh:
        fields = ["ain", "issue", "count", "finished_at_utc"]
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for row in parse_issue_rows:
            writer.writerow(row)
    with open(summary_json, "w", encoding="utf-8") as fh:
        json.dump({
            "generated_at_utc": _now(),
            "start_line": a.start_line,
            "end_line": a.end_line,
            "total_ains": total,
            "processed_ains": sum(stats.values()),
            "docs_rows_written": docs_rows_written,
            "status_groups": dict(stats),
            "status_counts": dict(status_counts),
            "unfinished_ains": len(retry_rows),
            "parse_issue_ains": len(parse_issue_rows),
            "parse_skipped_rows": sum(int(row["count"]) for row in parse_issue_rows),
            "max_pages": a.max_pages,
            "max_minutes": a.max_minutes,
            "artifact_contract": {
                "docs_csv": ["ain", "doc_no", "record_date", "county_type", "grantors", "grantees"],
                "scan_csv": ["ain", "doc_count", "status"],
                "retry_ains_csv": ["ain", "doc_count", "status", "shard_start_line", "shard_end_line", "finished_at_utc"],
                "parse_issues_csv": ["ain", "issue", "count", "finished_at_utc"],
                "not_collected_by_ain_index": ["mailing_address", "phone_numbers", "document_page_images", "assembled_pdf"],
            },
            "retry_csv": os.path.basename(retry_csv),
            "retry_txt": os.path.basename(retry_txt),
            "parse_issues_csv": os.path.basename(parse_issues_csv),
        }, fh, indent=2, sort_keys=True)
    el = time.time() - t0
    print("\nSHARD DONE %d AINs in %.1f min (%.0f ain/min) stats=%s"
          % (total, el / 60, total / el * 60 if el else 0, dict(stats)), flush=True)


if __name__ == "__main__":
    main()
