import email.message
import importlib.util
import io
import unittest
import urllib.error
from pathlib import Path
from unittest import mock


def load_module():
    spec = importlib.util.spec_from_file_location("rabbit_pages_under_test", Path(__file__).with_name("rabbit_pages.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def http_500(**headers):
    msg = email.message.Message()
    for name, value in headers.items():
        msg[name] = value
    return urllib.error.HTTPError("https://worker/thumb/1/3", 500, "Internal Server Error", msg, io.BytesIO(b"oops"))


class WorkerFiveHundredTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_module()

    def ask(self, error):
        with mock.patch.object(self.mod.urllib.request, "urlopen", side_effect=error):
            return self.mod.ask("key", "20260000001", 3)[0]

    def test_netr_500_through_the_worker_is_the_end_of_the_document(self):
        self.assertEqual(self.ask(http_500(**{"X-Upstream-Status": "500"})), "end")

    def test_the_workers_own_bare_500_is_an_error_rabbit_asks_again(self):
        self.assertEqual(self.ask(http_500()), "error_500")


class PageImageTests(unittest.TestCase):
    def test_a_grey_jpeg_page_is_a_page(self):
        mod = load_module()
        resp = mock.MagicMock()
        resp.__enter__.return_value = resp
        resp.headers = {"X-Upstream-Status": "200", "Content-Type": "image/jpeg"}
        resp.read.return_value = b"\xff\xd8\xff\xe0" + b"\0" * 6000
        with mock.patch.object(mod.urllib.request, "urlopen", return_value=resp):
            self.assertEqual(mod.ask("key", "20260761134", 1)[0], "page")


if __name__ == "__main__":
    unittest.main()
