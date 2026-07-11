#!/usr/bin/env python3
"""lead_class.py -- map a RAW LA county index Type string to the full Zoar
TARGET LEAD vocabulary (research-only classification, pure function).

Extends doc_taxonomy's foreclosure classes to the broader distress / estate /
judgment / lien vocabulary the harvest deliverable asks for. Foreclosure
classes are delegated to doc_taxonomy so the negative traps (DEFAULT
CERTIFICATION, REQUEST FOR NOTICE, RESCISSION) stay authoritative.

Returns a single snake_case lead_class string from the union vocabulary:
  notice_of_default, notice_of_trustees_sale, trustees_deed_upon_sale,
  affidavit_death, affidavit_successor_trustee,
  affidavit_succession_interest, revocable_transfer_death_deed,
  decree_distribution_probate,
  abstract_of_judgment, tax_lien, mechanics_lien, interspousal_transfer,
  dissolution_marriage, lis_pendens, hoa_lien, code_enforcement,
  (plus pass-through: rescission, request_for_notice, substitution_of_trustee,
   assignment_of_dot, default_certification, other)
"""
from __future__ import annotations
import re
from typing import Optional

try:
    import doc_taxonomy as _tax
except Exception:                                   # pragma: no cover
    _tax = None


def _norm(s: Optional[str]) -> str:
    if not s:
        return ""
    s = s.upper()
    s = re.sub(r"[&~^*_]+", " ", s)
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


# Ordered, most-specific first. Negative/foreclosure traps handled by doc_taxonomy
# BEFORE these run, so these only see non-foreclosure types.
_RULES = [
    ("revocable_transfer_death_deed", [
        r"^REVOCABLE\s+TRANSFER\s+(?:ON\s+)?DEATH\s+DEED$",
        r"\bREVOCABLE\s+TRANSFER\s+ON\s+DEATH\b",
        r"\bTRANSFER\s+ON\s+DEATH\b",
        r"\bREV\s+TRANS\s+DEATH\b",
        r"\bTOD\s+DEED\b",
    ]),
    ("affidavit_death", [
        r"\bAFFIDAVIT\b.*\bDEATH\b",
        r"\bAFF\b.*\bDEATH\b",
        r"\bDEATH\s+OF\s+(JOINT\s+TENANT|TRUSTEE|SPOUSE|GRANTOR)\b",
        r"\bSPOUSAL\s+PROPERTY\b.*\bDEATH\b",
    ]),
    # These are distinct LA recorder estate-transfer instruments. Keep the
    # matches exact so generic successor-trustee and succession documents do
    # not enter the AOD lane.
    ("affidavit_successor_trustee", [
        r"^AFFIDAVIT\s+SUCCESSOR\s+TRUSTEE$",
    ]),
    ("affidavit_succession_interest", [
        r"^AFFIDAVIT\s+SUCCESSION\s+INTEREST$",
    ]),
    # Bare "AFFIDAVIT" with no qualifier: in the LA recorder index the
    # overwhelming majority of bare affidavits are Affidavits of Death (estate
    # transfer signal). Surface as affidavit_death_unspecified so it is captured
    # as a probable estate lead but kept distinct from confirmed-death affidavits.
    ("affidavit_death_unspecified", [
        r"^AFFIDAVIT$",
    ]),
    ("decree_distribution_probate", [
        r"\bDECREE\b",
        r"\bPROBATE\b",
        r"\bORDER\b.*\bDISTRIBUTION\b",
        r"\bLETTERS\s+TESTAMENTARY\b",
        r"\bORDER\s+FOR\s+PROBATE\b",
    ]),
    ("abstract_of_judgment", [
        r"\bABSTRACT\b.*\bJUDG",
        r"\bABST\b.*\bJUDG",
    ]),
    ("tax_lien", [
        r"\bTAX\s+LIEN\b",
        r"\bFEDERAL\s+TAX\b",
        r"\bSTATE\s+TAX\s+LIEN\b",
        r"\bIRS\b.*\bLIEN\b",
        r"\bFTB\b.*\bLIEN\b",
        r"\bNOTICE\s+OF\s+TAX\b",
    ]),
    ("mechanics_lien", [
        r"\bMECHANIC",
        r"\bCLAIM\s+OF\s+LIEN\b.*\bMECH",
    ]),
    ("hoa_lien", [
        r"\bDELINQUENT\s+ASSESSMENT\b",
        r"\bHOA\b.*\bLIEN\b",
        r"\bHOMEOWNERS?\b.*\bASSOC",
        r"\bASSESSMENT\s+LIEN\b",
        r"\bLIEN\b.*\bASSESSMENT\b",
    ]),
    ("code_enforcement", [
        r"\bCODE\s+ENFORCEMENT\b",
        r"\bNOTICE\s+OF\s+(SUBSTANDARD|VIOLATION|NON\s*COMPLIANCE)\b",
        r"\bNUISANCE\b.*\bABATE",
        r"\bABATEMENT\b",
    ]),
    ("interspousal_transfer", [
        r"\bINTERSPOUSAL\b",
        r"\bINTER\s+SPOUSAL\b",
    ]),
    ("dissolution_marriage", [
        r"\bDISSOLUTION\b",
        r"\bMARITAL\s+SETTLEMENT\b",
        r"\bMARRIAGE\b.*\bDISSOL",
        r"\bJUDGMENT\b.*\bDISSOL",
    ]),
    ("lis_pendens", [
        r"\bLIS\s+PENDENS\b",
        r"\bNOTICE\s+(OF\s+)?(PENDENCY|ACTION)\b",
        r"\bNOTICE\s+PENDING\s+ACTION\b",
    ]),
]

# foreclosure-class passthrough from doc_taxonomy operational_class
_FORECLOSURE_PASSTHROUGH = {
    "notice_of_default": "notice_of_default",
    "notice_of_trustees_sale": "notice_of_trustees_sale",
    "trustees_deed": "trustees_deed_upon_sale",
    "rescission": "rescission",
    "request_for_notice": "request_for_notice",
    "substitution_of_trustee": "substitution_of_trustee",
    "assignment_of_dot": "assignment_of_dot",
    "default_certification": "default_certification",
}


def lead_class(county_type: Optional[str]) -> str:
    """Return a single snake_case lead_class for a raw county Type string."""
    raw = county_type or ""
    blob = _norm(raw)
    if not blob:
        return "other"

    # 1) Let doc_taxonomy own the foreclosure family + its negative traps.
    if _tax is not None:
        cc = _tax.classify_county_type(raw)
        oc = cc.get("operational_class")
        if oc in _FORECLOSURE_PASSTHROUGH:
            return _FORECLOSURE_PASSTHROUGH[oc]
        # non_foreclosure / needs_review fall through to our broader vocab

    # 2) Broader estate / judgment / lien vocabulary.
    for cls, pats in _RULES:
        if any(re.search(p, blob) for p in pats):
            return cls

    return "other"


if __name__ == "__main__":
    tests = [
        "NOTICE OF DEFAULT AND ELECTION TO SELL UNDER DEED OF TRUST",
        "DEFAULT CERTIFICATION",
        "AFFIDAVIT DEATH OF JOINT TENANT",
        "REVOCABLE TRANSFER ON DEATH DEED",
        "DECREE OF DISTRIBUTION",
        "ABSTRACT OF JUDGMENT",
        "FEDERAL TAX LIEN",
        "MECHANICS LIEN",
        "INTERSPOUSAL TRANSFER DEED",
        "JUDGMENT OF DISSOLUTION OF MARRIAGE",
        "LIS PENDENS",
        "NOTICE OF DELINQUENT ASSESSMENT",
        "GRANT DEED",
    ]
    for t in tests:
        print("%-50s -> %s" % (t[:50], lead_class(t)))
