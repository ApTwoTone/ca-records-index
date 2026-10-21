import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import rabbit_ain

AIN, DOC = "5555000111", "20240000005"


class Resp:
    def __init__(self, body, status=200):
        self.text, self.status_code = body if isinstance(body, str) else json.dumps(body), status


def page(total, docs, names=None):
    names = names or {}
    rows = "".join(f"<tr><td><a>{d}</a></td><td>2024-09-20</td><td>DEED<a>cart</a></td>"
                   f"<td>{names.get(d, ('A', 'B'))[0]}</td><td>{names.get(d, ('A', 'B'))[1]}</td></tr>" for d in docs)
    return Resp([f"{total} documents found", f"<table>{rows}</table>"])


def docs(start, n):
    return [str(20230000000 + i) for i in range(start, start + n)]


WALL = Resp(["Too many searches. Please wait a moment.", ""])
NONE = Resp(["No documents found", ""])


class WalkTests(unittest.TestCase):
    def setUp(self):
        for target in ("_pace_request", "time.sleep"):
            patcher = mock.patch(f"rabbit_ain.{target}")
            patcher.start()
            self.addCleanup(patcher.stop)

    def walk(self, *answers):
        session = mock.Mock()
        session.post.side_effect = list(answers)
        return rabbit_ain.walk(session, AIN, DOC), session.post.call_args_list

    def test_proven_on_page_2_stops_early(self):
        result, calls = self.walk(page(30, docs(0, 10)), page(30, docs(10, 4) + [DOC] + docs(14, 5)))
        self.assertEqual(result, ("proven", 2, 2))
        self.assertEqual([c.kwargs["data"]["page"] for c in calls], ["1", "2"])
        self.assertEqual(calls[0].kwargs["data"]["ain"], AIN)
        self.assertEqual(calls[0].kwargs["data"]["doc_no"], "")

    def test_not_in_chain_after_two_full_pages_and_a_short_last(self):
        result, _ = self.walk(page(23, docs(0, 10)), page(23, docs(10, 10)), page(23, docs(20, 3)))
        self.assertEqual(result, ("not_in_chain", 3, 3))

    def test_no_docs(self):
        self.assertEqual(self.walk(NONE)[0], ("no_docs", 0, 1))

    def test_a_later_page_with_no_documents_is_incomplete(self):
        self.assertEqual(self.walk(page(23, docs(0, 10)), NONE)[0][0], "incomplete: page 2 says no documents")

    def test_total_changes_is_incomplete(self):
        state = self.walk(page(23, docs(0, 10)), page(24, docs(10, 10)))[0][0]
        self.assertEqual(state, "incomplete: the total changed from 23 to 24 on page 2")

    def test_a_repeated_page_is_incomplete(self):
        self.assertEqual(self.walk(page(23, docs(0, 10)), page(23, docs(0, 10)))[0][0],
                         "incomplete: page 2 only repeats rows")

    def test_a_short_page_before_the_total_is_incomplete(self):
        state = self.walk(page(23, docs(0, 10)), page(23, docs(10, 4)))[0][0]
        self.assertEqual(state, "incomplete: page 2 is short (14 of 23 rows)")

    def test_soft_wall_then_success(self):
        result, calls = self.walk(WALL, WALL, page(3, docs(0, 2) + [DOC]))
        self.assertEqual(result, ("proven", 1, 3))
        self.assertEqual({c.kwargs["data"]["page"] for c in calls}, {"1"})
        self.assertEqual([c.args for c in rabbit_ain.time.sleep.call_args_list], [(6,), (10,)])

    def test_still_walled_after_three_reasks(self):
        self.assertEqual(self.walk(WALL, WALL, WALL, WALL)[0], ("walled", 0, 4))

    def test_errors_are_asked_once(self):
        self.assertEqual(self.walk(Resp("oops", 500))[0], ("error_http_500", 0, 1))
        self.assertEqual(self.walk(Resp("<html>"))[0], ("error_non_json", 0, 1))
        self.assertEqual(self.walk(OSError("reset"))[0], ("error_OSError", 0, 1))


class ShardTests(unittest.TestCase):
    def run_shard(self, pairs, answers, salt=""):
        session = mock.Mock()
        session.post.side_effect = list(answers)
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch("rabbit_ain._pace_request"), mock.patch("rabbit_ain.time.sleep"), \
                mock.patch("rabbit_ain.idx._session", return_value=session), \
                mock.patch.dict("os.environ", {"NAME_SALT": salt}):
            (Path(tmp) / "pairs.txt").write_text(" ".join(pairs))
            with mock.patch("sys.argv", ["rabbit_ain.py", f"{tmp}/pairs.txt", f"{tmp}/out"]):
                rabbit_ain.main()
            self.assertEqual(sorted(p.name for p in (Path(tmp) / "out").iterdir()), ["companions.csv", "status.csv"])
            rows = list(csv.DictReader((Path(tmp) / "out" / "status.csv").read_text().splitlines()))
            self.companions = (Path(tmp) / "out" / "companions.csv").read_text()
        return [r["state"] for r in rows], session.post.call_count

    def companion_page(self):
        plus2, minus7 = str(int(DOC) + 2), str(int(DOC) - 7)
        names = {plus2: ("JOHN Q SMITH", "SMITH FAMILY TRUST<br>JOHN Q SMITH"), minus7: ("JOHN Q SMITH", "X")}
        return page(3, [minus7, plus2, DOC], names), plus2

    def test_neighbours_within_5_are_reported_with_salted_name_hashes(self):
        answer, plus2 = self.companion_page()
        self.assertEqual(self.run_shard([f"{AIN}:{DOC}"], [answer], salt="test-salt")[0], ["proven"])
        rows = list(csv.DictReader(self.companions.splitlines()))
        with mock.patch.dict("os.environ", {"NAME_SALT": "test-salt"}):
            want = rabbit_ain.token_hashes(["JOHN Q SMITH", "SMITH FAMILY TRUST"])
        self.assertEqual(len(want), 2)  # JOHN, SMITH; Q, FAMILY, TRUST dropped
        self.assertEqual(rows, [{"ain": AIN, "doc_no": DOC, "companion": plus2, "record_date": "2024-09-20",
                                 "county_type": "DEED", "tokens": " ".join(want)}])
        self.assertNotIn("SMITH", self.companions.upper())

    def test_no_salt_writes_no_tokens(self):
        answer, plus2 = self.companion_page()
        self.run_shard([f"{AIN}:{DOC}"], [answer])
        rows = list(csv.DictReader(self.companions.splitlines()))
        self.assertEqual([(r["companion"], r["tokens"]) for r in rows], [(plus2, "")])

    def test_403_stops_the_shard(self):
        pairs = [f"{AIN}:{DOC}", f"5555000222:{DOC}", f"5555000333:{DOC}"]
        states, sent = self.run_shard(pairs, [NONE, Resp("", 403)])
        self.assertEqual(states[:2], ["no_docs", "http_403"])
        self.assertEqual(states[2], f"not_asked: http_403 on 5555000222:{DOC}")
        self.assertEqual(sent, 2)

    def test_three_walled_pairs_stop_the_shard(self):
        pairs = [f"555500000{i}:{DOC}" for i in range(5)]
        states, sent = self.run_shard(pairs, [WALL] * 12)
        self.assertEqual(states[:3], ["walled"] * 3)
        self.assertTrue(all(s.startswith("not_asked: 3 pairs walled") for s in states[3:]))
        self.assertEqual(sent, 12)

    def test_a_bad_pair_list_is_refused(self):
        with self.assertRaises(SystemExit):
            self.run_shard([f"{AIN}:3024"], [])


if __name__ == "__main__":
    unittest.main()
