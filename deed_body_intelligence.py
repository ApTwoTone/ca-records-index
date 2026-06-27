#!/usr/bin/env python3
"""Extract buyer/seller intelligence signals from recorded-document page OCR.

This is not a contact list builder. It converts public recorded-document page
images into structured intelligence sidecars for later proof, trend, and buyer
profile work. Raw OCR text is preserved beside the CSV so international mailing
blocks and deed-body domicile clauses can be re-reviewed instead of being
over-normalized into bad addresses.

Usage:
  python3 deed_body_intelligence.py <png_dir> <index_csv> <out_dir>

Inputs:
  png_dir   Directory containing NETR page PNGs named <doc_no>_<page>.png.
  index_csv AIN docs CSV or doc_metadata.csv with doc_no plus index fields.
  out_dir   Output directory for CSV, JSON summary, and raw OCR text files.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Iterable


ENTITY_TYPES = (
    "CORPORATION",
    "CORP",
    "INCORPORATED",
    "INC",
    "LIMITED LIABILITY COMPANY",
    "LIMITED PARTNERSHIP",
    "GENERAL PARTNERSHIP",
    "PARTNERSHIP",
    "COMPANY",
    "CO",
    "LIMITED",
    "LTD",
    "LLC",
    "LP",
    "LLP",
    "PLC",
    "PTE LTD",
    "SARL",
    "S A",
    "SA",
    "GMBH",
    "BV",
    "NV",
    "TRUST",
)

US_STATES = {
    "ALABAMA", "ALASKA", "ARIZONA", "ARKANSAS", "CALIFORNIA", "COLORADO",
    "CONNECTICUT", "DELAWARE", "FLORIDA", "GEORGIA", "HAWAII", "IDAHO",
    "ILLINOIS", "INDIANA", "IOWA", "KANSAS", "KENTUCKY", "LOUISIANA",
    "MAINE", "MARYLAND", "MASSACHUSETTS", "MICHIGAN", "MINNESOTA",
    "MISSISSIPPI", "MISSOURI", "MONTANA", "NEBRASKA", "NEVADA",
    "NEW HAMPSHIRE", "NEW JERSEY", "NEW MEXICO", "NEW YORK",
    "NORTH CAROLINA", "NORTH DAKOTA", "OHIO", "OKLAHOMA", "OREGON",
    "PENNSYLVANIA", "RHODE ISLAND", "SOUTH CAROLINA", "SOUTH DAKOTA",
    "TENNESSEE", "TEXAS", "UTAH", "VERMONT", "VIRGINIA", "WASHINGTON",
    "WEST VIRGINIA", "WISCONSIN", "WYOMING", "DISTRICT OF COLUMBIA",
    "USA", "UNITED STATES", "UNITED STATES OF AMERICA",
}

JURISDICTION_ALIASES = [
    (r"\bHONG\s+KONG(?:\s+SAR)?\b", "Hong Kong SAR", "asia"),
    (r"\bJAPAN\b", "Japan", "asia"),
    (r"\bCHINA\b|\bPRC\b|\bPEOPLE'?S\s+REPUBLIC\s+OF\s+CHINA\b", "China", "asia"),
    (r"\bTAIWAN\b", "Taiwan", "asia"),
    (r"\bSINGAPORE\b", "Singapore", "asia"),
    (r"\bKOREA\b|\bSOUTH\s+KOREA\b", "South Korea", "asia"),
    (r"\bUNITED\s+KINGDOM\b|\bU\.?K\.?\b|\bENGLAND\b|\bWALES\b|\bSCOTLAND\b", "United Kingdom", "europe"),
    (r"\bFRANCE\b|\bFRENCH\b", "France", "europe"),
    (r"\bGERMANY\b|\bDEUTSCHLAND\b", "Germany", "europe"),
    (r"\bNETHERLANDS\b|\bHOLLAND\b", "Netherlands", "europe"),
    (r"\bLUXEMBOURG\b", "Luxembourg", "europe"),
    (r"\bSWITZERLAND\b", "Switzerland", "europe"),
    (r"\bIRELAND\b", "Ireland", "europe"),
    (r"\bITALY\b", "Italy", "europe"),
    (r"\bSPAIN\b", "Spain", "europe"),
    (r"\bUNITED\s+ARAB\s+EMIRATES\b|\bU\.?A\.?E\.?\b|\bDUBAI\b|\bABU\s+DHABI\b", "United Arab Emirates", "middle_east"),
    (r"\bQATAR\b", "Qatar", "middle_east"),
    (r"\bSAUDI\s+ARABIA\b", "Saudi Arabia", "middle_east"),
    (r"\bKUWAIT\b", "Kuwait", "middle_east"),
    (r"\bBAHRAIN\b", "Bahrain", "middle_east"),
    (r"\bISRAEL\b", "Israel", "middle_east"),
    (r"\bLEBANON\b", "Lebanon", "middle_east"),
    (r"\bJORDAN\b", "Jordan", "middle_east"),
    (r"\bCANADA\b", "Canada", "north_america"),
    (r"\bMEXICO\b", "Mexico", "north_america"),
    (r"\bBRITISH\s+VIRGIN\s+ISLANDS\b|\bBVI\b", "British Virgin Islands", "caribbean"),
    (r"\bCAYMAN\s+ISLANDS\b", "Cayman Islands", "caribbean"),
    (r"\bBERMUDA\b", "Bermuda", "caribbean"),
    (r"\bAUSTRALIA\b", "Australia", "oceania"),
    (r"\bNEW\s+ZEALAND\b", "New Zealand", "oceania"),
]

MINERAL_TERMS = [
    "MINERAL", "MINING", "PLACER", "LODE", "PATENTED", "CLAIM", "OIL",
    "GAS", "HYDROCARBON", "ROYALTY", "WATER RIGHTS", "TIMBER", "EASEMENT",
]

APN_RE = re.compile(
    r"\b(?:APN|AIN|A\.?P\.?N\.?|PARCEL\s+(?:NO|NUMBER)|ASSESSOR'?S?\s+PARCEL)\s*[:#]?\s*"
    r"([0-9]{4})[\s\-]?([0-9]{3})[\s\-]?([0-9]{3})\b",
    re.I,
)
APN_BARE_RE = re.compile(r"\b([0-9]{4})-([0-9]{3})-([0-9]{3})\b")
TRANSFER_TAX_RE = re.compile(
    r"(DOCUMENTARY\s+TRANSFER\s+TAX|CITY\s+TRANSFER\s+TAX|CITY\s+TAX)\s*[:$# ]*\s*(NONE|NO\s*TAX|[0-9][0-9,]*(?:\.[0-9]{1,2})?)",
    re.I,
)
COMPANY_NO_RE = re.compile(r"\b(?:COMPANY|REGISTRATION|ENTITY|FILE)\s+(?:NO|NUMBER|#)\.?\s*[:#]?\s*([A-Z0-9\-]{3,30})\b", re.I)
OCR_CONFIDENCE_PREFIX_RE = re.compile(r"(?m)^\s*(?:0|1)(?:\.\d{1,3})?\s+")


def now_utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def clean_ws(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def upper_blob(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").upper())


def strip_ocr_confidence_prefixes(text: str) -> str:
    """Apple Vision wrappers often prefix each OCR line with confidence like 1.00."""
    return OCR_CONFIDENCE_PREFIX_RE.sub("", text or "")


def split_jsonish(value: str) -> list[str]:
    value = (value or "").strip()
    if not value:
        return []
    try:
        parsed = json.loads(value)
        values = parsed if isinstance(parsed, list) else [parsed]
        flattened = []
        for item in values:
            if isinstance(item, str) and item.strip().startswith("["):
                flattened.extend(split_jsonish(item))
            else:
                cleaned = clean_ws(str(item))
                if cleaned:
                    flattened.append(cleaned)
        return flattened
    except Exception:
        pass
    return [clean_ws(v) for v in re.split(r";|\|", value) if clean_ws(v)]


def join_unique(values: Iterable[str]) -> str:
    out = []
    seen = set()
    for value in values:
        value = clean_ws(str(value))
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return json.dumps(out, ensure_ascii=False)


def load_index(path: Path) -> dict[str, dict[str, str]]:
    by_doc: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            doc = clean_ws(row.get("doc_no") or row.get("document_no") or "")
            if not doc:
                continue
            for field in ["ain", "record_date", "county_type", "grantors", "grantees", "lead_class"]:
                raw = row.get(field) or ""
                if field in {"ain", "record_date", "county_type", "lead_class", "grantors", "grantees"}:
                    for item in split_jsonish(raw):
                        by_doc[doc][field].add(item)
    return {
        doc: {field: join_unique(sorted(values)) for field, values in fields.items()}
        for doc, fields in by_doc.items()
    }


def group_pages(png_dir: Path) -> dict[str, list[Path]]:
    pages: dict[str, list[tuple[int, Path]]] = defaultdict(list)
    for path in png_dir.glob("**/*.png"):
        match = re.match(r"(\d+)_(\d+)\.png$", path.name)
        if not match:
            continue
        pages[match.group(1)].append((int(match.group(2)), path))
    return {doc: [path for _, path in sorted(items)] for doc, items in pages.items()}


def ocr_one(path: Path, ocr_bin: str | None) -> tuple[str, str]:
    if ocr_bin and Path(ocr_bin).exists():
        try:
            proc = subprocess.run([ocr_bin, str(path)], text=True, capture_output=True, timeout=120)
            if proc.returncode == 0:
                return proc.stdout, "ok_vision"
            return proc.stdout or proc.stderr, f"vision_exit_{proc.returncode}"
        except Exception as exc:
            return "", f"vision_error_{type(exc).__name__}"

    tess = shutil.which("tesseract")
    if tess:
        try:
            proc = subprocess.run([tess, str(path), "stdout", "--psm", "6"], text=True, capture_output=True, timeout=120)
            if proc.returncode == 0:
                return proc.stdout, "ok_tesseract"
            return proc.stdout or proc.stderr, f"tesseract_exit_{proc.returncode}"
        except Exception as exc:
            return "", f"tesseract_error_{type(exc).__name__}"

    return "", "ocr_engine_missing"


def ocr_pages(paths: list[Path], ocr_bin: str | None) -> tuple[str, list[str]]:
    chunks = []
    statuses = []
    for path in paths:
        text, status = ocr_one(path, ocr_bin)
        statuses.append(status)
        chunks.append(f"\n\n--- PAGE {path.name} OCR_STATUS={status} ---\n{text}")
    return "\n".join(chunks), statuses


def extract_apns(text: str) -> list[str]:
    found = []
    for rx in (APN_RE, APN_BARE_RE):
        for match in rx.finditer(text or ""):
            found.append("%s-%s-%s" % (match.group(1), match.group(2), match.group(3)))
    return list(dict.fromkeys(found))


def canonical_jurisdiction(raw: str) -> tuple[str, str, bool]:
    blob = upper_blob(raw)
    for pattern, canonical, region in JURISDICTION_ALIASES:
        if re.search(pattern, blob, re.I):
            return canonical, region, True
    blob = re.sub(r"[^A-Z ]", " ", blob)
    blob = re.sub(r"\s+", " ", blob).strip()
    if blob in US_STATES:
        return blob.title(), "us", False
    # Unknown free text is kept for manual review but must not inflate foreign
    # counts. OCR can over-capture clause fragments such as "the following
    # described real property ... SAR corporation".
    return clean_ws(raw).title(), "unknown", False


def extract_entity_domiciles(text: str) -> list[dict[str, str]]:
    blob = upper_blob(text)
    etype = r"(?:%s)" % "|".join(re.escape(t) for t in sorted(ENTITY_TYPES, key=len, reverse=True))
    patterns = [
        re.compile(
            r"(?P<entity>[A-Z0-9][A-Z0-9 .,'&()/\\-]{2,120}?),?\s+"
            r"(?:A|AN)\s+(?P<jurisdiction>[A-Z][A-Z .'-]{2,60}?)\s+"
            r"(?P<entity_type>%s)\b" % etype,
            re.I,
        ),
        re.compile(
            r"(?P<entity>[A-Z0-9][A-Z0-9 .,'&()/\\-]{2,120}?)\s+"
            r"(?P<entity_type>%s),?\s+(?:A|AN)\s+"
            r"(?P<jurisdiction>[A-Z][A-Z .'-]{2,60}?)\s+(?:ENTITY|COMPANY|CORPORATION)\b" % etype,
            re.I,
        ),
    ]
    out = []
    seen = set()
    for rx in patterns:
        for match in rx.finditer(blob):
            entity = clean_ws(match.group("entity").strip(" ,.;:"))
            jurisdiction_raw = clean_ws(match.group("jurisdiction").strip(" ,.;:"))
            entity_type = clean_ws(match.group("entity_type").strip(" ,.;:"))
            if len(entity) < 3 or len(jurisdiction_raw) < 2:
                continue
            canonical, region, is_foreign = canonical_jurisdiction(jurisdiction_raw)
            phrase = clean_ws(match.group(0).strip(" ,.;:"))
            key = (entity, canonical, entity_type, phrase)
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "entity": entity,
                "jurisdiction_raw": jurisdiction_raw,
                "jurisdiction": canonical,
                "region": region,
                "is_foreign": str(bool(is_foreign)).lower(),
                "entity_type": entity_type,
                "phrase": phrase,
            })
    return out


def extract_block(lines: list[str], starts: tuple[str, ...], max_lines: int = 12) -> str:
    starts_upper = tuple(s.upper() for s in starts)
    blocks = []
    for i, line in enumerate(lines):
        up = line.upper()
        if any(s in up for s in starts_upper):
            block = [line]
            for nxt in lines[i + 1:i + 1 + max_lines]:
                clean = nxt.strip()
                if not clean:
                    if len(block) > 1:
                        break
                    continue
                if re.search(r"^(SPACE ABOVE|DOCUMENTARY|THIS CONVEYANCE|GRANTOR|GRANTEE|EXHIBIT|LEGAL DESCRIPTION)\b", clean, re.I):
                    break
                block.append(clean)
            blocks.append(" | ".join(clean_ws(x) for x in block if clean_ws(x)))
    for block in blocks:
        if country_from_block(block)[1]:
            return block
    return blocks[0] if blocks else ""


def country_from_block(block: str) -> tuple[str, bool]:
    if not block:
        return "", False
    hits = []
    for pattern, canonical, _region in JURISDICTION_ALIASES:
        if re.search(pattern, block, re.I):
            hits.append(canonical)
    if hits:
        return hits[0], True
    return "", False


def extract_body_grantee(text: str) -> str:
    match = re.search(
        r"\bGRANTS?\s+TO:?\s*(?P<party>.{5,240}?)(?:\bTHE\s+FOLLOWING\b|\bTHE\s+REAL\b|\bREAL\s+PROPERTY\b|\bSITUATED\b)",
        text,
        re.I | re.S,
    )
    if not match:
        return ""
    return clean_ws(match.group("party")).strip(" ,.;:")


def extract_transfer_taxes(text: str) -> tuple[str, str, str]:
    values = []
    estimates = []
    for label, raw in TRANSFER_TAX_RE.findall(text or ""):
        raw_clean = clean_ws(raw)
        values.append(f"{clean_ws(label)}={raw_clean}")
        number = re.sub(r"[^0-9.]", "", raw_clean)
        if number:
            try:
                tax = float(number)
            except ValueError:
                continue
            if tax > 0:
                estimates.append(round(tax / 0.0011, 2))
    confidence = "not_estimated"
    if estimates:
        confidence = "low_county_tax_rate_only_verify_city_exemptions"
    return "; ".join(values), json.dumps(estimates), confidence


def extract_company_numbers(text: str) -> list[str]:
    return list(dict.fromkeys(clean_ws(m.group(1)) for m in COMPANY_NO_RE.finditer(text or "")))


def entity_suffix_flag(values: Iterable[str]) -> bool:
    blob = upper_blob(" ".join(values))
    return any(re.search(r"\b%s\b" % re.escape(t), blob) for t in ENTITY_TYPES)


def analyze_text(doc: str, text: str, meta: dict[str, str], text_path: Path, statuses: list[str]) -> dict[str, str]:
    analysis_text = strip_ocr_confidence_prefixes(text)
    lines = [clean_ws(line) for line in analysis_text.splitlines() if clean_ws(line)]
    text_sha = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
    domiciles = extract_entity_domiciles(analysis_text)
    foreign = [d for d in domiciles if d["is_foreign"] == "true"]
    jurisdictions = [d["jurisdiction"] for d in foreign]
    recording_block = extract_block(lines, ("RECORDING REQUESTED BY", "REQUESTED BY"))
    mail_block = extract_block(lines, ("WHEN RECORDED MAIL TO", "MAIL TAX STATEMENTS TO", "MAIL TO"))
    mail_country, mail_foreign = country_from_block(mail_block)
    apns = extract_apns(analysis_text)
    body_grantee = extract_body_grantee(analysis_text)
    taxes_raw, estimates_json, estimate_conf = extract_transfer_taxes(analysis_text)
    company_numbers = extract_company_numbers(analysis_text)
    upper = upper_blob(analysis_text)
    mineral_hits = [term for term in MINERAL_TERMS if term in upper]
    trust_signal = bool(re.search(r"\bTRUSTEE\b|\bTRUST\b|\bTRUSTOR\b|\bBENEFICIARY\b", upper))
    corp_party = entity_suffix_flag(split_jsonish(meta.get("grantors", "")) + split_jsonish(meta.get("grantees", "")))
    tags = []
    if foreign:
        tags.append("foreign_entity_domicile_clause")
    if mail_foreign:
        tags.append("international_mail_block")
    if mineral_hits:
        tags.append("mineral_or_resource_rights")
    if corp_party:
        tags.append("corporate_party_from_index")
    if company_numbers:
        tags.append("company_registration_number")
    if trust_signal:
        tags.append("trust_or_trustee_language")
    if estimates_json != "[]":
        tags.append("transfer_tax_price_proxy_low_confidence")
    if any(s.startswith("ocr_engine_missing") for s in statuses):
        tags.append("ocr_engine_missing")

    return {
        "doc_no": doc,
        "index_ains": meta.get("ain", "[]"),
        "index_record_dates": meta.get("record_date", "[]"),
        "index_county_types": meta.get("county_type", "[]"),
        "index_grantors": meta.get("grantors", "[]"),
        "index_grantees": meta.get("grantees", "[]"),
        "ocr_status": "ok" if any(s.startswith("ok_") for s in statuses) else (statuses[0] if statuses else "no_pages"),
        "ocr_engines": join_unique(statuses),
        "pages_ocrd": str(len(statuses)),
        "ocr_text_path": str(text_path),
        "ocr_text_sha256": text_sha,
        "ocr_chars": str(len(text)),
        "apns_all": join_unique(apns),
        "recording_requested_by_raw": recording_block,
        "mail_to_raw": mail_block,
        "mail_to_country": mail_country,
        "mail_to_international_flag": str(mail_foreign).lower(),
        "body_grantee_raw": body_grantee,
        "entity_domicile_phrases": json.dumps(domiciles, ensure_ascii=False),
        "foreign_entity_jurisdictions": join_unique(jurisdictions),
        "foreign_entity_flag": str(bool(foreign)).lower(),
        "company_numbers": join_unique(company_numbers),
        "transfer_tax_raw": taxes_raw,
        "estimated_consideration_from_county_tax": estimates_json,
        "consideration_confidence": estimate_conf,
        "mineral_rights_signal": str(bool(mineral_hits)).lower(),
        "mineral_terms": join_unique(mineral_hits),
        "trustee_trust_signal": str(trust_signal).lower(),
        "corporate_party_from_index_flag": str(corp_party).lower(),
        "buyer_seller_intel_tags": join_unique(tags),
    }


FIELDS = [
    "doc_no", "index_ains", "index_record_dates", "index_county_types",
    "index_grantors", "index_grantees", "ocr_status", "ocr_engines",
    "pages_ocrd", "ocr_text_path", "ocr_text_sha256", "ocr_chars", "apns_all",
    "recording_requested_by_raw", "mail_to_raw", "mail_to_country",
    "mail_to_international_flag", "body_grantee_raw", "entity_domicile_phrases",
    "foreign_entity_jurisdictions", "foreign_entity_flag", "company_numbers",
    "transfer_tax_raw", "estimated_consideration_from_county_tax",
    "consideration_confidence", "mineral_rights_signal", "mineral_terms",
    "trustee_trust_signal", "corporate_party_from_index_flag",
    "buyer_seller_intel_tags",
]


def run(png_dir: Path, index_csv: Path, out_dir: Path, ocr_bin: str | None) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    text_dir = out_dir / "ocr_text"
    text_dir.mkdir(parents=True, exist_ok=True)
    meta_by_doc = load_index(index_csv)
    pages_by_doc = group_pages(png_dir)
    rows = []
    status_counts = Counter()
    tag_counts = Counter()
    for doc, paths in sorted(pages_by_doc.items(), key=lambda item: int(item[0]) if item[0].isdigit() else item[0]):
        text, statuses = ocr_pages(paths, ocr_bin)
        text_path = text_dir / f"{doc}.txt"
        text_path.write_text(text, encoding="utf-8")
        row = analyze_text(doc, text, meta_by_doc.get(doc, {}), text_path, statuses)
        rows.append(row)
        status_counts[row["ocr_status"]] += 1
        for tag in json.loads(row["buyer_seller_intel_tags"]):
            tag_counts[tag] += 1

    csv_path = out_dir / "deed_body_intelligence.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    summary = {
        "generated_at_utc": now_utc(),
        "png_dir": str(png_dir),
        "index_csv": str(index_csv),
        "out_dir": str(out_dir),
        "docs_with_pages": len(pages_by_doc),
        "docs_ocrd": len(rows),
        "foreign_entity_docs": sum(1 for r in rows if r["foreign_entity_flag"] == "true"),
        "international_mail_docs": sum(1 for r in rows if r["mail_to_international_flag"] == "true"),
        "mineral_signal_docs": sum(1 for r in rows if r["mineral_rights_signal"] == "true"),
        "ocr_status_counts": dict(status_counts),
        "tag_counts": dict(tag_counts),
        "outputs": {
            "deed_body_intelligence_csv": str(csv_path),
            "ocr_text_dir": str(text_dir),
        },
    }
    (out_dir / "deed_body_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def self_test() -> int:
    sample = """
    RECORDING REQUESTED BY:
    U.S. Petroleum Limited,
    a Hong Kong SAR corporation
    WHEN RECORDED MAIL TO
    MOHAMMED RUSTAM
    RM. 1905-08, 19th Floor
    161 Connaught Road Central
    Hong Kong SAR

    TRUST TRANSFER GRANT DEED
    Mohammed Rustam hereby GRANTS to:
    U.S. Petroleum Limited, a Hong Kong SAR corporation, company number 968292,
    the following described real property in Los Angeles County.
    APN#2848-010-011, APN#2848-010-021, APN#2848-010-022 and APN#2848-011-001.
    patented placer mining claims, mineral, oil and gas rights.
    Documentary Transfer Tax $ None

    Yahirushi Co, Ltd., a Japan Corporation accepts title.
    Example UAE Buyer Ltd, a Dubai company, also appears.
    France Holdings SARL, a France company, appears.
    """
    row = analyze_text("20220482987", sample, {"grantees": json.dumps(["US PETROLEUM LIMITED"])}, Path("ocr_text/20220482987.txt"), ["ok_self_test"])
    assert row["foreign_entity_flag"] == "true", row
    assert "Hong Kong SAR" in row["foreign_entity_jurisdictions"], row
    assert "Japan" in row["foreign_entity_jurisdictions"], row
    assert "United Arab Emirates" in row["foreign_entity_jurisdictions"], row
    assert "France" in row["foreign_entity_jurisdictions"], row
    assert row["mail_to_international_flag"] == "true", row
    assert row["mineral_rights_signal"] == "true", row
    assert "2848-010-011" in row["apns_all"], row
    print("self_test_ok")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("png_dir", nargs="?")
    parser.add_argument("index_csv", nargs="?")
    parser.add_argument("out_dir", nargs="?")
    parser.add_argument("--ocr-bin", default=os.environ.get("OCR_BIN", "/tmp/ocr"))
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    if not args.png_dir or not args.index_csv or not args.out_dir:
        parser.error("png_dir, index_csv, and out_dir are required unless --self-test is used")
    return run(Path(args.png_dir), Path(args.index_csv), Path(args.out_dir), args.ocr_bin)


if __name__ == "__main__":
    raise SystemExit(main())
