#!/usr/bin/env python3
"""Shared cryptographic completeness validation for NETR document pages."""

from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from pathlib import Path

from pull_docs import page_fingerprint_file


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes"}


def _valid_evidence(
    outdir: Path,
    doc: str,
    evidence: dict[str, str],
    page_paths: dict[tuple[str, int], Path],
) -> tuple[bool, str]:
    event = (evidence.get("event") or "").strip()
    upstream = (evidence.get("upstream_status") or "").strip()
    if event == "upstream_500_end":
        if upstream != "500":
            return False, "upstream end is not backed by status 500"
        try:
            candidate_bytes = int(evidence.get("candidate_bytes") or "0")
        except ValueError:
            return False, "invalid upstream-end candidate size"
        if candidate_bytes:
            candidate = outdir / (evidence.get("candidate_path") or "")
            if not candidate.is_file():
                return False, "upstream-end response bytes missing"
            if hashlib.sha256(candidate.read_bytes()).hexdigest() != evidence.get("candidate_body_sha256"):
                return False, "upstream-end response hash mismatch"
        return True, ""
    if event not in {"exact_byte_duplicate", "exact_pixel_duplicate"}:
        return False, f"unsupported terminal event {event or 'missing'}"
    try:
        matched_page = int(evidence.get("matched_page") or "0")
        terminal_page = int(evidence.get("page") or "0")
    except ValueError:
        return False, "invalid matched/terminal page"
    if matched_page <= 0 or terminal_page <= matched_page:
        return False, "duplicate did not match a prior page"
    candidate = outdir / (evidence.get("candidate_path") or "")
    matched = page_paths.get((doc, matched_page))
    if not candidate.is_file() or matched is None or not matched.is_file():
        return False, "candidate or matched page bytes missing"
    candidate_body = hashlib.sha256(candidate.read_bytes()).hexdigest()
    matched_body = hashlib.sha256(matched.read_bytes()).hexdigest()
    if candidate_body != evidence.get("candidate_body_sha256"):
        return False, "candidate byte hash mismatch"
    if matched_body != evidence.get("matched_body_sha256"):
        return False, "matched byte hash mismatch"
    if event == "exact_byte_duplicate":
        return candidate_body == matched_body, "byte hashes are not exact"
    candidate_pixels = str(page_fingerprint_file(candidate)["pixel_sha256"])
    matched_pixels = str(page_fingerprint_file(matched)["pixel_sha256"])
    recorded_candidate = (evidence.get("candidate_pixel_sha256") or "").strip()
    recorded_matched = (evidence.get("matched_pixel_sha256") or "").strip()
    valid = bool(recorded_candidate) and (
        candidate_pixels == matched_pixels == recorded_candidate == recorded_matched
    )
    return valid, "decoded pixel hashes are not exact"


def validate_artifact_dir(outdir: Path) -> dict[str, object]:
    status_rows = read_csv(outdir / "doc_status.csv")
    manifest_rows = read_csv(outdir / "pages_manifest.csv")
    evidence_rows = read_csv(outdir / "terminal_evidence.csv")
    manifests_by_doc: dict[str, list[dict[str, str]]] = {}
    page_paths: dict[tuple[str, int], Path] = {}
    for row in manifest_rows:
        doc = (row.get("doc_no") or "").strip()
        manifests_by_doc.setdefault(doc, []).append(row)
        if (row.get("status") or "").startswith("ok") and (row.get("path") or "").strip():
            try:
                page_paths[(doc, int(row.get("page") or "0"))] = outdir / row["path"]
            except ValueError:
                pass
    terminal_by_doc: dict[str, list[dict[str, str]]] = {}
    for row in evidence_rows:
        if truthy(row.get("terminal") or ""):
            terminal_by_doc.setdefault((row.get("doc_no") or "").strip(), []).append(row)
    proven: set[str] = set()
    proof_records: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    for status_row in status_rows:
        doc = (status_row.get("doc_no") or "").strip()
        if (status_row.get("status") or "").strip() != "done":
            continue
        rows = manifests_by_doc.get(doc, [])
        try:
            ok_pages = sorted({
                int(row.get("page") or "0") for row in rows
                if (row.get("status") or "").startswith("ok")
            })
        except ValueError:
            errors.append({"doc_no": doc, "reason": "invalid page number"})
            continue
        if not ok_pages or ok_pages != list(range(1, max(ok_pages) + 1)):
            errors.append({"doc_no": doc, "reason": "non-contiguous OK pages"})
            continue
        valid: list[dict[str, str]] = []
        reasons = []
        for evidence in terminal_by_doc.get(doc, []):
            accepted, reason = _valid_evidence(outdir, doc, evidence, page_paths)
            if accepted:
                valid.append(evidence)
            else:
                reasons.append(reason)
        if len(valid) != 1:
            errors.append({
                "doc_no": doc,
                "reason": "missing/ambiguous terminal proof" if not reasons else "; ".join(reasons),
            })
            continue
        terminal_page = int(valid[0].get("page") or "0")
        if terminal_page != max(ok_pages) + 1:
            errors.append({"doc_no": doc, "reason": "terminal is not immediately after last page"})
            continue
        terminal_manifest = []
        for row in rows:
            try:
                if int(row.get("page") or "0") == terminal_page:
                    terminal_manifest.append(row)
            except ValueError:
                pass
        expected = "end" if valid[0].get("event") == "upstream_500_end" else "duplicate_end"
        if len(terminal_manifest) != 1 or terminal_manifest[0].get("status") != expected:
            errors.append({"doc_no": doc, "reason": "manifest/evidence terminal mismatch"})
            continue
        proven.add(doc)
        proof_records.append({
            key: (valid[0].get(key) or "").strip()
            for key in (
                "doc_no", "page", "event", "upstream_status", "candidate_bytes",
                "candidate_body_sha256", "candidate_pixel_sha256", "matched_page",
                "matched_body_sha256", "matched_pixel_sha256",
            )
        })
    done = {
        (row.get("doc_no") or "").strip() for row in status_rows
        if (row.get("status") or "").strip() == "done"
    }
    return {
        "complete": bool(status_rows) and done == proven and not errors,
        "status_rows": len(status_rows),
        "status_counts": dict(Counter((row.get("status") or "").strip() for row in status_rows)),
        "manifest_rows": len(manifest_rows),
        "terminal_evidence_rows": len(evidence_rows),
        "done_docs": sorted(done),
        "proven_docs": sorted(proven),
        "proof_records": proof_records,
        "errors": errors,
    }


def validate_artifact_root(root: Path, requested_docs: set[str]) -> dict[str, object]:
    candidates = sorted(root.glob("**/pages_manifest.csv"))
    raw = [path for path in candidates if "-raw" in path.parent.name]
    if raw:
        candidates = raw
    proven: set[str] = set()
    errors: list[dict[str, str]] = []
    proof_records: dict[str, dict[str, str]] = {}
    artifact_summaries = []
    for manifest in candidates:
        summary = validate_artifact_dir(manifest.parent)
        artifact_summaries.append({"path": str(manifest.parent), **summary})
        proven.update(set(summary["proven_docs"]) & requested_docs)
        for record in summary["proof_records"]:
            if record["doc_no"] in requested_docs:
                proof_records[record["doc_no"]] = record
        errors.extend(summary["errors"])
    missing = sorted(requested_docs - proven)
    proof_inventory_sha256 = hashlib.sha256(
        json.dumps(
            [proof_records[key] for key in sorted(proof_records)],
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "complete": bool(candidates) and not missing,
        "requested_docs": len(requested_docs),
        "proven_docs": len(proven),
        "missing_docs": missing,
        "artifact_dirs": len(candidates),
        "proof_errors": errors,
        "proof_inventory_sha256": proof_inventory_sha256,
        "artifact_summaries": artifact_summaries,
    }
