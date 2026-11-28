"""Offline JSON import validation and transaction tests.

Only synthetic data and temporary files are used.  No WeChat process, account,
network, or JSON-referenced media is opened.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKER_DIR = ROOT / "src" / "PythonWorker"
VENDOR_DIR = WORKER_DIR / "vendor"
sys.path.insert(0, str(VENDOR_DIR))
sys.path.insert(0, str(WORKER_DIR))

from wechat_context_exporter.models import Conversation, ConversationKind, Message, MessageType  # noqa: E402
from wechat_memory.exporters import export_conversation  # noqa: E402
from wechat_memory.offline_import import load_json_file  # noqa: E402
import worker  # noqa: E402


def request(request_id: str, command: str, payload: dict | None = None) -> dict:
    return {
        "protocolVersion": 1,
        "requestId": request_id,
        "type": "request",
        "command": command,
        "payload": payload or {},
    }


def run_session(requests: list[dict], demo: Path) -> list[dict]:
    raw = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in requests).encode("utf-8")
    process = subprocess.run(
        [sys.executable, "-I", "-X", "utf8", "-u", str(WORKER_DIR / "worker.py"), "--demo", str(demo)],
        input=raw,
        capture_output=True,
        timeout=60,
        cwd=str(ROOT),
    )
    if process.returncode != 0:
        raise AssertionError(process.stderr.decode("utf-8", "replace"))
    return [json.loads(line) for line in process.stdout.decode("utf-8").splitlines()]


class OfflineImportTests(unittest.TestCase):
    def write_json(self, folder: str, payload: object, name: str = "source.json") -> Path:
        path = Path(folder) / name
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def test_application_json_round_trip_preserves_conversation_and_message(self) -> None:
        with tempfile.TemporaryDirectory(prefix="offline-round-trip-") as folder:
            output = Path(folder) / "export.json"
            conversation = Conversation("offline-conv", "合成离线会话", ConversationKind.GROUP)
            message = Message(
                id="m-1",
                conversation_id=conversation.id,
                sender="合成发送者",
                timestamp=datetime(2026, 11, 28, 9, 30, tzinfo=timezone.utc),
                type=MessageType.TEXT,
                content="导出后重新导入 ✅",
                is_outgoing=True,
                reply_to="m-0",
            )
            export_conversation(conversation, [message], output, "json", materialize_media=False)

            imported = load_json_file(output)

            self.assertEqual(imported.schema, "wechat-memory-json-v1")
            self.assertEqual(imported.message_count, 1)
            self.assertEqual(imported.conversations[0].id, conversation.id)
            restored = imported.conversations[0].messages[0]
            self.assertEqual(restored.timestamp.isoformat(), "2026-11-28T17:30:00+08:00")
            self.assertTrue(restored.is_outgoing)
            self.assertEqual(restored.reply_to, "m-0")
            self.assertEqual(restored.content, message.content)

    def test_legacy_demo_shape_with_naive_timestamp_is_explicitly_adapted(self) -> None:
        source = ROOT / "reference" / "wechat-ai-memory" / "examples" / "demo_chat.json"

        imported = load_json_file(source)

        self.assertEqual(imported.schema, "wechat-memory-json-v1")
        self.assertEqual(len(imported.conversations), 2)
        self.assertEqual(
            imported.conversations[0].messages[0].timestamp.tzinfo,
            timezone(timedelta(hours=8)),
        )
        self.assertEqual(
            imported.conversations[0].messages[0].timestamp.isoformat(),
            "2026-08-21T09:12:00+08:00",
        )

    def test_invalid_structure_timestamp_and_id_are_rejected(self) -> None:
        cases = [
            ({"version": 1, "conversations": {}}, "SOURCE_INVALID"),
            ({"version": 1, "conversations": [{"id": "c", "name": "n"}]}, "SOURCE_INVALID"),
            ({"version": 1, "conversations": [{"id": "c", "name": "n", "messages": [{
                "id": "m", "sender": "s", "timestamp": "not-a-time", "type": "text", "content": "x"
            }]}]}, "SOURCE_INVALID"),
            ({"version": 1, "conversations": [{"id": "c", "name": "n", "messages": [{
                "id": "m\nline", "sender": "s", "timestamp": "2026-11-28T09:00:00+08:00", "type": "text", "content": "x"
            }]}]}, "SOURCE_INVALID"),
        ]
        with tempfile.TemporaryDirectory(prefix="offline-invalid-") as folder:
            for index, (payload, code) in enumerate(cases):
                with self.subTest(index=index):
                    with self.assertRaises(worker.WorkerError) as raised:
                        worker.handle_import_json({"path": str(self.write_json(folder, payload, f"bad-{index}.json"))}, worker.Context("unused"))
                    self.assertEqual(raised.exception.code, code)

    def test_utf8_and_empty_history_are_distinguished_from_parse_failure(self) -> None:
        with tempfile.TemporaryDirectory(prefix="offline-encoding-") as folder:
            invalid_utf8 = Path(folder) / "invalid-utf8.json"
            invalid_utf8.write_bytes(b"{\"version\":1,\"conversations\":[]}" + b"\x80")
            with self.assertRaises(worker.WorkerError) as raised:
                worker.handle_import_json({"path": str(invalid_utf8)}, worker.Context("unused"))
            self.assertEqual(raised.exception.code, "SOURCE_INVALID")

            empty = self.write_json(folder, {"version": 1, "conversations": []}, "empty.json")
            imported = load_json_file(empty)
            self.assertEqual(imported.conversations, ())
            self.assertEqual(imported.message_count, 0)

    def test_unknown_version_and_oversized_shape_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="offline-limits-") as folder:
            unknown = self.write_json(folder, {"version": 2, "conversations": []}, "future.json")
            with self.assertRaises(worker.WorkerError) as raised:
                worker.handle_import_json({"path": str(unknown)}, worker.Context("unused"))
            self.assertEqual(raised.exception.code, "SOURCE_UNSUPPORTED")

            too_many = {"version": 1, "conversations": [
                {"id": f"c-{index}", "name": "会话", "messages": []}
                for index in range(10_001)
            ]}
            oversized = self.write_json(folder, too_many, "too-many.json")
            with self.assertRaises(worker.WorkerError) as raised:
                worker.handle_import_json({"path": str(oversized)}, worker.Context("unused"))
            self.assertEqual(raised.exception.code, "SOURCE_TOO_LARGE")

    def test_protocol_import_failure_keeps_demo_then_switches_to_snapshot(self) -> None:
        with tempfile.TemporaryDirectory(prefix="offline-protocol-") as folder:
            invalid = Path(folder) / "invalid.json"
            invalid.write_text("{broken", encoding="utf-8")
            valid = self.write_json(folder, {
                "version": 1,
                "conversations": [{
                    "id": "protocol-offline",
                    "name": "协议离线会话",
                    "kind": "direct",
                    "messages": [{
                        "id": "m1",
                        "sender": "合成发送者",
                        "timestamp": "2026-11-28T09:00:00+08:00",
                        "type": "text",
                        "content": "协议导入成功",
                    }],
                }],
            })
            responses = run_session([
                request("h", "hello"),
                request("before", "list_conversations", {"source": "demo"}),
                request("bad", "import_json", {"path": str(invalid)}),
                request("after-bad", "list_conversations", {"source": "demo"}),
                request("good", "import_json", {"path": str(valid)}),
                request("offline", "list_conversations", {"source": "offline"}),
                request("shutdown", "shutdown"),
            ], ROOT / "demo" / "conversations.json")
            self.assertTrue(responses[0]["ok"])
            self.assertEqual(responses[2]["error"]["code"], "SOURCE_INVALID")
            self.assertEqual(responses[1]["result"], responses[3]["result"])
            self.assertEqual(responses[4]["result"]["source"], "offline")
            self.assertEqual(responses[5]["result"]["items"][0]["id"], "protocol-offline")
            self.assertTrue(responses[6]["result"]["shutdown"])

    def test_failed_import_keeps_existing_demo_source_and_success_is_snapshot(self) -> None:
        with tempfile.TemporaryDirectory(prefix="offline-transaction-") as folder:
            context = worker.Context(str(ROOT / "demo" / "conversations.json"))
            before = worker.handle_list_conversations({"source": "demo"}, context)
            invalid = Path(folder) / "invalid.json"
            invalid.write_text("{broken", encoding="utf-8")

            with self.assertRaises(worker.WorkerError) as raised:
                worker.handle_import_json({"path": str(invalid)}, context)
            self.assertEqual(raised.exception.code, "SOURCE_INVALID")
            self.assertEqual(worker.handle_list_conversations({"source": "demo"}, context), before)

            valid = self.write_json(folder, {
                "version": 1,
                "conversations": [{
                    "id": "offline",
                    "name": "离线快照",
                    "kind": "direct",
                    "messages": [{
                        "id": "m1",
                        "sender": "合成发送者",
                        "timestamp": "2026-11-28T09:00:00+08:00",
                        "type": "image",
                        "content": str(Path(folder) / "must-not-be-opened.png"),
                    }],
                }],
            })
            result = worker.handle_import_json({"path": str(valid)}, context)
            self.assertEqual(result["source"], "offline")
            valid.unlink()

            listed = worker.handle_list_conversations({"source": "offline"}, context)
            self.assertEqual(listed["items"][0]["id"], "offline")
            messages = worker.handle_get_messages({
                "source": "offline", "conversationId": "offline", "offset": 0, "limit": 50,
            }, context)
            self.assertEqual(messages["items"][0]["content"], "[图片]")
            self.assertNotIn("attachmentPath", messages["items"][0])
            exported = Path(folder) / "safe-export.json"
            worker.handle_export_json({
                "source": "offline", "conversationId": "offline",
                "outputPath": str(exported), "overwrite": False,
            }, context)
            exported_message = json.loads(exported.read_text(encoding="utf-8"))["conversations"][0]["messages"][0]
            self.assertEqual(exported_message["type"], "system")
            self.assertEqual(exported_message["content"], "[图片]")


if __name__ == "__main__":
    unittest.main()
