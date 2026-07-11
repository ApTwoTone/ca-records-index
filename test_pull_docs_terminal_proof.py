#!/usr/bin/env python3
from __future__ import annotations

import csv
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image, ImageDraw

import pull_docs


def png_bytes(image: Image.Image, *, compress_level: int = 6) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", compress_level=compress_level)
    return buffer.getvalue()


class FingerprintTests(unittest.TestCase):
    def base_page(self) -> Image.Image:
        image = Image.new("RGB", (512, 700), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((30, 30, 480, 660), outline="black", width=3)
        draw.text((60, 80), "RECORDER DOCUMENT PAGE", fill="black")
        return image

    def test_coarse_ahash_never_terminates_distinct_pixels(self):
        first = self.base_page()
        second = first.copy()
        second.putpixel((250, 350), (0, 0, 0))
        fp1 = pull_docs.page_fingerprint_bytes(png_bytes(first))
        fp2 = pull_docs.page_fingerprint_bytes(png_bytes(second))
        distance = (int(fp1["ahash"]) ^ int(fp2["ahash"])).bit_count()
        self.assertLessEqual(distance, 4)
        self.assertNotEqual(fp1["pixel_sha256"], fp2["pixel_sha256"])
        self.assertIsNone(pull_docs.exact_duplicate_match(fp2, [(1, fp1)]))

    def test_reencoded_identical_pixels_are_exact_pixel_duplicate(self):
        image = self.base_page()
        fp1 = pull_docs.page_fingerprint_bytes(png_bytes(image, compress_level=1))
        fp2 = pull_docs.page_fingerprint_bytes(png_bytes(image, compress_level=9))
        self.assertNotEqual(fp1["body_sha256"], fp2["body_sha256"])
        match = pull_docs.exact_duplicate_match(fp2, [(3, fp1)])
        self.assertIsNotNone(match)
        self.assertEqual(match[0], 3)
        self.assertEqual(match[2], "exact_pixel_duplicate")

    def test_blank_page_is_not_substantive(self):
        blank = Image.new("RGB", (512, 700), "white")
        fp = pull_docs.page_fingerprint_bytes(png_bytes(blank))
        self.assertFalse(pull_docs.is_substantive_page(fp))


class CompletionProofTests(unittest.TestCase):
    def write_csv(self, path: Path, fields: list[str], rows: list[dict[str, object]]) -> None:
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

    def build_exact_duplicate_artifact(self, root: Path) -> None:
        doc = "20260000001"
        image = Image.new("RGB", (512, 700), "white")
        ImageDraw.Draw(image).text((50, 50), "PAGE ONE EVIDENCE", fill="black")
        body = png_bytes(image)
        page = root / f"{doc}_1.png"
        page.write_bytes(body)
        fingerprint = pull_docs.page_fingerprint_bytes(body)
        candidate_dir = root / "terminal_candidates"
        candidate_dir.mkdir()
        candidate = candidate_dir / f"{doc}_2_exact_byte_duplicate.png"
        candidate.write_bytes(body)
        self.write_csv(root / "pages_manifest.csv", [
            "doc_no", "page", "status", "upstream_status", "path", "bytes", "fetched_at_utc"
        ], [
            {"doc_no": doc, "page": 1, "status": "ok", "upstream_status": 200,
             "path": page.name, "bytes": len(body), "fetched_at_utc": "2026-07-11T00:00:00Z"},
            {"doc_no": doc, "page": 2, "status": "duplicate_end", "upstream_status": 200,
             "path": "", "bytes": 0, "fetched_at_utc": "2026-07-11T00:00:01Z"},
        ])
        self.write_csv(root / "failed_pages.csv", [
            "doc_no", "page", "status", "upstream_status", "attempted_at_utc"
        ], [])
        self.write_csv(root / "doc_status.csv", [
            "doc_no", "status", "pages_ok", "last_page_checked", "finished_at_utc"
        ], [{"doc_no": doc, "status": "done", "pages_ok": 1, "last_page_checked": 2,
             "finished_at_utc": "2026-07-11T00:00:01Z"}])
        row = pull_docs.evidence_row(
            doc, 2, "exact_byte_duplicate", True, "200", "image/png",
            str(candidate.relative_to(root)), body, fingerprint,
            (1, fingerprint, "exact_byte_duplicate"), 0,
        )
        self.write_csv(root / "terminal_evidence.csv", pull_docs.EVIDENCE_FIELDS, [row])

    def run_checker(self, root: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "check_doc_pages_complete.py", str(root)],
            text=True, capture_output=True, cwd=Path(__file__).parent,
        )

    def test_checker_accepts_cryptographically_proven_duplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.build_exact_duplicate_artifact(root)
            result = self.run_checker(root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_checker_rejects_legacy_coarse_duplicate_without_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.build_exact_duplicate_artifact(root)
            (root / "terminal_evidence.csv").unlink()
            result = self.run_checker(root)
            self.assertEqual(result.returncode, 1)
            self.assertIn("terminal_proof_errors", result.stdout)

    def test_checker_rejects_tampered_candidate_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.build_exact_duplicate_artifact(root)
            candidate = next((root / "terminal_candidates").glob("*.png"))
            candidate.write_bytes(candidate.read_bytes() + b"tampered")
            result = self.run_checker(root)
            self.assertEqual(result.returncode, 1)

    def test_pull_keeps_near_match_and_stops_only_on_exact_pixels(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            doclist = root / "docs.txt"
            doclist.write_text("20260000002\n", encoding="utf-8")
            out = root / "out"
            first = Image.new("RGB", (512, 700), "white")
            ImageDraw.Draw(first).rectangle((30, 30, 480, 660), outline="black", width=3)
            second = first.copy()
            second.putpixel((250, 350), (0, 0, 0))
            first_body = png_bytes(first)
            second_body = png_bytes(second)
            responses = iter([
                ("ok", first_body, "200", "image/png"),
                ("ok", second_body, "200", "image/png"),
                ("ok", second_body, "200", "image/png"),
            ])
            argv = [
                "pull_docs.py", str(doclist), str(out), "--max-pages", "8",
                "--delay-min", "0", "--delay-max", "0",
            ]
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(pull_docs, "read_key", return_value="test"), \
                    mock.patch.object(pull_docs, "fetch_page", side_effect=lambda *_: next(responses)):
                self.assertEqual(pull_docs.main(), 0)
            self.assertTrue((out / "20260000002_1.png").is_file())
            self.assertTrue((out / "20260000002_2.png").is_file())
            self.assertFalse((out / "20260000002_3.png").exists())
            with (out / "terminal_evidence.csv").open(newline="", encoding="utf-8") as handle:
                evidence = list(csv.DictReader(handle))
            self.assertEqual(evidence[0]["event"], "perceptual_near_match_not_terminal")
            self.assertEqual(evidence[-1]["event"], "exact_byte_duplicate")
            self.assertEqual(self.run_checker(out).returncode, 0)

    def test_upstream_500_end_preserves_and_hashes_response_body(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            doclist = root / "docs.txt"
            doclist.write_text("20260000003\n", encoding="utf-8")
            out = root / "out"
            page = Image.new("RGB", (512, 700), "white")
            ImageDraw.Draw(page).text((50, 50), "SUBSTANTIVE PAGE", fill="black")
            body = png_bytes(page)
            end_body = b"upstream recorder page index ended"
            responses = iter([
                ("ok", body, "200", "image/png"),
                ("end", end_body, "500", "text/plain"),
            ])
            argv = [
                "pull_docs.py", str(doclist), str(out), "--max-pages", "8",
                "--delay-min", "0", "--delay-max", "0",
            ]
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(pull_docs, "read_key", return_value="test"), \
                    mock.patch.object(pull_docs, "fetch_page", side_effect=lambda *_: next(responses)):
                self.assertEqual(pull_docs.main(), 0)
            with (out / "terminal_evidence.csv").open(newline="", encoding="utf-8") as handle:
                evidence = list(csv.DictReader(handle))
            self.assertEqual(evidence[-1]["event"], "upstream_500_end")
            self.assertEqual(evidence[-1]["upstream_status"], "500")
            candidate = out / evidence[-1]["candidate_path"]
            self.assertEqual(candidate.read_bytes(), end_body)
            self.assertEqual(self.run_checker(out).returncode, 0)


if __name__ == "__main__":
    unittest.main()
