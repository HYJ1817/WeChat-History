"""Small synthetic regression test for the production paging benchmark."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import unittest


class BenchmarkMessageQueriesTests(unittest.TestCase):
    def test_small_synthetic_b2_b3_and_legacy_contracts(self) -> None:
        script = Path(__file__).resolve().parents[2] / "scripts" / "Benchmark-MessageQueries.py"
        completed = subprocess.run(
            [sys.executable, "-I", "-B", "-X", "utf8", str(script), "--self-test", "--no-tracemalloc"],
            capture_output=True, text=True, encoding="utf-8", timeout=180, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertTrue(report["checks"]["passed"], report["checks"]["failures"])
        self.assertTrue(report["fixtures_unchanged"])
        self.assertEqual(report["source_files_changed_during_run"], [])
        results = {item["case"]: item for item in report["results"]}
        self.assertTrue({
            "b2_first", "b2_deep", "b2_reverse", "b3_keyword_first",
            "b3_keyword_continuation", "global_sparse", "parser_sample",
        }.issubset(results))
        for name in ("b2_first", "b2_deep", "b2_reverse", "b3_keyword_first", "b3_keyword_continuation"):
            self.assertEqual(results[name]["status"], "ok", name)


if __name__ == "__main__":
    unittest.main()
