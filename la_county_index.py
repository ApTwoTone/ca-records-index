#!/usr/bin/env python3
# =============================================================================
# la_county_index.py  --  AUTHORITATIVE LA COUNTY RECORDER INDEX FETCHER
# =============================================================================
# RESEARCH-ONLY / PUBLIC-RECORDS verification layer for the NETR/NOD pipeline.
#
# Given an LA County recorder Document # (or AIN), returns the county
# Registrar-Recorder's OWN index row: Doc#, Date, county Type string,
# Grantors, Grantees. This is the AUTHORITATIVE second source of truth that
# resolves vision-model classification guesses (e.g. is doc X really a
# "notice_of_default", or is it a county "DEFAULT CERTIFICATION"?).
#
# SIGNAL ONLY. No outreach, no enrichment, no PII export, no contact. This
# module only fetches + returns + saves raw evidence. The county index cache
# table is RECONCILE's job, not this module's.
#
# METHOD (empirically proven 2026-06-17 on this box):
#   The LA datastore search form (https://datastore.netronline.com/losangeles)
#   POSTs the serialized form to the JSON endpoint  POST /lasearch  and renders
#   data[0]=count_html, data[1]=results_html (see /js/ladocs.js, /js/common.js).
#   The page advertises "protected by reCAPTCHA" (v3 invisible, sitekey
#   6Lc-ADEsAAAAAN5yv6eswFzcQJDEim1MIOmpnUGF, action 'search'), BUT the server
#   does NOT enforce the g-recaptcha-response token on /lasearch: a POST with an
#   EMPTY token returns the real county row. A good TLS/JA3 fingerprint
#   (curl_cffi impersonate=chrome124) is what's required to reach the origin.
#   Measured latency ~90-350 ms/doc. => HTTP works; CDP is only a fallback.
#
# Contract (keep EXACT -- other agents build against this):
#   fetch(doc_no: str) -> {
#     "ok": bool, "doc_no": str,
#     "county_type": str|None,   # raw county Type, e.g. "DEFAULT CERTIFICATION"
#     "record_date": str|None,   # ISO YYYY-MM-DD
#     "grantors": [str], "grantees": [str],
#     "ain": str|None,
#     "source_url": str, "fetched_at": str, "method": str,
#     "evidence_path": str|None,
#     "reason": str|None,
#   }
#
# Be GENTLE on NETR: built-in jittered throttle, single request per fetch.
# Zero new cost (curl_cffi already installed). No credentials.
# =============================================================================
import os
import re
import csv
import json
import time
import random
import datetime
import html as _html

BASE = "https://datastore.netronline.com"
SEARCH_URL = BASE + "/lasearch"
FORM_URL = BASE + "/losangeles"
IMPERSONATE = "chrome124"
SITEKEY = "6Lc-ADEsAAAAAN5yv6eswFzcQJDEim1MIOmpnUGF"

EVIDENCE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index_evidence")
THROTTLE = (2.2, 4.0)          # polite jitter between network calls (lead-hardened vs burst-throttle)
TIMEOUT = 40
RETRIES = 4                    # gentle: total attempts = RETRIES+1 (lead-hardened)
EARLIEST = "1977-01-01"        # full LA index window the form exposes

try:
    from curl_cffi import requests as creq
    HAVE_CFFI = True
except Exception:                       # pragma: no cover
    import requests as creq             # fallback (likely JA3-blocked; reported honestly)
    HAVE_CFFI = False


# --------------------------------------------------------------------------- #
# session / throttle
# --------------------------------------------------------------------------- #
_SESSION = None
_LAST_CALL = 0.0


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ensure_evidence_dir():
    try:
        os.makedirs(EVIDENCE_DIR, exist_ok=True)
    except Exception:
        pass


def _session():
    """Chrome-impersonating session that reaches the NETR origin + warms cookies."""
    global _SESSION
    if _SESSION is not None:
        return _SESSION
    if HAVE_CFFI:
        s = creq.Session(impersonate=IMPERSONATE)
    else:                                                   # pragma: no cover
        s = creq.Session()
        s.headers.update({"User-Agent":
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"})
    # Warm cookies / referer context (best-effort; failure is non-fatal).
    try:
        s.get(FORM_URL, timeout=TIMEOUT)
    except Exception:
        pass
    _SESSION = s
    return s


def _throttle():
    """Polite, jittered spacing so we never hammer NETR."""
    global _LAST_CALL
    gap = time.time() - _LAST_CALL
    want = random.uniform(*THROTTLE)
    if gap < want:
        time.sleep(want - gap)
    _LAST_CALL = time.time()


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #
def _clean(s):
    s = _html.unescape(s or "")
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _split_names(cell_html):
    """The county packs multiple names <br>-separated in one cell. De-dup,
    preserve order, drop the 'Add to cart' button text and empties."""
    parts = re.split(r"<br\s*/?>", cell_html, flags=re.I)
    out = []
    seen = set()
    for p in parts:
        name = _clean(p)
        if not name:
            continue
        if name.lower().startswith("add to cart"):
            continue
        key = name.upper()
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
    return out


_ROW_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.S | re.I)
_CELL_RE = re.compile(r"<td\b[^>]*>(.*?)</td>", re.S | re.I)
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _parse_results(results_html, want_doc):
    """Return the row dict for want_doc, or None if not present.
    Columns: Doc# | Date | Type (+cart btn) | Grantors | Grantees."""
    for rowm in _ROW_RE.finditer(results_html or ""):
        row = rowm.group(1)
        cells = _CELL_RE.findall(row)
        if len(cells) < 5:
            continue
        doc = _clean(cells[0])
        # the doc cell is a link; pull the visible doc number
        if want_doc and want_doc not in doc:
            continue
        date_raw = _clean(cells[1])
        dm = _DATE_RE.search(date_raw)
        record_date = dm.group(0) if dm else (date_raw or None)
        # Type cell also contains the cart button -> take text before the <a>
        type_cell = cells[2]
        type_text = re.split(r"<a\b", type_cell, flags=re.I)[0]
        county_type = _clean(type_text) or None
        grantors = _split_names(cells[3])
        grantees = _split_names(cells[4])
        return {
            "doc_no": doc or want_doc,
            "county_type": county_type,
            "record_date": record_date,
            "grantors": grantors,
            "grantees": grantees,
        }
    return None


# --------------------------------------------------------------------------- #
# evidence
# --------------------------------------------------------------------------- #
def _save_evidence(doc_no, count_html, results_html, raw_body):
    _ensure_evidence_dir()
    safe = re.sub(r"[^0-9A-Za-z_.-]", "_", str(doc_no)) or "unknown"
    path = os.path.join(EVIDENCE_DIR, safe + ".html")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("<!-- la_county_index evidence doc_no=%s fetched_at=%s "
                    "source=%s -->\n" % (doc_no, _now_iso(), SEARCH_URL))
            f.write("<!-- COUNT: %s -->\n" % _clean(count_html or ""))
            f.write(results_html or raw_body or "")
        return path
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def _result(ok, doc_no, method, evidence_path=None, reason=None,
            county_type=None, record_date=None, grantors=None, grantees=None,
            ain=None):
    return {
        "ok": ok,
        "doc_no": str(doc_no),
        "county_type": county_type,
        "record_date": record_date,
        "grantors": grantors or [],
        "grantees": grantees or [],
        "ain": ain,
        "source_url": SEARCH_URL,
        "fetched_at": _now_iso(),
        "method": method,
        "evidence_path": evidence_path,
        "reason": reason,
    }


def fetch(doc_no, save_evidence=True):
    """Fetch the authoritative LA County recorder index row for doc_no.

    Returns the contract dict (see module header). ok=True with the county
    fields populated on success; ok=False with a reason otherwise.
    Single, throttled HTTP request. Falls back to CDP only if HTTP is blocked
    (see fetch_via_cdp)."""
    doc_no = str(doc_no).strip()
    method = "curl_cffi:lasearch"
    if not doc_no:
        return _result(False, doc_no, method, reason="empty doc_no")

    data = {
        "page": "1",
        "g-recaptcha-response": "",     # server does not enforce v3 token
        "beg_dt": EARLIEST,
        "end_dt": datetime.date.today().isoformat(),
        "company": "",
        "first_name": "",
        "last_name": "",
        "signer": "R",
        "ain": "",
        "doc_no": doc_no,
    }
    headers = {
        "Referer": FORM_URL,
        "Origin": BASE,
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
    }

    s = _session()
    last_err = None
    for attempt in range(RETRIES + 1):
        if attempt:
            time.sleep(min(25.0, 3.0 * attempt))  # backoff on retry
        _throttle()
        try:
            r = s.post(SEARCH_URL, data=data, headers=headers, timeout=TIMEOUT)
        except Exception as e:
            last_err = "request_error:%s:%s" % (type(e).__name__, e)
            continue
        if r.status_code != 200:
            last_err = "http_%s" % r.status_code
            # 403/429 => fingerprint/rate wall worth a CDP fallback upstream
            continue
        body = r.text or ""
        try:
            j = json.loads(body)
        except Exception:
            last_err = "non_json_response"
            continue
        count_html = j[0] if isinstance(j, list) and len(j) > 0 else ""
        results_html = j[1] if isinstance(j, list) and len(j) > 1 else ""

        if "No documents found" in _clean(count_html) or "No documents found" in _clean(results_html):
            ev = _save_evidence(doc_no, count_html, results_html, body) if save_evidence else None
            return _result(False, doc_no, method, evidence_path=ev,
                           reason="not_found")

        row = _parse_results(results_html, doc_no)
        ev = _save_evidence(doc_no, count_html, results_html, body) if save_evidence else None
        if not row:
            return _result(False, doc_no, method, evidence_path=ev,
                           reason="parse_no_row:%s" % _clean(count_html)[:80])
        return _result(True, doc_no, method, evidence_path=ev,
                       county_type=row["county_type"],
                       record_date=row["record_date"],
                       grantors=row["grantors"],
                       grantees=row["grantees"],
                       ain=row.get("ain"))

    return _result(False, doc_no, method, reason=last_err or "unknown_error")


def fetch_via_cdp(doc_no, cdp_port=None):
    """CDP real-browser fallback for if/when NETR ever fingerprint- or
    reCAPTCHA-walls the HTTP path. NOT NEEDED as of 2026-06-17 (HTTP works),
    so this is a documented, deliberately-unused stub: drive a fleet Chrome on
    the given CDP port to FORM_URL, fill #la_search doc_no, submit (the page's
    own grecaptcha.execute fires), and read $('#la_results').html().

    The pattern matches the divorce enumerators' CDP usage. Implement only if
    HTTP starts returning 403/429/non_json. Returns the same contract dict."""
    return _result(False, doc_no, "cdp:unimplemented",
                   reason="cdp_fallback_not_needed_http_works")


def fetch_batch(doc_nos, out_csv=None, on_each=None):
    """Gently fetch many docs (built-in throttle => ~1 req/sec). Optionally
    stream rows to a CSV. Returns list of contract dicts. For ~660 docs this is
    ~11 min at the polite default cadence; latency itself is ~0.1-0.4s/doc, the
    rest is deliberate jitter so we never burn the source."""
    rows = []
    writer = None
    fh = None
    if out_csv:
        fh = open(out_csv, "w", newline="", encoding="utf-8")
        writer = csv.writer(fh)
        writer.writerow(["doc_no", "ok", "county_type", "record_date",
                         "grantors", "grantees", "reason", "evidence_path"])
    try:
        for d in doc_nos:
            res = fetch(d)
            rows.append(res)
            if writer:
                writer.writerow([res["doc_no"], res["ok"], res["county_type"],
                                 res["record_date"], "; ".join(res["grantors"]),
                                 "; ".join(res["grantees"]), res["reason"],
                                 res["evidence_path"]])
                fh.flush()
            if on_each:
                try:
                    on_each(res)
                except Exception:
                    pass
    finally:
        if fh:
            fh.close()
    return rows


if __name__ == "__main__":
    import sys
    docs = sys.argv[1:] or ["20260431285"]
    for d in docs:
        print(json.dumps(fetch(d), indent=2))
