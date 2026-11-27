"""Fixed-code tests for the real WeChat adapter's error mapping."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKER_DIR = ROOT / "src" / "PythonWorker"
VENDOR_DIR = WORKER_DIR / "vendor"
sys.path.insert(0, str(VENDOR_DIR))
sys.path.insert(0, str(WORKER_DIR))

from wechat_context_exporter.sources.base import SourceError  # noqa: E402
from wechat_memory.wechat4_source import WeChat4Source  # noqa: E402


class ErrorMappingTests(unittest.TestCase):
    def _map(self, text: str):
        return WeChat4Source._map_source_error(SourceError(text), "key")

    def test_attach_failure_is_actionable(self):
        failure = self._map("Could not attach while WeChat was starting: backend error")
        self.assertEqual(failure.code, "WECHAT_KEY_UNAVAILABLE")
        self.assertIn("微信进程", failure.message)

    def test_close_timeout_is_actionable(self):
        failure = self._map("WeChat did not close in time")
        self.assertEqual(failure.code, "WECHAT_CONNECT_FAILED")
        self.assertIn("退出微信", failure.message)

    def test_unknown_failure_stays_fixed(self):
        failure = self._map("something unexpected happened")
        self.assertEqual(failure.code, "WECHAT_CONNECT_FAILED")
        self.assertTrue(failure.message)
        self.assertNotIn("something unexpected", failure.message)


if __name__ == "__main__":
    unittest.main()
