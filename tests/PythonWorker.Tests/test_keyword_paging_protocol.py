"""Actual JSON-lines B3 requests, errors, and source capability boundaries.

First request (query is a literal string, including empty/whitespace):
 {source, sourceSessionId, conversationId, query, startDate?, endDate?, order?, limit?}
B2 first request: the same shape with query:null (or no query).
Continuation for BOTH modes:
 {source, sourceSessionId, pageSessionId, cursor, limit?}
B3 response:
 {pageSessionId, sourceSessionId, items, total, candidateCount, previousCursor,
  nextCursor, sortVersion:"precise-v1", projectionVersion, mode:"keyword", order}
No mode/media/version/revision/offset fields are accepted from clients; those
criteria are server-owned. A changed first-request criterion requires a new
session, not a continuation. Entire messages are retained; byte-limited pages
advance cursors by their actual contents. Total is never A's 1,000-hit Top-K.
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
from test_message_cursor_protocol import ProtocolWorker


_BOOTSTRAP = r'''
import sys
from pathlib import Path
from collections import Counter
from dataclasses import replace
sys.path.insert(0, sys.argv[1])
sys.path.insert(0, sys.argv[2])
import worker
import zstandard
from synthetic_messages import SyntheticSourceFactory
from wechat_memory import wechat4_source as live, global_search as search
root, mode = Path(sys.argv[3]), sys.argv[5]
factory = SyntheticSourceFactory(root)
rows = [{"create_time": 1795478400 + i, "content": "needle row " + str(i), "server_id": 7} for i in range(61)]
rows[0]["content"] = zstandard.ZstdCompressor().compress(b"needle compressed")
rows[0]["compression"] = 4
rows[1].update(local_type=49, content='<msg><appmsg><type>5</type><title>needle card</title></appmsg></msg>')
rows[58].update(local_type=3, content="a" * 32)
rows[59].update(local_type=34, content='<msg><voicemsg voicelength="1000"/></msg>')
rows[60]["content"] = "no matching text"
if mode == "response-limit":
    rows = [{"create_time": 1795478400, "content": "needle" + "x" * (1024 * 1024)}]
factory.add_shard("keyword_protocol.db", {"live": rows, "other": [{"create_time": 1795478400, "content": "needle other chat"}]},
                  without_rowid=(mode == "unsupported"))
source = factory.source()
counts = Counter()
project, decode, parse = source._row_to_message, live._decode_content, live.parse_appmsg
def counted_project(*args, **kwargs):
    counts["project"] += 1
    return project(*args, **kwargs)
def counted_decode(*args, **kwargs):
    counts["decode"] += 1
    return decode(*args, **kwargs)
def counted_parse(*args, **kwargs):
    counts["parse"] += 1
    return parse(*args, **kwargs)
source._row_to_message, live._decode_content, live.parse_appmsg = counted_project, counted_decode, counted_parse
class Images:
    def resolve(self, *args):
        counts["image"] += 1
        return root / "needle-image.png"
    def close(self):
        pass
class Voices:
    def resolve(self, *args):
        counts["voice"] += 1
        return root / "voice.silk"
class Transcripts:
    def load(self, *args):
        counts["transcript"] += 1
        return "needle existing transcript"
source._images, source._voices, source._transcripts = Images(), Voices(), Transcripts()
source.image_key_available = source.voice_available = True
source.connect = lambda *args, **kwargs: {"accountLabel": "合成账户", "conversationCount": 2,
    "messageTableCount": 2, "imageKeyAvailable": True, "voiceAvailable": True}
def forbidden_legacy(*args, **kwargs):
    raise AssertionError("keyword preview fell back to repeated legacy get_messages")
source.get_messages = forbidden_legacy
base = live.WeChat4Source
class SyntheticWeChat4Source(base):
    def __new__(cls):
        return source
live.WeChat4Source = SyntheticWeChat4Source
if mode == "cache-limit":
    search.QUERY_CACHE_BYTES = 6000
elif mode == "candidate-limit":
    worker.MAX_EXPORT_MESSAGES = 60
elif mode == "expiry":
    original = search.KeywordPageCache.cursor_page
    def expiring(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        session_id = result["pageSessionId"]
        self.sessions[session_id] = replace(self.sessions[session_id], expires_at=0)
        return result
    search.KeywordPageCache.cursor_page = expiring
worker.HANDLERS["fixture_counts"] = lambda payload, context: dict(counts)
worker.HANDLERS["fixture_revision"] = lambda payload, context: (
    setattr(source, "_media_revision", source._media_revision + 1) or {"changed": True})
raise SystemExit(worker.serve(sys.stdin.buffer, sys.stdout.buffer, sys.argv[4]))
'''


class KeywordProtocolWorker(ProtocolWorker):
    def __init__(self, root: Path, demo_path: Path, mode: str = "normal"):
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


class KeywordPagingProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="keyword-protocol-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.count = 0

    def worker(self, mode="normal"):
        self.count += 1
        root = self.root / f"worker-{self.count}"
        root.mkdir()
        pipe = KeywordProtocolWorker(root, ROOT / "demo/conversations.json", mode)
        self.addCleanup(pipe.close)
        return pipe

    def first_live(self, pipe, *, ok=True, **extra):
        connected = pipe.call("connect", {"source": "wechat4"})
        sid = connected["sourceSessionId"]
        page = pipe.call("get_messages_page", {
            "source": "wechat4", "sourceSessionId": sid, "conversationId": "live",
            "query": "needle", "order": "asc", "limit": 10, **extra,
        }, ok=ok)
        return sid, page

    def more(self, pipe, sid, page, *, previous=False, ok=True, **extra):
        return pipe.call("get_messages_page", {
            "source": "wechat4", "sourceSessionId": sid, "pageSessionId": page["pageSessionId"],
            "cursor": page["previousCursor" if previous else "nextCursor"], "limit": 10, **extra,
        }, ok=ok)

    def test_live_first_forward_backward_keep_complete_count_and_never_repeat_projection(self):
        pipe = self.worker()
        hello = pipe.call("hello")
        self.assertIn("message_keyword_pagination", hello["capabilities"])
        self.assertEqual(hello["sourceCapabilities"]["wechat4"],
                         ["message_cursor_pagination", "message_keyword_pagination"])
        sid, first = self.first_live(pipe)
        self.assertEqual((first["total"], first["candidateCount"]), (60, 61))
        self.assertEqual(first["mode"], "keyword")
        self.assertEqual(first["sourceSessionId"], sid)
        self.assertEqual(first["sortVersion"], "precise-v1")
        self.assertTrue(first["projectionVersion"].startswith("canonical-"))
        self.assertIsNone(first["previousCursor"])
        initial_counts = pipe.call("fixture_counts")
        self.assertEqual(initial_counts, {"project": 61, "decode": 61, "parse": 1, "image": 1, "voice": 1, "transcript": 1})
        second = self.more(pipe, sid, first)
        self.assertEqual(second["pageSessionId"], first["pageSessionId"])
        self.assertEqual(self.more(pipe, sid, second, previous=True)["items"], first["items"])
        items = list(first["items"])
        page = first
        while page["nextCursor"]:
            page = self.more(pipe, sid, page)
            items.extend(page["items"])
        self.assertEqual(len(items), 60)
        self.assertEqual(items[-2]["content"], "[图片]")
        self.assertIn("attachmentPath", items[-2])
        self.assertEqual(items[-1]["content"], "needle existing transcript")
        self.assertEqual(items[-1]["transcript"], "needle existing transcript")
        self.assertEqual(pipe.call("fixture_counts"), initial_counts)
        # No opaque row/table/locator identities are exposed by the protocol.
        self.assertNotIn(table_name("live"), json.dumps(items))
        ordinary = pipe.call("get_messages_page", {
            "source": "wechat4", "sourceSessionId": sid, "conversationId": "live", "query": None, "limit": 3,
        })
        self.assertEqual(ordinary["total"], 61)
        self.assertNotIn("mode", ordinary)
        self.assertNotIn("candidateCount", ordinary)

    def test_keyword_conditions_rejected_on_continuation_and_media_revision_changes(self):
        pipe = self.worker()
        sid, first = self.first_live(pipe)
        for extra in ({"query": "other"}, {"query": None}, {"order": "desc"}, {"conversationId": "other"},
                      {"startDate": "2026-11-24"}, {"endDate": "2026-11-24"}):
            error = self.more(pipe, sid, first, ok=False, **extra)
            self.assertEqual(error["code"], "PAGE_CRITERIA_CHANGED")
        for extra in ({"offset": 10}, {"mode": "keyword"}, {"enableVoice": False}, {"projectionVersion": "fake"}):
            self.assertEqual(self.more(pipe, sid, first, ok=False, **extra)["code"], "INVALID_ARGUMENT")
        self.assertTrue(self.more(pipe, sid, first)["items"])
        pipe.call("fixture_revision")
        self.assertEqual(self.more(pipe, sid, first, ok=False)["code"], "PAGE_CRITERIA_CHANGED")
        self.assertEqual(self.more(pipe, sid, first, ok=False)["code"], "PAGE_EXPIRED")

    def test_database_change_and_source_disconnect_are_not_mixed_into_cached_results(self):
        pipe = self.worker()
        sid, first = self.first_live(pipe)
        connection = sqlite3.connect(pipe.root / "keyword_protocol.db")
        try:
            connection.execute(f'UPDATE "{table_name("live")}" SET message_content=? WHERE rowid=3', ("changed",))
            connection.commit()
        finally:
            connection.close()
        error = self.more(pipe, sid, first, ok=False)
        self.assertEqual(error["code"], "PAGE_SOURCE_CHANGED")
        self.assertTrue(error["retryable"])
        self.assertEqual(self.more(pipe, sid, first, ok=False)["code"], "PAGE_EXPIRED")
        pipe.call("disconnect")
        self.assertEqual(self.more(pipe, sid, first, ok=False)["code"], "SOURCE_SESSION_CHANGED")

    def test_budget_expiry_candidate_response_and_schema_errors_are_explicit(self):
        for mode, expected in (("cache-limit", "SEARCH_CACHE_LIMIT"),
                               ("candidate-limit", "RESULT_LIMIT_EXCEEDED"),
                               ("response-limit", "RESPONSE_LIMIT_EXCEEDED"),
                               ("unsupported", "PAGE_UNSUPPORTED_SCHEMA")):
            with self.subTest(mode=mode):
                pipe = self.worker(mode)
                _sid, error = self.first_live(pipe, ok=False)
                self.assertEqual(error["code"], expected)
                self.assertIn("message_keyword_pagination", pipe.call("hello")["capabilities"])
        pipe = self.worker("expiry")
        sid, first = self.first_live(pipe)
        error = self.more(pipe, sid, first, ok=False)
        self.assertEqual(error["code"], "PAGE_EXPIRED")
        self.assertTrue(error["retryable"])

    def test_demo_offline_support_is_literal_memory_projection_without_media_recovery(self):
        pipe = self.worker()
        hello = pipe.call("hello")
        self.assertEqual(hello["sourceCapabilities"]["demo"],
                         ["message_cursor_pagination", "message_keyword_pagination"])
        conversations = pipe.call("list_conversations", {"source": "demo"})
        demo_sid = conversations["sourceSessionId"]
        conversation_id = conversations["items"][0]["id"]
        demo = pipe.call("get_messages_page", {
            "source": "demo", "sourceSessionId": demo_sid, "conversationId": conversation_id,
            "query": "", "order": "asc", "limit": 1,
        })
        self.assertEqual(demo["mode"], "keyword")
        self.assertEqual(demo["candidateCount"], demo["total"])
        projection_only = {"displayKind", "quoteSummary", "quoteSender", "forwardItems",
                           "parseStatus", "truncated", "omittedCount"}
        self.assertFalse(projection_only.intersection(demo["items"][0]))
        pipe.call("get_messages_page", {
            "source": "demo", "sourceSessionId": demo_sid, "pageSessionId": demo["pageSessionId"],
            "cursor": demo["nextCursor"], "limit": 1,
        })
        cursor = pipe.call("get_messages_page", {
            "source": "demo", "sourceSessionId": demo_sid, "conversationId": conversation_id, "query": None,
        })
        self.assertEqual(cursor["total"], demo["total"])
        self.assertNotIn("mode", cursor)
        self.assertEqual(cursor["sortVersion"], "precise-v1")
        offline_path = self.root / "offline.json"
        offline_path.write_text(json.dumps({"version": 1, "conversations": [{
            "id": "offline", "name": "离线会话", "kind": "direct", "messages": [
                {"id": "image", "sender": "sender", "type": "image", "content": "C:/never-open/needle.png",
                 "timestamp": "2026-11-24T10:00:00+08:00"},
                {"id": "voice", "sender": "sender", "type": "voice", "content": "voice placeholder",
                 "transcript": "hidden needle", "timestamp": "2026-11-24T10:00:01+08:00"},
            ],
        }]}), encoding="utf-8")
        imported = pipe.call("import_json", {"path": str(offline_path)})
        sid = imported["sourceSessionId"]
        offline_path.unlink()
        image = pipe.call("get_messages_page", {
            "source": "offline", "sourceSessionId": sid, "conversationId": "offline", "query": "needle",
        })
        self.assertEqual((image["total"], image["candidateCount"]), (1, 2))
        self.assertEqual(image["items"][0]["content"], "[图片]")
        self.assertNotIn("attachmentPath", image["items"][0])
        self.assertFalse(projection_only.intersection(image["items"][0]))
        hidden = pipe.call("get_messages_page", {
            "source": "offline", "sourceSessionId": sid, "conversationId": "offline", "query": "hidden needle",
        })
        self.assertEqual((hidden["total"], hidden["candidateCount"]), (0, 2))
        self.assertEqual(pipe.call("fixture_counts"), {})
        # The old demo token cannot become an offline continuation.
        self.assertEqual(pipe.call("get_messages_page", {
            "source": "demo", "sourceSessionId": demo_sid, "pageSessionId": demo["pageSessionId"],
            "cursor": demo["nextCursor"],
        }, ok=False)["code"], "SOURCE_SESSION_CHANGED")


if __name__ == "__main__":
    unittest.main()
