import csv
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock


def load_module():
    fake_index = types.SimpleNamespace(THROTTLE=(0, 0), RETRIES=0, fetch=lambda *_a, **_k: {})
    fake_class = types.SimpleNamespace(lead_class=lambda value: "classified:" + str(value))
    with mock.patch.dict(sys.modules, {"la_county_index": fake_index, "lead_class": fake_class}):
        spec = importlib.util.spec_from_file_location(
            "harvest_index_shard_under_test", Path(__file__).with_name("harvest_index_shard.py")
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


class ExactRetryTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_module()

    def test_shared_pacer_serializes_worker_request_starts(self):
        self.mod.NEXT_REQUEST_AT = 0.0
        monotonic = mock.Mock(side_effect=[100.0, 100.0])
        with mock.patch.object(self.mod.time, "monotonic", monotonic), \
             mock.patch.object(self.mod.time, "sleep") as sleep, \
             mock.patch.object(self.mod.random, "uniform", return_value=3.2):
            self.mod._pace_request()
            self.mod._pace_request()
        sleep.assert_called_once()
        self.assertAlmostEqual(sleep.call_args.args[0], 3.2)
        self.assertEqual(self.mod.NEXT_REQUEST_AT, 106.4)

    def test_exact_doc_retry_deduplicates_and_never_scans_gaps(self):
        seen = []

        def fetch(doc, **_kwargs):
            seen.append(doc)
            return {"doc_no": doc, "ok": True, "county_type": "NOTICE DEFAULT"}

        self.mod.fetch_resilient = fetch
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.csv"
            self.mod.harvest_docs([20260478123, 20260490000, 20260478123], out, conc=1)
            with out.open(newline="", encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
        self.assertEqual(seen, [20260478123, 20260490000])
        self.assertEqual([row["doc_no"] for row in rows], [str(x) for x in seen])

    def test_http_200_soft_wall_is_retryable(self):
        self.assertTrue(self.mod._is_throttled({"reason": "parse_no_row: Too many searches. Please wait a moment"}))
        self.assertFalse(self.mod._is_throttled({"reason": "not_found"}))


if __name__ == "__main__":
    unittest.main()
