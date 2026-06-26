#!/usr/bin/env python3
"""merge_and_report.py -- merge all shard CSVs into one, classify/repair
lead_class, dedup by doc_no, and print the deliverable breakdown.

Usage: python3 merge_and_report.py <shards_dir> <merged_out.csv>
"""
import sys, os, csv, glob, collections

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lead_class as lc

TARGET = [
    "notice_of_default", "notice_of_trustees_sale", "trustees_deed_upon_sale",
    "affidavit_death", "revocable_transfer_death_deed",
    "decree_distribution_probate", "abstract_of_judgment", "tax_lien",
    "mechanics_lien", "interspousal_transfer", "dissolution_marriage",
    "lis_pendens", "hoa_lien", "code_enforcement",
]

def main():
    shards_dir, out_csv = sys.argv[1], sys.argv[2]
    rows = {}
    for f in sorted(glob.glob(os.path.join(shards_dir, "*.csv"))):
        with open(f, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                dn = r.get("doc_no")
                if not dn:
                    continue
                # recompute lead_class from county_type for consistency
                if str(r.get("ok")).lower() == "true" and r.get("county_type"):
                    r["lead_class"] = lc.lead_class(r.get("county_type"))
                rows[dn] = r   # last wins; shards are non-overlapping anyway

    fieldnames = ["doc_no", "ok", "county_type", "record_date",
                  "grantors", "grantees", "ain", "lead_class", "reason"]
    with open(out_csv, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        for dn in sorted(rows):
            w.writerow({k: rows[dn].get(k, "") for k in fieldnames})

    total = len(rows)
    ok = sum(1 for r in rows.values() if str(r.get("ok")).lower() == "true")
    by_type = collections.Counter()
    by_lead = collections.Counter()
    for r in rows.values():
        if str(r.get("ok")).lower() == "true":
            by_type[(r.get("county_type") or "(blank)")] += 1
            by_lead[r.get("lead_class") or "other"] += 1

    print("MERGED rows=%d  ok=%d  not_found/fail=%d" % (total, ok, total - ok))
    print("\n=== BY COUNTY TYPE (top 40) ===")
    for t, c in by_type.most_common(40):
        print("%6d  %s" % (c, t))
    print("\n=== TARGET LEAD COUNTS ===")
    for k in TARGET:
        print("%6d  %s" % (by_lead.get(k, 0), k))
    print("%6d  %s" % (by_lead.get("affidavit_death_unspecified", 0),
                       "affidavit_death_unspecified (probable estate)"))
    print("\nmerged CSV: %s" % os.path.abspath(out_csv))

if __name__ == "__main__":
    main()
