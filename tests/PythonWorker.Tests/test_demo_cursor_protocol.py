"""B2 demo coverage through the real JSON-lines worker, using synthetic JSON.

No account directory, real connection, media path, task or export is accessed.
The immutable demo version is shared by A/B2/B3 until source activation changes.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from test_message_cursor_protocol import ProtocolWorker


class DemoCursorProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="demo-cursor-protocol-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.demo = self.root / "demo.json"
        self.worker_count = 0
        self.messages = [
            ("earlier", "2026-11-23T23:59:59.999999+08:00"),
            ("late", "2026-11-24T12:00:00.000900+08:00"),
            ("tie-first", "2026-11-24T12:00:00.000250+08:00"),
            ("tie-second", "2026-11-24T12:00:00.000250+08:00"),
            ("day-end", "2026-11-24T23:59:59.999999+08:00"),
            ("later", "2026-11-25T00:00:00+08:00"),
        ]
        self.write_demo(self.messages)

    def write_demo(self, messages):
        self.demo.write_text(json.dumps({"version": 1, "conversations": [
            {"id": "c", "name": "合成演示会话", "kind": "direct", "messages": [
                {"id": message_id, "sender": "合成发送者", "timestamp": stamp,
                 "type": "text", "content": "needle " + message_id}
                for message_id, stamp in messages
            ]},
            {"id": "empty", "name": "空会话", "kind": "group", "messages": []},
        ]}, ensure_ascii=False), encoding="utf-8")

    def worker(self, mode="normal"):
        self.worker_count += 1
        root = self.root / f"worker-{self.worker_count}"
        root.mkdir()
        pipe = ProtocolWorker(root, self.demo, mode)
        self.addCleanup(pipe.close)
        sid = pipe.call("hello")["sourceSessionId"]
        return pipe, sid

    def first(self, pipe, sid, *, ok=True, **extra):
        return pipe.call("get_messages_page", {
            "source": "demo", "sourceSessionId": sid, "conversationId": "c",
            "order": "asc", "limit": 2, **extra,
        }, ok=ok)

    def more(self, pipe, sid, page, *, before=False, ok=True, **extra):
        return pipe.call("get_messages_page", {
            "source": "demo", "sourceSessionId": sid, "pageSessionId": page["pageSessionId"],
            "cursor": page["previousCursor" if before else "nextCursor"], "limit": 2, **extra,
        }, ok=ok)

    def ids(self, page):
        return [item["id"] for item in page["items"]]

    def test_precise_range_total_order_and_bidirectional_replay_with_different_limits(self):
        pipe, sid = self.worker()
        expected = ["tie-first", "tie-second", "late", "day-end"]
        for order in ("asc", "desc"):
            with self.subTest(order=order):
                sequence = expected if order == "asc" else expected[::-1]
                first = self.first(pipe, sid, order=order, limit=1, query=None,
                                   startDate="2026-11-24", endDate="2026-11-24")
                self.assertEqual((first["total"], first["sortVersion"]), (4, "precise-v1"))
                self.assertEqual(self.ids(first), sequence[:1])
                self.assertIsNone(first["previousCursor"])
                second = self.more(pipe, sid, first, limit=2)
                self.assertEqual(self.ids(second), sequence[1:3])
                self.assertEqual(second["pageSessionId"], first["pageSessionId"])
                self.assertEqual(second["total"], 4)
                self.assertEqual(self.ids(self.more(pipe, sid, first, limit=1)), sequence[1:2])
                back = self.more(pipe, sid, second, before=True, limit=3)
                self.assertEqual(self.ids(back), sequence[:1])
                self.assertIsNone(back["previousCursor"])
                final = self.more(pipe, sid, second, limit=3)
                self.assertEqual(self.ids(final), sequence[3:])
                self.assertIsNone(final["nextCursor"])
                # A before cursor binds the adjacent row, not an old page size.
                self.assertEqual(self.ids(self.more(pipe, sid, final, before=True, limit=1)), sequence[2:3])

    def test_empty_conversation_and_empty_range_have_exact_zero_without_tokens(self):
        pipe, sid = self.worker()
        for arguments in ({"conversationId": "empty"}, {"startDate": "2026-11-26"}):
            with self.subTest(arguments=arguments):
                page = self.first(pipe, sid, **arguments)
                self.assertEqual((page["total"], page["items"]), (0, []))
                self.assertIsNone(page["previousCursor"])
                self.assertIsNone(page["nextCursor"])

    def test_snapshot_is_shared_with_search_and_keyword_and_legacy_remains_available(self):
        pipe, sid = self.worker()
        first = self.first(pipe, sid)
        self.assertEqual(first["total"], 6)
        original_ids = ["earlier", "tie-first", "tie-second", "late", "day-end", "later"]
        replacement = [("replacement", "2026-11-24T12:00:00+08:00")]
        self.write_demo(replacement)
        # The old offset API retains its original live-file behaviour.
        legacy = pipe.call("get_messages", {"source": "demo", "conversationId": "c"})
        self.assertEqual((legacy["total"], self.ids(legacy)), (1, ["replacement"]))
        self.demo.unlink()
        # B2's first and later pages, A and B3 read the same immutable version,
        # even after the selected demo file is edited or removed.
        complete = self.first(pipe, sid, limit=100)
        self.assertEqual(self.ids(complete), original_ids)
        second = self.more(pipe, sid, first)
        self.assertEqual(self.ids(second), original_ids[2:4])
        self.assertEqual(second["total"], 6)
        keyword = self.first(pipe, sid, query="needle", limit=100)
        self.assertEqual((keyword["mode"], keyword["total"]), ("keyword", 6))
        self.assertEqual(self.ids(keyword), original_ids)
        search = pipe.call("search_messages", {
            "source": "demo", "sourceSessionId": sid, "query": "needle", "order": "asc",
        })
        self.assertEqual(search["total"], 6)
        self.assertEqual([item["messageId"] for item in search["items"]], original_ids)

    def test_failed_import_keeps_cursor_but_switching_source_expires_old_version(self):
        pipe, sid = self.worker()
        first = self.first(pipe, sid)
        bad = self.root / "bad.json"
        bad.write_text("{invalid", encoding="utf-8")
        self.assertEqual(pipe.call("import_json", {"path": str(bad)}, ok=False)["code"], "SOURCE_INVALID")
        self.assertEqual(self.more(pipe, sid, first)["total"], 6)
        imported = pipe.call("import_json", {"path": str(self.demo)})
        self.assertNotEqual(imported["sourceSessionId"], sid)
        self.assertEqual(self.more(pipe, sid, first, ok=False)["code"], "SOURCE_SESSION_CHANGED")
        self.write_demo([("new-version", "2026-11-24T12:00:00+08:00")])
        new_sid = pipe.call("list_conversations", {"source": "demo"})["sourceSessionId"]
        self.assertNotEqual(new_sid, sid)
        expired = self.more(pipe, new_sid, first, ok=False)
        self.assertEqual(expired["code"], "PAGE_EXPIRED")
        self.assertTrue(expired["retryable"])
        fresh = self.first(pipe, new_sid)
        self.assertEqual((fresh["total"], self.ids(fresh)), (1, ["new-version"]))
        disconnected_sid = pipe.call("disconnect")["sourceSessionId"]
        self.assertNotEqual(disconnected_sid, new_sid)
        self.assertEqual(self.first(pipe, new_sid, ok=False)["code"], "SOURCE_SESSION_CHANGED")

    def test_ttl_expiry_reports_page_expired_and_allows_a_fresh_first_page(self):
        pipe, sid = self.worker("demo-expire")
        first = self.first(pipe, sid)
        expired = self.more(pipe, sid, first, ok=False)
        self.assertEqual(expired["code"], "PAGE_EXPIRED")
        self.assertTrue(expired["retryable"])
        fresh = self.first(pipe, sid)
        self.assertNotEqual(fresh["pageSessionId"], first["pageSessionId"])
        self.assertEqual(self.ids(fresh), self.ids(first))

    def test_unknown_cross_session_tokens_and_new_continuation_conditions_are_rejected(self):
        pipe, sid = self.worker()
        first, other = self.first(pipe, sid), self.first(pipe, sid)
        for cursor in ("unknown", other["nextCursor"]):
            with self.subTest(cursor=cursor):
                self.assertEqual(self.more(pipe, sid, first, cursor=cursor, ok=False)["code"], "PAGE_EXPIRED")
        for key, value in (("conversationId", "empty"), ("order", "desc"), ("startDate", None), ("query", None)):
            with self.subTest(key=key):
                error = self.more(pipe, sid, first, ok=False, **{key: value})
                self.assertEqual(error["code"], "INVALID_ARGUMENT")
        self.assertEqual(self.first(pipe, sid, conversationId="unknown", ok=False)["code"],
                         "CONVERSATION_NOT_FOUND")
        self.assertEqual(self.more(pipe, sid, first)["total"], 6)


if __name__ == "__main__":
    unittest.main()
