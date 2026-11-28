"""JSON-lines coverage for live preview cursor pagination.

The live source is backed by temporary synthetic SQLite shards; no account,
key capture, production database, export, or task path is used.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from synthetic_messages import ROOT, table_name


_BOOTSTRAP = r'''
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
sys.path.insert(0, sys.argv[2])
import worker
from synthetic_messages import SyntheticSourceFactory
import wechat_memory.wechat4_source as live

root = Path(sys.argv[3])
mode = sys.argv[5]
factory = SyntheticSourceFactory(root)
rows = [
    {"create_time": 1795478400 + index, "content": "cursor row " + str(index), "server_id": 7}
    for index in range(61)
]
factory.add_shard("protocol_live.db", {"live": rows}, without_rowid=(mode == "unsupported"))
source = factory.source()
source.connect = lambda *args, **kwargs: {
    "accountLabel": "合成账户", "conversationCount": 1, "messageTableCount": 1,
    "imageKeyAvailable": False, "voiceAvailable": False,
}
# Cursor preview must not route through the legacy/export paging API.
def forbidden_legacy_read(*args, **kwargs):
    raise AssertionError("cursor protocol called get_messages")
source.get_messages = forbidden_legacy_read

original_first = source.get_messages_page
if mode == "over-limit":
    def oversized_page(conversation_id, **kwargs):
        kwargs["limit"] += 1
        return original_first(conversation_id, **kwargs)
    source.get_messages_page = oversized_page
elif mode == "malformed":
    from types import SimpleNamespace
    def malformed_page(*args, **kwargs):
        return SimpleNamespace(items=(object(),), total=1, page_session_id="synthetic-session",
                               previous_cursor=None, next_cursor=None, sort_version="precise-v1")
    source.get_messages_page = malformed_page

original_continue = source.get_messages_cursor_page
if mode == "expire":
    def expired_page(*args, **kwargs):
        live.CURSOR_SESSION_TTL_SECONDS = 0
        try:
            return original_continue(*args, **kwargs)
        finally:
            live.CURSOR_SESSION_TTL_SECONDS = 600
    source.get_messages_cursor_page = expired_page

base_source_type = live.WeChat4Source
class SyntheticWeChat4Source(base_source_type):
    def __new__(cls):
        return source
live.WeChat4Source = SyntheticWeChat4Source
if mode == "demo-expire":
    from wechat_memory.memory_paging import ImmutableMessagePageProvider
    from wechat_memory.page_sessions import PageSessionLimits
    original_memory_first = ImmutableMessagePageProvider.get_messages_page
    def expiring_memory_first(self, *args, **kwargs):
        page = original_memory_first(self, *args, **kwargs)
        self._page_sessions.limits = PageSessionLimits(ttl_seconds=0)
        return page
    ImmutableMessagePageProvider.get_messages_page = expiring_memory_first
raise SystemExit(worker.serve(sys.stdin.buffer, sys.stdout.buffer, sys.argv[4]))
'''


class ProtocolWorker:
    def __init__(self, root: Path, demo_path: Path, mode: str = "normal") -> None:
        self.root = root
        self.process = subprocess.Popen(
            [sys.executable, "-I", "-X", "utf8", "-u", "-c", _BOOTSTRAP,
             str(ROOT / "src/PythonWorker"), str(ROOT / "tests/PythonWorker.Tests"),
             str(root), str(demo_path), mode],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=str(ROOT),
        )
        self.watchdog = threading.Timer(45, self.process.kill)
        self.watchdog.start()
        self.counter = 0

    def call(self, command: str, payload: dict | None = None, *, ok: bool = True) -> dict:
        self.counter += 1
        request_id = str(self.counter)
        frame = {
            "protocolVersion": 1, "requestId": request_id, "type": "request",
            "command": command, "payload": payload or {},
        }
        assert self.process.stdin is not None and self.process.stdout is not None
        self.process.stdin.write((json.dumps(frame, ensure_ascii=False) + "\n").encode("utf-8"))
        self.process.stdin.flush()
        while True:
            line = self.process.stdout.readline()
            if not line:
                self.process.wait(timeout=5)
                stderr = self.process.stderr.read().decode("utf-8", errors="replace") if self.process.stderr else ""
                raise AssertionError(f"worker exited without a response: {stderr}")
            if len(line) > 1024 * 1024:
                raise AssertionError("worker response exceeded the protocol line limit")
            response = json.loads(line)
            if response["type"] == "progress":
                continue
            if response["requestId"] != request_id:
                raise AssertionError(f"unexpected response id: {response}")
            if response["ok"] != ok:
                raise AssertionError(f"unexpected success state: {response}")
            return response["result"] if ok else response["error"]

    def close(self) -> None:
        try:
            if self.process.poll() is None:
                self.call("shutdown")
                self.process.wait(timeout=15)
            if self.process.returncode != 0:
                stderr = self.process.stderr.read().decode("utf-8", errors="replace") if self.process.stderr else ""
                raise AssertionError(f"worker exited with {self.process.returncode}: {stderr}")
            assert self.process.stderr is not None
            stderr = self.process.stderr.read().decode("utf-8", errors="replace")
            if stderr:
                raise AssertionError(f"unexpected worker diagnostics: {stderr}")
        finally:
            self.watchdog.cancel()
            if self.process.poll() is None:
                self.process.kill()
                self.process.wait(timeout=10)
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass


class MessageCursorProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="cursor-protocol-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.offline_json = self.root / "offline.json"
        self.worker_count = 0
        self.offline_json.write_text(json.dumps({"version": 1, "conversations": [{
            "id": "offline", "name": "离线合成会话", "kind": "direct", "messages": [
                {"id": f"offline-message-{index}", "sender": "合成发送者", "type": "text",
                 "timestamp": f"2026-11-24T12:00:0{index}+08:00", "content": f"synthetic offline {index}"}
                for index in range(3)
            ],
        }]}), encoding="utf-8")

    def worker(self, mode: str = "normal") -> ProtocolWorker:
        self.worker_count += 1
        root = self.root / f"worker-{self.worker_count}"
        root.mkdir()
        return ProtocolWorker(root, ROOT / "demo/conversations.json", mode)

    def connect_live(self, pipe: ProtocolWorker) -> str:
        connected = pipe.call("connect", {"source": "wechat4"})
        self.assertTrue(connected["connected"])
        return connected["sourceSessionId"]

    def first_page(self, pipe: ProtocolWorker, source_session_id: str, *, ok: bool = True, **extra) -> dict:
        return pipe.call("get_messages_page", {
            "source": "wechat4", "sourceSessionId": source_session_id,
            "conversationId": "live", **extra,
        }, ok=ok)

    def test_capabilities_source_scope_first_page_and_continuation(self):
        pipe = self.worker()
        try:
            hello = pipe.call("hello")
            self.assertIn("message_cursor_pagination", hello["capabilities"])
            self.assertEqual(hello["sourceCapabilities"]["wechat4"],
                             ["message_cursor_pagination", "message_keyword_pagination"])
            self.assertEqual(hello["sourceCapabilities"]["demo"],
                             ["message_cursor_pagination", "message_keyword_pagination"])
            self.assertEqual(hello["sourceCapabilities"]["offline"],
                             ["message_cursor_pagination", "message_keyword_pagination"])

            demo_sid = hello["sourceSessionId"]
            demo_conversations = pipe.call("list_conversations", {"source": "demo"})["items"]
            demo_first = pipe.call("get_messages_page", {
                "source": "demo", "sourceSessionId": demo_sid,
                "conversationId": demo_conversations[0]["id"], "limit": 1,
            })
            self.assertEqual(demo_first["total"], demo_conversations[0]["messageCount"])
            self.assertEqual(len(demo_first["items"]), 1)
            self.assertTrue(demo_first["nextCursor"])
            imported = pipe.call("import_json", {"path": str(self.offline_json)})
            offline_sid = imported["sourceSessionId"]
            offline_first = pipe.call("get_messages_page", {
                "source": "offline", "sourceSessionId": offline_sid,
                "conversationId": "offline", "order": "asc", "limit": 1,
            })
            self.assertEqual(offline_first["total"], 3)
            self.assertEqual(offline_first["items"][0]["id"], "offline-message-0")
            self.assertTrue(offline_first["nextCursor"])
            offline_next = pipe.call("get_messages_page", {
                "source": "offline", "sourceSessionId": offline_sid,
                "pageSessionId": offline_first["pageSessionId"],
                "cursor": offline_first["nextCursor"], "limit": 1,
            })
            self.assertEqual(offline_next["items"][0]["id"], "offline-message-1")
            self.assertEqual(offline_next["pageSessionId"], offline_first["pageSessionId"])

            live_sid = self.connect_live(pipe)
            first = self.first_page(pipe, live_sid, order="desc", limit=10, query=None)
            self.assertEqual(first["total"], 61)
            self.assertEqual(len(first["items"]), 10)
            self.assertEqual(first["sortVersion"], "precise-v1")
            self.assertIsNone(first["previousCursor"])
            self.assertTrue(first["nextCursor"])
            self.assertTrue(first["pageSessionId"])
            second = pipe.call("get_messages_page", {
                "source": "wechat4", "sourceSessionId": live_sid,
                "pageSessionId": first["pageSessionId"], "cursor": first["nextCursor"], "limit": 10,
            })
            self.assertEqual(second["pageSessionId"], first["pageSessionId"])
            self.assertEqual(second["total"], 61)
            self.assertEqual(len(second["items"]), 10)
            self.assertNotEqual(first["items"][0]["content"], second["items"][0]["content"])
            self.assertTrue(second["previousCursor"])

            invalid = pipe.call("get_messages_page", {
                "source": "wechat4", "sourceSessionId": live_sid,
                "pageSessionId": first["pageSessionId"], "cursor": first["nextCursor"],
                "startDate": "2026-11-24",
            }, ok=False)
            self.assertEqual(invalid["code"], "INVALID_ARGUMENT")
            invalid = self.first_page(pipe, live_sid, ok=False, query=42)
            self.assertEqual(invalid["code"], "INVALID_ARGUMENT")
            invalid = pipe.call("get_messages_page", {
                "source": "wechat4", "conversationId": "live",
            }, ok=False)
            self.assertEqual(invalid["code"], "INVALID_ARGUMENT")
            invalid = self.first_page(pipe, live_sid, ok=False, limit=101)
            self.assertEqual(invalid["code"], "INVALID_ARGUMENT")
        finally:
            pipe.close()

    def test_offline_reimport_replaces_snapshot_and_invalidates_existing_cursor(self):
        pipe = self.worker()
        try:
            imported = pipe.call("import_json", {"path": str(self.offline_json)})
            old_sid = imported["sourceSessionId"]
            first = pipe.call("get_messages_page", {
                "source": "offline", "sourceSessionId": old_sid,
                "conversationId": "offline", "order": "asc", "limit": 1,
            })
            self.assertEqual(first["total"], 3)

            replacement = {"version": 1, "conversations": [{
                "id": "offline", "name": "替换后的合成会话", "kind": "direct", "messages": [
                    {"id": f"replacement-{index}", "sender": "合成发送者", "type": "text",
                     "timestamp": f"2026-11-25T12:00:0{index}+08:00", "content": f"replacement {index}"}
                    for index in range(2)
                ],
            }]}
            self.offline_json.write_text(json.dumps(replacement), encoding="utf-8")
            reimported = pipe.call("import_json", {"path": str(self.offline_json)})
            new_sid = reimported["sourceSessionId"]
            self.assertNotEqual(new_sid, old_sid)

            stale = pipe.call("get_messages_page", {
                "source": "offline", "sourceSessionId": old_sid,
                "pageSessionId": first["pageSessionId"], "cursor": first["nextCursor"],
            }, ok=False)
            self.assertEqual(stale["code"], "SOURCE_SESSION_CHANGED")
            expired = pipe.call("get_messages_page", {
                "source": "offline", "sourceSessionId": new_sid,
                "pageSessionId": first["pageSessionId"], "cursor": first["nextCursor"],
            }, ok=False)
            self.assertEqual(expired["code"], "PAGE_EXPIRED")
            self.assertTrue(expired["retryable"])

            refreshed = pipe.call("get_messages_page", {
                "source": "offline", "sourceSessionId": new_sid,
                "conversationId": "offline", "order": "asc", "limit": 1,
            })
            self.assertEqual(refreshed["total"], 2)
            self.assertEqual(refreshed["items"][0]["id"], "replacement-0")
            continued = pipe.call("get_messages_page", {
                "source": "offline", "sourceSessionId": new_sid,
                "pageSessionId": refreshed["pageSessionId"], "cursor": refreshed["nextCursor"],
                "limit": 1,
            })
            self.assertEqual(continued["items"][0]["id"], "replacement-1")
        finally:
            pipe.close()

    def test_stale_source_session_and_database_generation_errors_are_stable(self):
        pipe = self.worker()
        try:
            live_sid = self.connect_live(pipe)
            first = self.first_page(pipe, live_sid, limit=5)
            stale = pipe.call("get_messages_page", {
                "source": "wechat4", "sourceSessionId": "stale-session",
                "pageSessionId": first["pageSessionId"], "cursor": first["nextCursor"],
            }, ok=False)
            self.assertEqual(stale["code"], "SOURCE_SESSION_CHANGED")
            database = pipe.root / "protocol_live.db"
            table = table_name("live")
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    f'INSERT INTO "{table}" VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                    (999, 1795481000, 1, 2, "late synthetic row", 0, 0, None),
                )
                connection.commit()
            finally:
                connection.close()
            changed = pipe.call("get_messages_page", {
                "source": "wechat4", "sourceSessionId": live_sid,
                "pageSessionId": first["pageSessionId"], "cursor": first["nextCursor"],
            }, ok=False)
            self.assertEqual(changed["code"], "PAGE_SOURCE_CHANGED")
            self.assertTrue(changed["retryable"])
            expired = pipe.call("get_messages_page", {
                "source": "wechat4", "sourceSessionId": live_sid,
                "pageSessionId": first["pageSessionId"], "cursor": first["nextCursor"],
            }, ok=False)
            self.assertEqual(expired["code"], "PAGE_EXPIRED")
            self.assertTrue(expired["retryable"])
        finally:
            pipe.close()

    def test_expired_cursor_and_unsupported_schema_are_reported(self):
        pipe = self.worker("expire")
        try:
            live_sid = self.connect_live(pipe)
            first = self.first_page(pipe, live_sid, limit=5)
            expired = pipe.call("get_messages_page", {
                "source": "wechat4", "sourceSessionId": live_sid,
                "pageSessionId": first["pageSessionId"], "cursor": first["nextCursor"],
            }, ok=False)
            self.assertEqual(expired["code"], "PAGE_EXPIRED")
            self.assertTrue(expired["retryable"])
        finally:
            pipe.close()

        pipe = self.worker("unsupported")
        try:
            live_sid = self.connect_live(pipe)
            unsupported = self.first_page(pipe, live_sid, limit=5, ok=False)
            self.assertEqual(unsupported["code"], "PAGE_UNSUPPORTED_SCHEMA")
        finally:
            pipe.close()

    def test_invalid_source_page_responses_are_rejected(self):
        for mode, limit in (("malformed", 5), ("over-limit", 1)):
            with self.subTest(mode=mode):
                pipe = self.worker(mode)
                try:
                    live_sid = self.connect_live(pipe)
                    error = self.first_page(pipe, live_sid, limit=limit, ok=False)
                    self.assertEqual(error["code"], "PAGE_RESPONSE_INVALID")
                finally:
                    pipe.close()


if __name__ == "__main__":
    unittest.main()
