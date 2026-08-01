import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import verify_ain_doc_candidates as proof


class Response:
    def __init__(self, payload=None, status=200, text=None):
        self.status_code = status
        self.text = text if text is not None else json.dumps(payload)


def page(count, docs):
    rows = "".join(
        f"<tr><td>{doc}</td><td>2026-07-01</td><td>NOTICE DEFAULT</td><td>A</td><td>B</td></tr>"
        for doc in docs
    )
    return [count, rows]


class ReverseProofTests(unittest.TestCase):
    def test_throttle_never_becomes_not_found(self):
        session = mock.Mock()
        session.post.return_value = Response(["Too many searches. Please wait a moment", ""])
        with tempfile.TemporaryDirectory() as td, mock.patch.object(proof.time, "sleep"):
            result = proof.prove_candidate(session, {"ain": "1234567890", "doc_no": "20260000001"},
                                           Path(td), 2, 0, 0, 2)
        self.assertEqual(result["outcome"], "THROTTLED")
        self.assertFalse(result["proved"])

    def test_expected_doc_on_second_page_is_proven(self):
        session = mock.Mock()
        session.post.side_effect = [
            Response(page("2 documents; only the most recent", ["20260000001"])),
            Response(page("2 documents", ["20260000002"])),
        ]
        with tempfile.TemporaryDirectory() as td, mock.patch.object(proof.time, "sleep"):
            result = proof.prove_candidate(session, {"ain": "1234567890", "doc_no": "20260000002"},
                                           Path(td), 1, 0, 0, 3)
        self.assertEqual(result["outcome"], "PROVEN")
        self.assertEqual(result["matched_row"]["doc_no"], "20260000002")

    def test_valid_chain_without_expected_doc_is_not_not_found(self):
        session = mock.Mock()
        session.post.return_value = Response(page("1 document", ["20260000001"]))
        with tempfile.TemporaryDirectory() as td:
            result = proof.prove_candidate(session, {"ain": "1234567890", "doc_no": "20260000002"},
                                           Path(td), 1, 0, 0, 1)
        self.assertEqual(result["outcome"], "EXPECTED_DOC_NOT_IN_CHAIN")

    def test_http_error_is_separate_from_not_found(self):
        session = mock.Mock()
        session.post.return_value = Response(status=429, text="rate wall")
        with tempfile.TemporaryDirectory() as td, mock.patch.object(proof.time, "sleep"):
            result = proof.prove_candidate(session, {"ain": "1234567890", "doc_no": "20260000002"},
                                           Path(td), 2, 0, 0, 1)
        self.assertEqual(result["outcome"], "HTTP_ERROR")
        self.assertEqual(result["request_receipts"][-1]["http_status"], 429)

    def test_not_found_requires_explicit_valid_no_documents_response(self):
        session = mock.Mock()
        session.post.return_value = Response(["No documents found", ""])
        with tempfile.TemporaryDirectory() as td:
            result = proof.prove_candidate(session, {"ain": "1234567890", "doc_no": "20260000002"},
                                           Path(td), 1, 0, 0, 1)
        self.assertEqual(result["outcome"], "NOT_FOUND")
        self.assertEqual(result["request_receipts"][0]["http_status"], 200)


if __name__ == "__main__":
    unittest.main()
