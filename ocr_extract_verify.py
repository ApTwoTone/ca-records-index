#!/usr/bin/env python3
"""ocr_extract_verify.py -- OCR recorded-doc page images, extract APN + property
address, then TWO-SOURCE VERIFY each APN against the LA Assessor authoritative
index (APN -> situs address). An OCR'd APN is only sealed 'verified' if the
Assessor resolves it; the Assessor's address corroborates/corrects the OCR address.

Source 1 = the recorded document (OCR).
Source 2 = LA County Assessor portal reverse lookup (authoritative).

Usage:
  python3 ocr_extract_verify.py <png_dir> <index_csv> <out_csv> [--no-verify]

Writes one row per doc: doc_no, lead_class, grantors, ocr_apn, ocr_address,
assessor_apn, assessor_address, apn_verified, address_match, verdict.
"""
import sys, os, re, csv, json, glob, subprocess, time
import urllib.request, urllib.error, ssl

CTX = ssl.create_default_context()
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

# ---- OCR via the compiled Swift Vision binary (/tmp/ocr) ----
def ocr_pages(paths):
    if not paths:
        return ""
    try:
        out = subprocess.check_output(["/tmp/ocr"] + paths, stderr=subprocess.DEVNULL, timeout=120)
        return out.decode("utf-8", "replace")
    except Exception:
        return ""

# ---- APN extraction (LA APN = 4-3-3 digits, written many ways) ----
_APN_LABEL = re.compile(r"(?:A\.?P\.?N\.?|ASSESSOR'?S?\s+PARCEL(?:\s+(?:NO|NUMBER))?|PARCEL\s+(?:NO|NUMBER)|TAX\s+ID)\.?\s*[:#]?\s*([0-9]{4})[\s\-]?([0-9]{3})[\s\-]?([0-9]{3})", re.I)
_APN_BARE = re.compile(r"\b([0-9]{4})-([0-9]{3})-([0-9]{3})\b")

def extract_apn(text):
    m = _APN_LABEL.search(text)
    if m:
        return "%s-%s-%s" % (m.group(1), m.group(2), m.group(3))
    # bare 4-3-3 with dashes (avoid TS#/loan# which are usually not dashed in 4-3-3)
    m = _APN_BARE.search(text)
    if m:
        return "%s-%s-%s" % (m.group(1), m.group(2), m.group(3))
    return ""

# ---- Address extraction (street + city/CA/zip on the doc face) ----
_ADDR = re.compile(
    r"\b(\d{1,6}\s+[0-9A-Za-z .'#-]{2,40}?\b(?:ST|STREET|AVE|AVENUE|DR|DRIVE|RD|ROAD|BLVD|BOULEVARD|LN|LANE|CT|COURT|PL|PLACE|WAY|CIR|CIRCLE|TER|TERRACE|HWY|PKWY|PARKWAY)\b\.?)"
    r"(?:[,\s]+([A-Z][A-Za-z .]{2,30}?))?(?:[,\s]+(CA|CALIFORNIA))?\s*(9\d{4})?",
    re.I)

def extract_address(text):
    # prefer a line that also has a zip; skip the law-firm/trustee mailing block
    best = ""
    for m in _ADDR.finditer(text):
        cand = m.group(0).strip()
        # skip obvious trustee/law-office suite addresses
        low = cand.lower()
        if any(k in low for k in ("suite", "ste ", "p.o", "po box", "corporate park")):
            continue
        if m.group(4):  # has zip -> strong
            return re.sub(r"\s+", " ", cand)
        if not best:
            best = re.sub(r"\s+", " ", cand)
    return best

# ---- Source 2: LA Assessor authoritative APN -> address ----
def assessor_lookup(apn):
    """Source 2 (authoritative): LA County Assessor portal parceldetail by AIN.
    GET /api/parceldetail?ain=<10digits> -> {Parcel:{AIN,SitusStreet,SitusCity,
    SitusZipCode,UseType,...}}. Returns (address|'', ok_bool, use_type)."""
    ain = apn.replace("-", "")
    if len(ain) != 10:
        return ("", False, "")
    url = "https://portal.assessor.lacounty.gov/api/parceldetail?ain=%s" % ain
    for att in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=25, context=CTX) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            par = data.get("Parcel") if isinstance(data, dict) else None
            if not par:
                return ("", False, "")
            if str(par.get("AIN") or "").replace("-", "") != ain:
                return ("", False, "")
            street = (par.get("SitusStreet") or "").strip()
            city = (par.get("SitusCity") or "").strip()
            zc = (par.get("SitusZipCode") or "").strip()
            addr = ", ".join([p for p in [street, city, zc] if p])
            return (addr, True, (par.get("UseType") or "").strip())
        except urllib.error.HTTPError as e:
            if e.code in (429, 503):
                time.sleep(1.5 * (att + 1)); continue
            return ("", False, "")
        except Exception:
            time.sleep(1.0 * (att + 1))
    return ("", False, "")

def _norm_addr(a):
    return re.sub(r"[^a-z0-9 ]", "", (a or "").lower()).strip()

def main():
    png_dir, index_csv, out_csv = sys.argv[1], sys.argv[2], sys.argv[3]
    verify = "--no-verify" not in sys.argv

    # map doc_no -> (lead_class, grantors)
    meta = {}
    with open(index_csv, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            meta[r["doc_no"]] = (r.get("lead_class") or "", r.get("grantors") or "")

    # group png pages by doc
    pages_by_doc = {}
    for p in glob.glob(os.path.join(png_dir, "**", "*.png"), recursive=True):
        base = os.path.basename(p)
        m = re.match(r"(\d+)_(\d+)\.png$", base)
        if not m:
            continue
        pages_by_doc.setdefault(m.group(1), []).append((int(m.group(2)), p))

    rows = []
    n = 0
    for doc in sorted(pages_by_doc):
        paths = [p for _, p in sorted(pages_by_doc[doc])]
        text = ocr_pages(paths)
        ocr_apn = extract_apn(text)
        ocr_addr = extract_address(text)
        lead, grantors = meta.get(doc, ("", ""))
        ass_addr, ass_ok, use_type = ("", False, "")
        if verify and ocr_apn:
            ass_addr, ass_ok, use_type = assessor_lookup(ocr_apn)
            time.sleep(0.4)
        apn_verified = bool(ocr_apn and ass_ok)
        addr_match = bool(ocr_addr and ass_addr and
                          (_norm_addr(ocr_addr)[:12] in _norm_addr(ass_addr)
                           or _norm_addr(ass_addr)[:12] in _norm_addr(ocr_addr)))
        if apn_verified and addr_match:
            verdict = "VERIFIED_BOTH"
        elif apn_verified:
            verdict = "APN_VERIFIED_ASSESSOR_ADDR"   # trust assessor address
        elif ocr_apn:
            verdict = "OCR_ONLY_UNVERIFIED"
        else:
            verdict = "NO_APN_FOUND"
        rows.append({
            "doc_no": doc, "lead_class": lead, "grantors": grantors,
            "ocr_apn": ocr_apn, "ocr_address": ocr_addr,
            "assessor_apn": ocr_apn if ass_ok else "",
            "assessor_address": ass_addr,
            "use_type": use_type,
            "apn_verified": apn_verified, "address_match": addr_match,
            "verdict": verdict,
        })
        n += 1
        if n % 25 == 0:
            print("processed %d/%d docs" % (n, len(pages_by_doc)), flush=True)

    cols = ["doc_no", "lead_class", "grantors", "ocr_apn", "ocr_address",
            "assessor_apn", "assessor_address", "use_type",
            "apn_verified", "address_match", "verdict"]
    with open(out_csv, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow(r)

    # summary
    from collections import Counter
    vc = Counter(r["verdict"] for r in rows)
    got_apn = sum(1 for r in rows if r["ocr_apn"])
    print("\n=== OCR + 2-SOURCE VERIFY SUMMARY ===")
    print("docs processed: %d" % len(rows))
    print("APN extracted by OCR: %d" % got_apn)
    print("APN verified vs Assessor: %d" % sum(1 for r in rows if r["apn_verified"]))
    for v, c in vc.most_common():
        print("  %4d  %s" % (c, v))
    print("out: %s" % out_csv)

if __name__ == "__main__":
    main()
