#!/usr/bin/env python3
"""
doc_taxonomy.py -- CA legal document-type + foreclosure-STAGE taxonomy
=====================================================================

Why this exists
---------------
The NOD pipeline classifies each recorded foreclosure document from a single
vision/OCR guess.  Two failure modes hurt deal-flow:

  (a) We can mislabel the COUNTY'S OWN index Type (e.g. LA's "DEFAULT
      CERTIFICATION", whose recorded body OCRs as "NOTICE OF DEFAULT AND
      ELECTION TO SELL UNDER DEED OF TRUST").
  (b) We chase deals that are already too late.  Carlos: a NOD that ALREADY
      has a later filing (Notice of Trustee's Sale, Trustee's Deed) is
      "pretty much sold."  We must surface the EARLIEST signal and WARN when
      a property is past it.

This module is the single source of truth for:

  * mapping a RAW county index Type string  -> operational class + CA
    foreclosure STAGE (1/2/3) + is_actionable_nod + freshness tier
  * reconciling the county Type with the OCR'd body doc-type into one verdict
  * a property-level STAGE WARNING so an operator never chases a sold deal,
    and the earliest actionable NOD per property is surfaced.

CA foreclosure stage ground truth (encoded here)
------------------------------------------------
  STAGE 1  Notice of Default (NOD)           first / freshest, 90-day clock.
           Full recorded title is literally
           "NOTICE OF DEFAULT AND ELECTION TO SELL UNDER DEED OF TRUST"
           -- "election to sell" is PART OF the NOD, NOT disqualifying.
           LA county also indexes the NOD-package as "DEFAULT CERTIFICATION"
           (the NOD + the Civ. Code sec. 2923.5/2923.55 declaration of
           compliance / certification of borrower contact recorded with it).
  STAGE 2  Notice of Trustee's Sale (NTS/NOS) ~3+ months later, sets auction
           date.  LATE -- likely already marketed / sold.
  STAGE 3  Trustee's Deed Upon Sale          post-auction.  GONE.

  Not actionable signals (no stage):
    Rescission of NOD           -> NOD cancelled / cured.
    Request for Notice of Default -> a lienholder asking to be notified;
                                     NEVER a foreclosure filing.
    Substitution of Trustee / Assignment of Deed of Trust -> servicing
                                     mechanics, often co-recorded; not the
                                     action signal themselves.

Design rules
------------
  * Accept ANY raw string.  Unknown -> needs_review, fail-closed
    (is_actionable_nod=False).
  * Robust normalization: case / punctuation / whitespace / OCR ampersand
    noise insensitive.
  * "DEFAULT CERTIFICATION" resolves to the actionable STAGE-1 NOD package
    (see body-OCR evidence in module docstring of the resolution section).

Python 3.9-safe (Optional[...], no PEP-604 unions).

SAFETY: pure classification.  No I/O, no network, no DB writes, no sending.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Iterable

GATE_VERSION = "doc_taxonomy_v2_20260619"

# --------------------------------------------------------------------------
# Operational classes (stable vocabulary consumed by index_reconcile etc.)
# --------------------------------------------------------------------------
CLASS_NOD = "notice_of_default"            # STAGE 1, actionable
CLASS_NTS = "notice_of_trustees_sale"      # STAGE 2, late
CLASS_TRUSTEES_DEED = "trustees_deed"      # STAGE 3, gone
CLASS_RESCISSION = "rescission"            # NOD cured
CLASS_REQUEST_NOTICE = "request_for_notice"  # never actionable
CLASS_SUBSTITUTION = "substitution_of_trustee"  # servicing mechanic
CLASS_ASSIGNMENT = "assignment_of_dot"     # servicing mechanic
# CANONICAL DECISION (Phase B, addendum sec 6.4, 2026-06-19): "DEFAULT
# CERTIFICATION" is NOT a clean active NOD. It is the lender's Civ.Code
# 2923.5/2923.55 declaration-of-compliance certification, a distress/SUPPORTING
# document recorded around the NOD lifecycle -- it is NOT itself the operative
# Notice of Default and Election to Sell. It is a non-actionable distress
# support signal: visible/research-only, NEVER source_badge=NOD, never callable.
# This aligns doc_taxonomy with classify_doc_vision.classify_title (which maps
# DEFAULT CERTIFICATION -> "other") and doc_intake_gate.evaluate (which holds
# it), resolving the prior policy conflict.
CLASS_DEFAULT_CERTIFICATION = "default_certification"  # distress support, NOT NOD
CLASS_NON_FORECLOSURE = "non_foreclosure"  # deed/lien/death/divorce/etc.
CLASS_NEEDS_REVIEW = "needs_review"        # unknown / fail-closed

# Freshness tiers (operator-facing label paralleling stage)
TIER_FRESHEST = "freshest"        # STAGE 1 NOD -- act now
TIER_LATE = "late"                # STAGE 2 NTS  -- likely marketed/sold
TIER_GONE = "gone"                # STAGE 3 deed -- sold
TIER_CURED = "cured"              # rescission
TIER_NONE = "none"                # not a foreclosure-progress signal
TIER_UNKNOWN = "unknown"          # needs_review

# Stage by class (None where stage is not meaningful)
_STAGE_BY_CLASS = {
    CLASS_NOD: 1,
    CLASS_NTS: 2,
    CLASS_TRUSTEES_DEED: 3,
    CLASS_RESCISSION: None,
    CLASS_REQUEST_NOTICE: None,
    CLASS_SUBSTITUTION: None,
    CLASS_ASSIGNMENT: None,
    CLASS_DEFAULT_CERTIFICATION: None,
    CLASS_NON_FORECLOSURE: None,
    CLASS_NEEDS_REVIEW: None,
}
_TIER_BY_CLASS = {
    CLASS_NOD: TIER_FRESHEST,
    CLASS_NTS: TIER_LATE,
    CLASS_TRUSTEES_DEED: TIER_GONE,
    CLASS_RESCISSION: TIER_CURED,
    CLASS_REQUEST_NOTICE: TIER_NONE,
    CLASS_SUBSTITUTION: TIER_NONE,
    CLASS_ASSIGNMENT: TIER_NONE,
    CLASS_DEFAULT_CERTIFICATION: TIER_NONE,
    CLASS_NON_FORECLOSURE: TIER_NONE,
    CLASS_NEEDS_REVIEW: TIER_UNKNOWN,
}


# --------------------------------------------------------------------------
# Normalization
# --------------------------------------------------------------------------
def _norm(s: Optional[str]) -> str:
    """Uppercase, collapse punctuation/whitespace, drop OCR ampersand noise."""
    if not s:
        return ""
    s = s.upper()
    # common OCR junk between words: ampersands, tildes, stray symbols
    s = re.sub(r"[&~^*_]+", " ", s)
    s = re.sub(r"[^\w\s]", " ", s)          # punctuation -> space
    s = re.sub(r"\s+", " ", s).strip()
    return s


# --------------------------------------------------------------------------
# Ordered matchers for the RAW COUNTY INDEX TYPE string.
# IMPORTANT: order matters -- the FIRST family that matches wins, so the
# most-specific / most-disqualifying patterns are listed first.
#
#   RESCISSION / REQUEST-FOR-NOTICE / CANCELLATION must beat the bare
#   "NOTICE OF DEFAULT" substring, because both literally contain it.
# --------------------------------------------------------------------------
_COUNTY_RULES = [
    # ---- NOT a foreclosure filing: a lienholder asking to be notified ----
    (CLASS_REQUEST_NOTICE, [
        r"\bREQUEST\b.*\bNOTICE\b.*\bDEFAULT\b",
        r"\bREQUEST\b.*\bNOTICE\b.*\bSALE\b",
        r"\bREQUEST\s+FOR\s+NOTICE\b",
    ]),
    # ---- NOD cancelled / cured ----
    (CLASS_RESCISSION, [
        r"\bRESCISSION\b",
        r"\bRESCIND",
        r"\bCANCELLATION\b.*\b(NOTICE|DEFAULT|SALE)\b",
        r"\bWITHDRAWAL\b.*\bNOTICE\b.*\bDEFAULT\b",
    ]),
    # ---- DEFAULT CERTIFICATION: lender Civ.Code 2923.5/2923.55 compliance
    #      certification recorded around the NOD lifecycle. NOT the operative
    #      NOD. Must beat the bare "DEFAULT" substring in CLASS_NOD below, so it
    #      is listed FIRST. Non-actionable distress SUPPORT signal. (sec 6.4) --
    (CLASS_DEFAULT_CERTIFICATION, [
        r"\bDEFAULT\s+CERTIFICATION\b",
        r"\bCERTIFICATION\s+OF\s+DEFAULT\b",
        r"\bDECLARATION\s+OF\s+COMPLIANCE\b",
    ]),
    # ---- STAGE 3: post-auction deed ----
    # NOTE: possessive apostrophe in "TRUSTEE'S" is normalized to a space, so
    # match TRUSTEE / TRUSTEES / TRUSTEE S with an optional trailing token.
    (CLASS_TRUSTEES_DEED, [
        r"\bTRUSTEE\s*S?\s+DEED\b",
        r"\bDEED\s+UPON\s+SALE\b",
    ]),
    # ---- STAGE 2: notice of trustee's sale ----
    (CLASS_NTS, [
        r"\bNOTICE\s+OF\s+TRUSTEE\s*S?\s+SALE\b",
        r"\bNOTICE\s+OF\s+SALE\b",
        r"\bTRUSTEE\s*S?\s+SALE\b",
        r"\bNOTICE\s+TRUSTEE\b",
        r"\bN\s*O\s*T\s*S\b",
        r"\bNOS\b",
    ]),
    # ---- STAGE 1: NOD, incl. the "...AND ELECTION TO SELL" full title.
    #      DEFAULT CERTIFICATION is NO LONGER mapped here (sec 6.4 canonical
    #      decision -- see CLASS_DEFAULT_CERTIFICATION rule above). --
    (CLASS_NOD, [
        r"\bNOTICE\s+OF\s+DEFAULT\b",
        r"\bNOTICE\s+DEFAULT\b",
        r"\bDEFAULT\s+AND\s+ELECTION\s+TO\s+SELL\b",
        r"\bELECTION\s+TO\s+SELL\s+UNDER\s+DEED\s+OF\s+TRUST\b",
        r"\bNOTICE\s+OF\s+DELINQUENCY\b",
    ]),
    # ---- servicing mechanics (co-recorded; not the action signal) ----
    (CLASS_SUBSTITUTION, [
        r"\bSUBSTITUTION\s+OF\s+TRUSTEE\b",
        r"\bSUBSTITUTION\s+TRUSTEE\b",
        r"\bSUB\s+OF\s+TRUSTEE\b",
    ]),
    (CLASS_ASSIGNMENT, [
        r"\bASSIGNMENT\s+OF\s+(THE\s+)?DEED\s+OF\s+TRUST\b",
        r"\bASSIGNMENT\s+(OF\s+)?DEED\s+OF\s+TRUST\b",
        r"\bASSIGNMENT\s+TRUST\s+DEED\b",
        r"\bASSIGNMENT\s+OF\s+(RENTS|MORTGAGE)\b",
    ]),
]

# Strong "this is clearly NOT a foreclosure-progress doc" county families,
# so they classify as non_foreclosure rather than needs_review.
_NON_FORECLOSURE_RULES = [
    r"\bGRANT\s+DEED\b", r"\bQUITCLAIM\b", r"\bQUIT\s+CLAIM\b",
    r"\bINTERSPOUSAL\b", r"\bDISSOLUTION\b", r"\bMARITAL\b",
    r"\bAFFIDAVIT\b.*\bDEATH\b", r"\bPROBATE\b", r"\bDECEDENT\b",
    r"\bDECREE\b", r"\bRECONVEYANCE\b", r"\bSATISFACTION\b",
    r"\bLIS\s+PENDENS\b", r"\bNOTICE\s+ACTION\b",
    r"\bABSTRACT\b.*\bJUDG", r"\bMECHANIC",
    r"\bTAX\s+LIEN\b", r"\bLIEN\s+INVOLUNTARY\b",
    r"\bDELINQUENT\s+ASSESSMENT\b", r"\bHOMESTEAD\b", r"\bUCC\b",
    r"\bSUBORDINATION\b", r"\bLOAN\s+MODIFICATION\b",
    r"\bRELEASE\b", r"\bDEED\s+OF\s+TRUST\b",
]


def classify_county_type(county_type: Optional[str]) -> Dict[str, object]:
    """
    Map a RAW county-index Type string -> operational class + CA stage.

    Returns dict with keys:
      operational_class : one of CLASS_* above
      foreclosure_stage : 1|2|3 or None
      is_actionable_nod : True ONLY for a fresh STAGE-1 NOD package
      freshness_tier    : TIER_* label
      notes             : human-readable rationale
      raw               : the raw input (echoed)
      gate_version      : GATE_VERSION

    Fail-closed: unknown / empty -> needs_review, is_actionable_nod=False.
    """
    raw = county_type or ""
    blob = _norm(raw)

    if not blob:
        return _result(CLASS_NEEDS_REVIEW, raw,
                       "empty county type -> needs_review (fail-closed)")

    for cls, pats in _COUNTY_RULES:
        if any(re.search(p, blob) for p in pats):
            note = _county_note(cls, blob)
            return _result(cls, raw, note)

    if any(re.search(p, blob) for p in _NON_FORECLOSURE_RULES):
        return _result(CLASS_NON_FORECLOSURE, raw,
                       "recognized non-foreclosure recorded doc")

    return _result(CLASS_NEEDS_REVIEW, raw,
                   "unrecognized county type -> needs_review (fail-closed)")


def _county_note(cls: str, blob: str) -> str:
    if cls == CLASS_DEFAULT_CERTIFICATION:
        return ("'DEFAULT CERTIFICATION' = lender Civ.Code 2923.5/2923.55 "
                "declaration-of-compliance certification recorded around the "
                "NOD lifecycle; NOT the operative Notice of Default & Election "
                "to Sell. Non-actionable distress SUPPORT signal: research/"
                "visibility-only, never a clean NOD badge, never callable. "
                "(canonical decision, addendum sec 6.4, 2026-06-19)")
    if cls == CLASS_NOD:
        if "ELECTION TO SELL" in blob:
            return ("full NOD title incl. 'AND ELECTION TO SELL' -- election "
                    "to sell is PART of the NOD, NOT disqualifying. STAGE 1.")
        return "Notice of Default. STAGE 1, freshest, actionable."
    if cls == CLASS_NTS:
        return ("Notice of Trustee's Sale. STAGE 2 -- auction set, ~3+ mo "
                "after NOD; likely already marketed/sold.")
    if cls == CLASS_TRUSTEES_DEED:
        return "Trustee's Deed Upon Sale. STAGE 3 -- post-auction, gone."
    if cls == CLASS_RESCISSION:
        return "Rescission/cancellation of NOD -- cured, not actionable."
    if cls == CLASS_REQUEST_NOTICE:
        return ("Request for Notice of Default -- a lienholder asking to be "
                "notified; NOT a foreclosure filing, never actionable.")
    if cls == CLASS_SUBSTITUTION:
        return ("Substitution of Trustee -- servicing mechanic, often "
                "co-recorded; not the action signal itself.")
    if cls == CLASS_ASSIGNMENT:
        return ("Assignment of Deed of Trust -- servicing mechanic; not the "
                "action signal itself.")
    return ""


# Classes that are distress/supporting context but NEVER a clean active NOD and
# NEVER callable. Surfaced research/visibility-only. (addendum sec 6.2/6.3)
DISTRESS_SUPPORT_CLASSES = frozenset({CLASS_DEFAULT_CERTIFICATION})


def _result(cls: str, raw: str, note: str) -> Dict[str, object]:
    stage = _STAGE_BY_CLASS.get(cls)
    return {
        "operational_class": cls,
        "foreclosure_stage": stage,
        "is_actionable_nod": (cls == CLASS_NOD),
        # distress support = visible research-only, never clean NOD, never callable
        "is_distress_support": (cls in DISTRESS_SUPPORT_CLASSES),
        "freshness_tier": _TIER_BY_CLASS.get(cls, TIER_UNKNOWN),
        "notes": note,
        "raw": raw,
        "gate_version": GATE_VERSION,
    }


# --------------------------------------------------------------------------
# Map the pipeline's OCR-derived doc_type (snake_case vocabulary) to a class.
# These are the values stored in recorder_docs.doc_type.
# --------------------------------------------------------------------------
_OCR_DOCTYPE_TO_CLASS = {
    "notice_of_default": CLASS_NOD,
    "notice_of_trustees_sale": CLASS_NTS,
    "trustees_deed_upon_sale": CLASS_TRUSTEES_DEED,
    "rescission_notice_of_default": CLASS_RESCISSION,
    "rescission_notice_of_sale": CLASS_RESCISSION,
    "release_of_notice": CLASS_RESCISSION,
    "request_notice_of_default": CLASS_REQUEST_NOTICE,
    "substitution_of_trustee": CLASS_SUBSTITUTION,
    "assignment_deed_of_trust": CLASS_ASSIGNMENT,
}


def _class_from_ocr_doctype(ocr_doc_type: Optional[str]) -> Optional[str]:
    if not ocr_doc_type:
        return None
    key = ocr_doc_type.strip().lower()
    if key in _OCR_DOCTYPE_TO_CLASS:
        return _OCR_DOCTYPE_TO_CLASS[key]
    # also try treating the OCR doc_type as a free-text title
    free = classify_county_type(ocr_doc_type)
    cls = free["operational_class"]
    return cls if cls != CLASS_NEEDS_REVIEW else None


# --------------------------------------------------------------------------
# Reconcile OCR body doc-type with county index Type into ONE verdict.
# --------------------------------------------------------------------------
def classify(ocr_doc_type: Optional[str],
             county_type: Optional[str]) -> Dict[str, object]:
    """
    Reconcile the OCR'd body doc-type and the RAW county index Type into one
    verdict.

    Policy:
      * county_type is the authoritative index label; start from it.
      * If OCR body resolves to a class AND it disagrees with county, the more
        ADVANCED stage wins for safety against chasing sold deals (a body that
        reads NTS over a county "NOD" -> treat as NTS / late), BUT a body that
        reads NOD under a county RESCISSION/REQUEST/DEFAULT CERTIFICATION stays
        non-actionable (cured/never/support wins -- fail-closed away from
        action). DEFAULT CERTIFICATION's body often OCRs as an NOD-like title;
        the authoritative county index label must NOT be promoted to a clean
        NOD by that body read (addendum sec 6.4 canonical decision).
      * If county is needs_review, fall back to the OCR class.
      * Disagreement is always flagged in `agreement` + `notes`.
    """
    county = classify_county_type(county_type)
    county_cls = county["operational_class"]
    ocr_cls = _class_from_ocr_doctype(ocr_doc_type)

    # Non-actionable "trumps": cured / request-for-notice / default-certification
    # can never be promoted to an actionable clean NOD by an OCR NOD read.
    NON_ACTIONABLE_LOCK = {
        CLASS_RESCISSION,
        CLASS_REQUEST_NOTICE,
        CLASS_DEFAULT_CERTIFICATION,
    }

    if ocr_cls is None:
        chosen = county_cls
        agreement = "ocr_unresolved"
        note = "OCR doc-type unresolved; using county index label."
    elif county_cls == CLASS_NEEDS_REVIEW:
        chosen = ocr_cls
        agreement = "county_unknown_used_ocr"
        note = "county type unknown; using OCR body class."
    elif county_cls == ocr_cls:
        chosen = county_cls
        agreement = "agree"
        note = "county and OCR agree."
    elif county_cls in NON_ACTIONABLE_LOCK:
        chosen = county_cls
        agreement = "disagree_county_nonactionable_locked"
        note = ("county is %s (cured/never-actionable); not promoting on OCR "
                "%s -- fail-closed." % (county_cls, ocr_cls))
    elif ocr_cls in NON_ACTIONABLE_LOCK:
        chosen = ocr_cls
        agreement = "disagree_ocr_nonactionable_locked"
        note = ("OCR body reads %s (cured/request); de-escalating from county "
                "%s -- fail-closed." % (ocr_cls, county_cls))
    else:
        # both are foreclosure-progress classes that disagree -> take the
        # more ADVANCED stage (safer: warns against late deals).
        c_stage = _STAGE_BY_CLASS.get(county_cls) or 0
        o_stage = _STAGE_BY_CLASS.get(ocr_cls) or 0
        if o_stage > c_stage:
            chosen = ocr_cls
        else:
            chosen = county_cls
        agreement = "disagree_advanced_stage_wins"
        note = ("county=%s vs OCR=%s disagree; took more-advanced stage=%s."
                % (county_cls, ocr_cls, chosen))

    out = _result(chosen, county_type or "",
                  (county["notes"] + " | " if county["notes"] else "") + note)
    out["agreement"] = agreement
    out["county_class"] = county_cls
    out["ocr_class"] = ocr_cls
    out["ocr_doc_type"] = ocr_doc_type or ""
    # When reconciliation lands on needs_review, force non-actionable already
    # handled by _result; keep explicit for clarity.
    return out


# --------------------------------------------------------------------------
# Property-level STAGE WARNING.
# --------------------------------------------------------------------------
def stage_warning(doc_no: str,
                  property_key: str,
                  all_docs_for_property: Iterable[Dict[str, object]]
                  ) -> Dict[str, object]:
    """
    Given ALL recorded docs for a single property, decide whether the property
    is past the actionable window.

    Each item in `all_docs_for_property` should be a dict with at least:
        doc_no            (str)
        county_type       (str)   raw county index Type  [preferred]
      and/or
        ocr_doc_type      (str)   OCR body class
        recording_date    (str)   ISO 'YYYY-MM-DD'       [optional, for ordering]

    Returns:
        property_key
        anchor_doc_no       the doc_no this warning was requested for
        max_stage           highest foreclosure stage present (1/2/3) or None
        late                True if a STAGE-2/3 filing exists (likely sold)
        gone                True if a STAGE-3 trustee's deed exists
        warning             operator-facing string ('' if none)
        earliest_actionable_nod  {doc_no, recording_date} of the freshest
                                 actionable NOD, or None
        stage_docs          per-doc [{doc_no, class, stage, actionable}]
    """
    docs = list(all_docs_for_property or [])
    stage_docs: List[Dict[str, object]] = []
    nod_candidates: List[Dict[str, object]] = []
    max_stage = 0
    has_gone = False
    has_late = False

    for d in docs:
        verdict = classify(d.get("ocr_doc_type"), d.get("county_type"))
        cls = verdict["operational_class"]
        stage = verdict["foreclosure_stage"]
        actionable = bool(verdict["is_actionable_nod"])
        dn = d.get("doc_no", "")
        rec = d.get("recording_date") or ""
        stage_docs.append({
            "doc_no": dn,
            "class": cls,
            "stage": stage,
            "recording_date": rec,
            "actionable": actionable,
        })
        if isinstance(stage, int):
            if stage > max_stage:
                max_stage = stage
            if stage == 3:
                has_gone = True
            if stage >= 2:
                has_late = True
        if actionable:
            nod_candidates.append({"doc_no": dn, "recording_date": rec})

    # earliest actionable NOD by recording_date (blank dates sort last)
    earliest = None
    if nod_candidates:
        def _key(x):
            return (x["recording_date"] == "", x["recording_date"], x["doc_no"])
        earliest = sorted(nod_candidates, key=_key)[0]

    warning = ""
    if has_gone:
        warning = ("LATE/GONE: property has a STAGE-3 Trustee's Deed Upon Sale "
                   "-- sold at auction. Do NOT pursue as a pre-foreclosure lead.")
    elif has_late:
        warning = ("LATE: property has a STAGE-2 Notice of Trustee's Sale -- "
                   "auction scheduled; likely already marketed/sold. Treat as "
                   "low-priority / verify before pursuing.")

    return {
        "property_key": property_key,
        "anchor_doc_no": doc_no,
        "max_stage": (max_stage or None),
        "late": has_late,
        "gone": has_gone,
        "warning": warning,
        "earliest_actionable_nod": earliest,
        "stage_docs": stage_docs,
        "gate_version": GATE_VERSION,
    }


# --------------------------------------------------------------------------
# Manual smoke test
# --------------------------------------------------------------------------
if __name__ == "__main__":
    samples = [
        "DEFAULT CERTIFICATION",
        "NOTICE OF DEFAULT AND ELECTION TO SELL UNDER DEED OF TRUST",
        "NOTICE OF DEFAULT",
        "NOTICE OF TRUSTEE'S SALE",
        "TRUSTEE'S DEED UPON SALE",
        "RESCISSION OF NOTICE OF DEFAULT",
        "REQUEST FOR NOTICE OF DEFAULT",
        "SUBSTITUTION OF TRUSTEE",
        "ASSIGNMENT OF DEED OF TRUST",
        "GRANT DEED",
        "SOME WEIRD UNSEEN TYPE 12345",
        "",
    ]
    for s in samples:
        r = classify_county_type(s)
        print("%-55s -> class=%-22s stage=%s actionable=%s tier=%s"
              % (s[:55], r["operational_class"], r["foreclosure_stage"],
                 r["is_actionable_nod"], r["freshness_tier"]))
