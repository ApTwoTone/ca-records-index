#!/usr/bin/env python3
"""select_lead_docs.py -- from the merged index CSV, pick the doc#s worth pulling
the recorded PDF/page-images for (so OCR can extract APN + property address).

Default: the property-bearing lead classes (NOD/NTS/trustee-deed + AOD/RTDD +
liens + lis pendens + probate). Writes a newline doc# list AND a contiguous
min/max so the thumb fan-out can shard it.

Usage: python3 select_lead_docs.py <merged.csv> <out_doclist.txt> [class1 class2 ...]
"""
import sys, csv

PROPERTY_BEARING = {
    "notice_of_default", "notice_of_trustees_sale", "trustees_deed_upon_sale",
    "affidavit_death", "affidavit_death_unspecified",
    "affidavit_successor_trustee", "affidavit_succession_interest",
    "revocable_transfer_death_deed", "decree_distribution_probate",
    "abstract_of_judgment", "tax_lien", "mechanics_lien", "hoa_lien",
    "lis_pendens",
}

def main():
    src, out = sys.argv[1], sys.argv[2]
    classes = set(sys.argv[3:]) or PROPERTY_BEARING
    docs = []
    counts = {}
    with open(src, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if str(r.get("ok")).lower() != "true":
                continue
            lc = (r.get("lead_class") or "").strip()
            if lc in classes:
                docs.append(r["doc_no"])
                counts[lc] = counts.get(lc, 0) + 1
    docs = sorted(set(docs), key=lambda x: int(x))
    with open(out, "w") as f:
        f.write("\n".join(docs) + ("\n" if docs else ""))
    print("selected %d docs for PDF pull" % len(docs))
    for k in sorted(counts, key=lambda k: -counts[k]):
        print("  %5d  %s" % (counts[k], k))
    if docs:
        print("range: %s .. %s" % (docs[0], docs[-1]))
    print("doclist: %s" % out)

if __name__ == "__main__":
    main()
