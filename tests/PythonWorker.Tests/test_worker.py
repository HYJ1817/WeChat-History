"""Behaviour tests for the standalone demo worker.

Run from the project root:
  .venv\\Scripts\\python.exe -I -X utf8 -m unittest discover -s tests/PythonWorker.Tests -v
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
WORKER_DIR = ROOT / "src" / "PythonWorker"
WORKER = WORKER_DIR / "worker.py"
DEMO = ROOT / "demo" / "conversations.json"
sys.path.insert(0, str(WORKER_DIR))
import worker  # noqa: E402


def request(request_id, command, payload=None):
    return {
        "protocolVersion": 1,
        "requestId": request_id,
        "type": "request",
        "command": command,
        "payload": payload or {},
    }


def run_session(requests, demo=DEMO):
    raw = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in requests).encode("utf-8")
    process = subprocess.run(
        [sys.executable, "-I", "-X", "utf8", "-u", str(WORKER), "--demo", str(demo)],
        input=raw, capture_output=True, timeout=60, cwd=str(ROOT),
    )
    if process.returncode != 0:
        raise AssertionError(process.stderr.decode("utf-8", "replace"))
    lines = process.stdout.decode("utf-8").splitlines()
    responses = [json.loads(line) for line in lines]  # every stdout line must be protocol JSON
    return responses, process


class DemoDataTests(unittest.TestCase):
    def test_counts_and_fictional_notice(self):
        conversations = worker.load_demo(DEMO)
        self.assertEqual([(c.id, len(c.messages)) for c in conversations],
                         [("demo-chat-001", 12), ("demo-chat-002", 120), ("demo-chat-empty", 0)])

    def test_naive_timestamp_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="worker 测试 ") as folder:
            bad = Path(folder) / "bad.json"
            bad.write_text(json.dumps({"version": 1, "conversations": [{
                "id": "c", "name": "n", "kind": "direct",
                "messages": [{"id": "m", "sender": "s", "timestamp": "2026-11-24T09:00:00",
                              "type": "text", "content": "no zone"}],
            }]}), encoding="utf-8")
            with self.assertRaises(worker.WorkerError) as raised:
                worker.load_demo(bad)
            self.assertEqual(raised.exception.code, "SOURCE_INVALID")

    def test_demo_image_requires_an_existing_file(self):
        from dataclasses import replace

        from PIL import Image

        conversation = worker.load_demo(DEMO)[0]
        image_item = next(item for item in conversation.messages if item.kind == "image")
        self.assertIs(
            worker._to_export_message(conversation, image_item).type,
            worker.MessageType.SYSTEM,
        )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "generated.png"
            Image.new("RGB", (8, 8), (10, 20, 30)).save(path)
            real_item = replace(image_item, content=str(path))
            self.assertIs(
                worker._to_export_message(conversation, real_item).type,
                worker.MessageType.IMAGE,
            )

    def test_filter_includes_both_selected_dates_only(self):
        conversation = worker.load_demo(DEMO)[0]
        selected = worker.filter_messages(conversation, date(2026, 11, 24), date(2026, 11, 24))
        self.assertEqual([m.id for m in selected], ["m005", "m006", "m007", "m008"])
        self.assertEqual(selected[-1].timestamp.isoformat(), "2026-11-24T23:59:59+08:00")

    def test_reversed_dates_are_rejected(self):
        conversation = worker.load_demo(DEMO)[0]
        with self.assertRaises(worker.WorkerError) as raised:
            worker.filter_messages(conversation, date(2026, 11, 25), date(2026, 11, 24))
        self.assertEqual(raised.exception.code, "INVALID_ARGUMENT")


class ProtocolTests(unittest.TestCase):
    def test_hello_list_and_shutdown(self):
        responses, process = run_session([
            request("r1", "hello"),
            request("r2", "list_conversations", {"source": "demo"}),
            request("r3", "shutdown"),
        ])
        self.assertEqual([r["requestId"] for r in responses], ["r1", "r2", "r3"])
        self.assertTrue(all(r["ok"] for r in responses))
        self.assertEqual(responses[0]["result"]["protocolVersion"], 1)
        self.assertEqual(responses[0]["result"]["timezone"], "UTC+08:00")
        self.assertEqual(responses[1]["result"]["items"][0]["title"], "演示 · 产品讨论（虚构）")
        self.assertEqual(responses[1]["result"]["items"][0]["messageCount"], 12)
        self.assertTrue(responses[2]["result"]["shutdown"])

    def test_malformed_json_gets_one_error_with_null_id(self):
        raw = b"{not json}\n"
        process = subprocess.run(
            [sys.executable, "-I", "-X", "utf8", "-u", str(WORKER), "--demo", str(DEMO)],
            input=raw, capture_output=True, timeout=30, cwd=str(ROOT))
        self.assertEqual(process.returncode, 0)
        response = json.loads(process.stdout.decode("utf-8"))
        self.assertIsNone(response["requestId"])
        self.assertEqual(response["error"]["code"], "INVALID_JSON")

    def test_unknown_command_and_wrong_version(self):
        responses, _ = run_session([
            request("a", "no_such_command"),
            {"protocolVersion": 2, "requestId": "b", "type": "request", "command": "hello", "payload": {}},
        ])
        self.assertEqual([r["error"]["code"] for r in responses], ["UNKNOWN_COMMAND", "PROTOCOL_MISMATCH"])

    def test_duplicate_request_id_is_rejected_without_replaying(self):
        responses, _ = run_session([request("same", "hello"), request("same", "hello")])
        self.assertTrue(responses[0]["ok"])
        self.assertEqual(responses[1]["error"]["code"], "DUPLICATE_REQUEST")

    def test_invalid_arguments(self):
        cases = [
            (request("x1", "get_messages", {"conversationId": "demo-chat-001", "limit": 0}), "INVALID_ARGUMENT"),
            (request("x2", "get_messages", {"conversationId": "demo-chat-001", "limit": 101}), "INVALID_ARGUMENT"),
            (request("x3", "get_messages", {"conversationId": "demo-chat-001", "limit": True}), "INVALID_ARGUMENT"),
            (request("x4", "get_messages", {"conversationId": "demo-chat-001", "startDate": "2026/11/24"}), "INVALID_ARGUMENT"),
            (request("x5", "get_messages", {"conversationId": "demo-chat-001", "startDate": "2026-11-25", "endDate": "2026-11-24"}), "INVALID_ARGUMENT"),
            (request("x6", "get_messages", {"conversationId": "missing"}), "CONVERSATION_NOT_FOUND"),
            (request("x7", "list_conversations", {"source": "wechat"}), "SOURCE_NOT_FOUND"),
            (request("x8", "get_messages", {"conversationId": "demo-chat-001", "order": "newest"}), "INVALID_ARGUMENT"),
        ]
        responses, _ = run_session([item for item, _ in cases])
        self.assertEqual([r["error"]["code"] for r in responses], [code for _, code in cases])

    def test_stdout_contains_only_protocol_json(self):
        _, process = run_session([request("a", "hello"), request("b", "list_conversations", {})])
        for line in process.stdout.decode("utf-8").splitlines():
            parsed = json.loads(line)
            self.assertEqual(parsed["protocolVersion"], 1)
            self.assertEqual(parsed["type"], "response")

    def test_worker_does_not_import_qt(self):
        self.assertEqual([name for name in sys.modules if "PySide" in name], [])


class MessageTests(unittest.TestCase):
    def test_date_filtering_over_pipe(self):
        responses, _ = run_session([
            request("m1", "get_messages", {"conversationId": "demo-chat-001",
                                           "startDate": "2026-11-24", "endDate": "2026-11-24",
                                           "offset": 0, "limit": 50}),
            request("m2", "get_messages", {"conversationId": "demo-chat-empty", "offset": 0, "limit": 50}),
        ])
        result = responses[0]["result"]
        self.assertEqual(result["total"], 4)
        self.assertIsNone(result["nextOffset"])
        self.assertEqual([item["kind"] for item in result["items"]], ["text", "text", "voice", "text"])
        self.assertTrue(all(item["timestamp"].endswith("+08:00") for item in result["items"]))
        self.assertEqual(responses[1]["result"], {"items": [], "total": 0, "nextOffset": None})

    def test_pagination_never_returns_everything_at_once(self):
        requests = [request(f"p{page}", "get_messages",
                            {"conversationId": "demo-chat-002", "offset": page * 50, "limit": 50})
                    for page in range(3)]
        responses, _ = run_session(requests)
        sizes = [len(r["result"]["items"]) for r in responses]
        offsets = [r["result"]["nextOffset"] for r in responses]
        self.assertEqual(sizes, [50, 50, 20])
        self.assertEqual(offsets, [50, 100, None])
        self.assertEqual(responses[0]["result"]["total"], 120)

    def test_desc_order_returns_newest_first_while_default_stays_chronological(self):
        responses, _ = run_session([
            request("desc", "get_messages", {"conversationId": "demo-chat-001", "order": "desc",
                                             "offset": 0, "limit": 50}),
            request("asc", "get_messages", {"conversationId": "demo-chat-001", "offset": 0, "limit": 50}),
        ])
        desc = [item["id"] for item in responses[0]["result"]["items"]]
        asc = [item["id"] for item in responses[1]["result"]["items"]]
        self.assertEqual(desc, list(reversed(asc)))
        self.assertEqual(responses[0]["result"]["total"], responses[1]["result"]["total"])

    def test_pagination_does_not_include_next_offset_past_total(self):
        responses, _ = run_session([request("q", "get_messages",
                                            {"conversationId": "demo-chat-002", "offset": 118, "limit": 50})])
        self.assertEqual(len(responses[0]["result"]["items"]), 2)
        self.assertIsNone(responses[0]["result"]["nextOffset"])


class ExportTests(unittest.TestCase):
    def _output(self, folder, name="会话 😀 导出.md"):
        nested = Path(folder) / "中文 目录"
        nested.mkdir()
        return nested / name

    def test_export_writes_real_markdown_with_filter_and_order(self):
        with tempfile.TemporaryDirectory(prefix="wechat memory 演示 ") as folder:
            output = self._output(folder)
            responses, _ = run_session([request("e", "export_markdown", {
                "conversationId": "demo-chat-001",
                "startDate": "2026-11-24", "endDate": "2026-11-24",
                "outputPath": str(output), "overwrite": False,
            })])
            result = responses[0]["result"]
            self.assertTrue(output.is_file())
            self.assertEqual(result["messageCount"], 4)
            self.assertEqual(result["bytes"], output.stat().st_size)
            document = output.read_text(encoding="utf-8")
            self.assertTrue(document.startswith("# 演示 · 产品讨论（虚构）"))
            self.assertIn("Conversation ID: `demo-chat-001`", document)
            self.assertIn("## 2026-11-24", document)
            self.assertIn("**Chen Yu** · 00:00", document)
            self.assertIn("**Chen Yu** · 23:59", document)
            self.assertIn("界面和导出必须一致 ✅", document)
            self.assertLess(document.index("09:30"), document.index("12:05"))
            self.assertLess(document.index("12:05"), document.index("23:59"))
            self.assertNotIn("09:12", document)   # 23rd
            self.assertNotIn("08:15", document)   # 25th
            self.assertEqual(document.count("## "), 1)

    def test_media_is_an_explicit_placeholder_not_a_fake_link(self):
        with tempfile.TemporaryDirectory() as folder:
            output = self._output(folder, "all.md")
            run_session([request("e", "export_markdown", {
                "conversationId": "demo-chat-001", "outputPath": str(output), "overwrite": False})])
            document = output.read_text(encoding="utf-8")
            self.assertIn("【图片占位】", document)
            self.assertNotIn("![Image attachment]", document)
            self.assertNotIn("Voice transcript", document)
            self.assertNotIn("File attachment:", document)
            self.assertIn("_【语音占位】", document)

    def test_empty_result_is_explicitly_marked(self):
        with tempfile.TemporaryDirectory() as folder:
            output = self._output(folder, "empty.md")
            responses, _ = run_session([request("e", "export_markdown", {
                "conversationId": "demo-chat-001", "startDate": "2026-11-26", "endDate": "2026-11-26",
                "outputPath": str(output), "overwrite": False})])
            self.assertEqual(responses[0]["result"]["messageCount"], 0)
            self.assertIn("没有消息", output.read_text(encoding="utf-8"))

    def test_existing_file_requires_explicit_overwrite(self):
        with tempfile.TemporaryDirectory() as folder:
            output = self._output(folder, "exists.md")
            output.write_text("keep me", encoding="utf-8")
            responses, _ = run_session([request("e1", "export_markdown", {
                "conversationId": "demo-chat-001", "outputPath": str(output), "overwrite": False})])
            self.assertEqual(responses[0]["error"]["code"], "OUTPUT_EXISTS")
            self.assertEqual(output.read_text(encoding="utf-8"), "keep me")
            responses, _ = run_session([request("e2", "export_markdown", {
                "conversationId": "demo-chat-001", "outputPath": str(output), "overwrite": True})])
            self.assertTrue(responses[0]["ok"])
            self.assertTrue(output.read_text(encoding="utf-8").startswith("# 演示"))

    def test_unwritable_and_invalid_output_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            missing_parent = Path(folder) / "不存在" / "x.md"
            responses, _ = run_session([request("e1", "export_markdown", {
                "conversationId": "demo-chat-001", "outputPath": str(missing_parent)})])
            self.assertEqual(responses[0]["error"]["code"], "OUTPUT_NOT_WRITABLE")
        for bad_path in ("relative.md",):
            responses, _ = run_session([request("e2", "export_markdown", {
                "conversationId": "demo-chat-001", "outputPath": bad_path})])
            self.assertEqual(responses[0]["error"]["code"], "INVALID_ARGUMENT")

    def test_missing_or_corrupt_source_is_reported(self):
        responses, _ = run_session([request("h", "hello"),
                                    request("l", "list_conversations", {})],
                                   demo=ROOT / "demo" / "does-not-exist.json")
        self.assertTrue(responses[0]["ok"])
        self.assertEqual(responses[1]["error"]["code"], "SOURCE_NOT_FOUND")
        with tempfile.TemporaryDirectory() as folder:
            corrupt = Path(folder) / "corrupt.json"
            corrupt.write_text("{broken", encoding="utf-8")
            responses, _ = run_session([request("l", "list_conversations", {})], demo=corrupt)
            self.assertEqual(responses[0]["error"]["code"], "SOURCE_INVALID")


class AccountListTests(unittest.TestCase):
    def test_account_items_use_protocol_camel_case(self):
        from wechat_memory.wechat4_source import AccountItem, WeChat4Source

        items = [AccountItem("token", "微信账户（最近使用）", "2026-11-27T10:00:00+08:00", True)]
        with patch.object(WeChat4Source, "discover_accounts", return_value=items):
            result = worker.handle_list_accounts({}, None)
        self.assertEqual(result["items"], [{
            "token": "token",
            "label": "微信账户（最近使用）",
            "lastUsed": "2026-11-27T10:00:00+08:00",
            "recent": True,
        }])


class JobExportTests(unittest.TestCase):
    def test_single_day_job_manifest_uses_the_selected_local_date(self):
        with tempfile.TemporaryDirectory(prefix="worker-job-") as folder:
            root = Path(folder)
            context = worker.Context(
                str(DEMO),
                jobs_dir=str(root / "jobs"),
                scenes_file=str(root / "scenes.json"),
            )
            result = worker.handle_export_job({
                "source": "demo",
                "conversationId": "demo-chat-001",
                "startDate": "2026-11-24",
                "endDate": "2026-11-24",
                "formats": ["markdown"],
                "target": {"kind": "none"},
            }, context)
            job = result["job"]
            self.assertEqual(job["start"], "2026-11-23T16:00:00Z")
            self.assertEqual(job["end"], "2026-11-24T15:59:59Z")
            files = Path(job["directory"]) / "files"
            self.assertTrue((files / "chat.md").is_file())


if __name__ == "__main__":
    unittest.main()
