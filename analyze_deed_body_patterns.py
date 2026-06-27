#!/usr/bin/env python3
"""Pattern mining over merged deed-body intelligence rows.

Outputs are research/intelligence artifacts, not a buyer call list. Promotion
to outreach remains a separate official-route and compliance workflow.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import argparse
import csv
import datetime
import difflib
import json
from pathlib import Path
import re
from typing import Iterable


READINESS = "deal_packet_rankable_not_callable"


def now_utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def parse_jsonish(value: str) -> list:
    value = clean(value)
    if not value:
        return []
    try:
        parsed = json.loads(value)
        values = parsed if isinstance(parsed, list) else [parsed]
        flattened = []
        for item in values:
            if isinstance(item, str) and item.strip().startswith("["):
                flattened.extend(parse_jsonish(item))
            else:
                flattened.append(item)
        return flattened
    except Exception:
        return [v.strip() for v in re.split(r";|\|", value) if v.strip()]


def join_json(values: Iterable[object]) -> str:
    out = []
    seen = set()
    for value in values:
        if isinstance(value, dict):
            key = json.dumps(value, sort_keys=True)
            item = value
        else:
            item = clean(value)
            key = item.upper()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return json.dumps(out, ensure_ascii=False)


def norm_entity(value: str) -> str:
    value = clean(value).upper()
    value = re.sub(r"[^A-Z0-9 ]", " ", value)
    value = re.sub(r"\b(THE|A|AN)\b", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def compact_entity(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", norm_entity(value))


def canonical_entity(entity: str, known_parties: Iterable[str]) -> str:
    """Prefer index parties when OCR has a clearly matching noisy entity."""
    entity_norm = norm_entity(entity)
    entity_compact = compact_entity(entity)
    best_party = ""
    best_score = 0.0
    for party in known_parties:
        party = clean(party)
        party_norm = norm_entity(party)
        if not party_norm:
            continue
        score = difflib.SequenceMatcher(None, entity_norm, party_norm).ratio()
        if entity_norm and (entity_norm in party_norm or party_norm in entity_norm):
            score = max(score, 0.9)
        if score > best_score:
            best_party = party
            best_score = score
    best_compact = compact_entity(best_party)
    matching_prefix = (
        len(entity_compact) >= 4
        and len(best_compact) >= 4
        and entity_compact[:4] == best_compact[:4]
    )
    if best_score >= 0.72 or (best_score >= 0.66 and matching_prefix):
        return best_party
    return entity


def exact_index_party_rank(domicile: dict[str, object], known_party_norms: set[str]) -> int:
    entity_norm = norm_entity(clean(domicile.get("entity")))
    return 0 if entity_norm in known_party_norms else 1


def date_min(values: Iterable[str]) -> str:
    dates = sorted(v for v in values if re.match(r"^\d{4}-\d{2}-\d{2}$", v or ""))
    return dates[0] if dates else ""


def date_max(values: Iterable[str]) -> str:
    dates = sorted(v for v in values if re.match(r"^\d{4}-\d{2}-\d{2}$", v or ""))
    return dates[-1] if dates else ""


def rows_from_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def write_csv(path: Path, rows: list[dict[str, object]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def iter_foreign_domiciles(row: dict[str, str]):
    for item in parse_jsonish(row.get("entity_domicile_phrases", "")):
        if not isinstance(item, dict):
            continue
        if clean(item.get("is_foreign")) != "true":
            continue
        yield item


def analyze(input_csv: Path, out_dir: Path) -> dict:
    rows = rows_from_csv(input_csv)
    out_dir.mkdir(parents=True, exist_ok=True)

    foreign_candidates = []
    international_mail = []
    resource_candidates = []
    transfer_tax_clues = []
    doc_signal_matrix = []
    jurisdiction_counts = Counter()
    region_counts = Counter()
    tag_counts = Counter()
    entity_rollups = defaultdict(lambda: {
        "entity_display": "",
        "entity_norm": "",
        "jurisdictions": set(),
        "regions": set(),
        "doc_nos": set(),
        "ains": set(),
        "record_dates": set(),
        "county_types": set(),
        "grantors": set(),
        "grantees": set(),
        "tags": set(),
        "phrases": set(),
        "company_numbers": set(),
    })
    candidate_seen = set()

    for row in rows:
        doc_no = clean(row.get("doc_no"))
        tags = [clean(v) for v in parse_jsonish(row.get("buyer_seller_intel_tags", ""))]
        for tag in tags:
            tag_counts[tag] += 1
        ains = [clean(v) for v in parse_jsonish(row.get("index_ains", "")) + parse_jsonish(row.get("apns_all", ""))]
        record_dates = [clean(v) for v in parse_jsonish(row.get("index_record_dates", ""))]
        county_types = [clean(v) for v in parse_jsonish(row.get("index_county_types", ""))]
        grantors = [clean(v) for v in parse_jsonish(row.get("index_grantors", ""))]
        grantees = [clean(v) for v in parse_jsonish(row.get("index_grantees", ""))]

        doc_signal_matrix.append({
            "doc_no": doc_no,
            "foreign_entity_flag": row.get("foreign_entity_flag", ""),
            "foreign_entity_jurisdictions": row.get("foreign_entity_jurisdictions", "[]"),
            "mail_to_international_flag": row.get("mail_to_international_flag", ""),
            "mail_to_country": row.get("mail_to_country", ""),
            "mineral_rights_signal": row.get("mineral_rights_signal", ""),
            "transfer_tax_raw": row.get("transfer_tax_raw", ""),
            "estimated_consideration_from_county_tax": row.get("estimated_consideration_from_county_tax", "[]"),
            "company_numbers": row.get("company_numbers", "[]"),
            "tags": row.get("buyer_seller_intel_tags", "[]"),
            "readiness": READINESS,
        })

        known_party_norms = {norm_entity(p) for p in grantors + grantees if norm_entity(p)}
        domiciles = sorted(
            iter_foreign_domiciles(row),
            key=lambda domicile: (
                exact_index_party_rank(domicile, known_party_norms),
                -len(clean(domicile.get("entity"))),
            ),
        )

        for domicile in domiciles:
            entity = canonical_entity(clean(domicile.get("entity")), grantors + grantees)
            entity_key = norm_entity(entity)
            jurisdiction = clean(domicile.get("jurisdiction"))
            region = clean(domicile.get("region"))
            phrase = clean(domicile.get("phrase"))
            candidate_key = (doc_no, entity_key, jurisdiction)
            if candidate_key not in candidate_seen:
                candidate_seen.add(candidate_key)
                jurisdiction_counts[jurisdiction] += 1
                region_counts[region] += 1
                foreign_candidates.append({
                    "doc_no": doc_no,
                    "entity": entity,
                    "entity_norm": entity_key,
                    "jurisdiction": jurisdiction,
                    "region": region,
                    "entity_type": clean(domicile.get("entity_type")),
                    "phrase": phrase,
                    "index_ains": join_json(ains),
                    "index_record_dates": join_json(record_dates),
                    "index_county_types": join_json(county_types),
                    "index_grantors": join_json(grantors),
                    "index_grantees": join_json(grantees),
                    "company_numbers": row.get("company_numbers", "[]"),
                    "tags": row.get("buyer_seller_intel_tags", "[]"),
                    "ocr_text_path": row.get("ocr_text_path", ""),
                    "commercial_readiness": READINESS,
                    "next_action": "roll_up_entity_then_verify_same_apn_chain_and_official_entity_route",
                })
            if entity_key:
                roll = entity_rollups[entity_key]
                roll["entity_display"] = roll["entity_display"] or entity
                roll["entity_norm"] = entity_key
                roll["jurisdictions"].add(jurisdiction)
                roll["regions"].add(region)
                roll["doc_nos"].add(doc_no)
                roll["ains"].update(ains)
                roll["record_dates"].update(record_dates)
                roll["county_types"].update(county_types)
                roll["grantors"].update(grantors)
                roll["grantees"].update(grantees)
                roll["tags"].update(tags)
                roll["phrases"].add(phrase)
                roll["company_numbers"].update(clean(v) for v in parse_jsonish(row.get("company_numbers", "")))

        if row.get("mail_to_international_flag") == "true":
            international_mail.append({
                "doc_no": doc_no,
                "mail_to_country": row.get("mail_to_country", ""),
                "mail_to_raw": row.get("mail_to_raw", ""),
                "foreign_entity_flag": row.get("foreign_entity_flag", ""),
                "foreign_entity_jurisdictions": row.get("foreign_entity_jurisdictions", "[]"),
                "commercial_readiness": "candidate_only_needs_proof",
                "next_action": "review_raw_ocr_and_page_image_do_not_treat_mailing_country_as_buyer_proof",
            })

        if row.get("mineral_rights_signal") == "true":
            resource_candidates.append({
                "doc_no": doc_no,
                "mineral_terms": row.get("mineral_terms", "[]"),
                "index_ains": join_json(ains),
                "index_record_dates": join_json(record_dates),
                "index_county_types": join_json(county_types),
                "foreign_entity_flag": row.get("foreign_entity_flag", ""),
                "foreign_entity_jurisdictions": row.get("foreign_entity_jurisdictions", "[]"),
                "commercial_readiness": READINESS,
                "next_action": "cluster_resource_rights_and_verify_property_type_assessor_land_use",
            })

        estimates = parse_jsonish(row.get("estimated_consideration_from_county_tax", ""))
        if estimates:
            transfer_tax_clues.append({
                "doc_no": doc_no,
                "transfer_tax_raw": row.get("transfer_tax_raw", ""),
                "estimated_consideration_from_county_tax": row.get("estimated_consideration_from_county_tax", "[]"),
                "consideration_confidence": row.get("consideration_confidence", ""),
                "commercial_readiness": "price_clue_needs_verification",
                "next_action": "verify_county_city_tax_rate_exemptions_and_deed_consideration",
            })

    rollup_rows = []
    for roll in entity_rollups.values():
        rollup_rows.append({
            "entity_norm": roll["entity_norm"],
            "entity_display": roll["entity_display"],
            "jurisdictions": join_json(sorted(roll["jurisdictions"])),
            "regions": join_json(sorted(roll["regions"])),
            "doc_count": len(roll["doc_nos"]),
            "ain_count": len([v for v in roll["ains"] if v]),
            "doc_nos": join_json(sorted(roll["doc_nos"])),
            "ains": join_json(sorted(roll["ains"])),
            "first_record_date": date_min(roll["record_dates"]),
            "last_record_date": date_max(roll["record_dates"]),
            "county_types": join_json(sorted(roll["county_types"])),
            "grantors": join_json(sorted(roll["grantors"])),
            "grantees": join_json(sorted(roll["grantees"])),
            "company_numbers": join_json(sorted(roll["company_numbers"])),
            "tags": join_json(sorted(roll["tags"])),
            "phrases": join_json(sorted(roll["phrases"])),
            "commercial_readiness": READINESS,
            "next_action": "verify_repeat_behavior_same_apn_chain_sos_or_foreign_registry_if_available",
        })
    rollup_rows.sort(key=lambda r: (-int(r["doc_count"]), str(r["entity_norm"])))

    jurisdiction_rows = [
        {
            "jurisdiction": jurisdiction,
            "region": next((r["region"] for r in foreign_candidates if r["jurisdiction"] == jurisdiction), ""),
            "doc_mentions": count,
            "unique_entities": len({r["entity_norm"] for r in foreign_candidates if r["jurisdiction"] == jurisdiction}),
        }
        for jurisdiction, count in jurisdiction_counts.most_common()
    ]

    write_csv(out_dir / "foreign_entity_candidates.csv", foreign_candidates, [
        "doc_no", "entity", "entity_norm", "jurisdiction", "region", "entity_type",
        "phrase", "index_ains", "index_record_dates", "index_county_types",
        "index_grantors", "index_grantees", "company_numbers", "tags",
        "ocr_text_path", "commercial_readiness", "next_action",
    ])
    write_csv(out_dir / "entity_rollup.csv", rollup_rows, [
        "entity_norm", "entity_display", "jurisdictions", "regions", "doc_count",
        "ain_count", "doc_nos", "ains", "first_record_date", "last_record_date",
        "county_types", "grantors", "grantees", "company_numbers", "tags",
        "phrases", "commercial_readiness", "next_action",
    ])
    write_csv(out_dir / "jurisdiction_summary.csv", jurisdiction_rows, [
        "jurisdiction", "region", "doc_mentions", "unique_entities",
    ])
    write_csv(out_dir / "international_mail_candidates.csv", international_mail, [
        "doc_no", "mail_to_country", "mail_to_raw", "foreign_entity_flag",
        "foreign_entity_jurisdictions", "commercial_readiness", "next_action",
    ])
    write_csv(out_dir / "resource_rights_candidates.csv", resource_candidates, [
        "doc_no", "mineral_terms", "index_ains", "index_record_dates",
        "index_county_types", "foreign_entity_flag", "foreign_entity_jurisdictions",
        "commercial_readiness", "next_action",
    ])
    write_csv(out_dir / "transfer_tax_price_clues.csv", transfer_tax_clues, [
        "doc_no", "transfer_tax_raw", "estimated_consideration_from_county_tax",
        "consideration_confidence", "commercial_readiness", "next_action",
    ])
    write_csv(out_dir / "doc_signal_matrix.csv", doc_signal_matrix, [
        "doc_no", "foreign_entity_flag", "foreign_entity_jurisdictions",
        "mail_to_international_flag", "mail_to_country", "mineral_rights_signal",
        "transfer_tax_raw", "estimated_consideration_from_county_tax",
        "company_numbers", "tags", "readiness",
    ])

    summary = {
        "generated_at_utc": now_utc(),
        "input_csv": str(input_csv),
        "out_dir": str(out_dir),
        "docs_reviewed": len(rows),
        "foreign_entity_candidate_rows": len(foreign_candidates),
        "foreign_entity_rollups": len(rollup_rows),
        "jurisdiction_counts": dict(jurisdiction_counts),
        "region_counts": dict(region_counts),
        "international_mail_candidates": len(international_mail),
        "resource_rights_candidates": len(resource_candidates),
        "transfer_tax_price_clues": len(transfer_tax_clues),
        "tag_counts": dict(tag_counts),
        "commercial_readiness": READINESS,
        "raw_contacts_exported": 0,
        "outputs": {
            "foreign_entity_candidates_csv": str(out_dir / "foreign_entity_candidates.csv"),
            "entity_rollup_csv": str(out_dir / "entity_rollup.csv"),
            "jurisdiction_summary_csv": str(out_dir / "jurisdiction_summary.csv"),
            "international_mail_candidates_csv": str(out_dir / "international_mail_candidates.csv"),
            "resource_rights_candidates_csv": str(out_dir / "resource_rights_candidates.csv"),
            "transfer_tax_price_clues_csv": str(out_dir / "transfer_tax_price_clues.csv"),
            "doc_signal_matrix_csv": str(out_dir / "doc_signal_matrix.csv"),
        },
    }
    (out_dir / "pattern_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input_csv")
    parser.add_argument("out_dir")
    args = parser.parse_args()
    summary = analyze(Path(args.input_csv), Path(args.out_dir))
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
