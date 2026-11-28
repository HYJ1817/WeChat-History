"""B3 source/worker integration, using only temporary synthetic data.

Protocol: get_messages_page({source, sourceSessionId, conversationId, query:<str>,
startDate?:YYYY-MM-DD, endDate?:YYYY-MM-DD, order?:asc|desc, limit?:1..100}).
query:null (or absent) is unchanged B2; even "" / whitespace select B3 and keep
legacy literal matching. Continuation ONLY accepts {source, sourceSessionId,
pageSessionId, cursor, limit?}; no query/date/conversation/order/media override.
B3 returns the B2 cursor fields plus mode:"keyword", sourceSessionId, order,
projectionVersion, and exact candidateCount (before matching), not a Top-K.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from collections import Counter
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from synthetic_messages import ROOT, SyntheticSourceFactory, table_name
from wechat_memory import global_search as search, wechat4_source as live
from wechat_memory.message_reader import SQLiteShardReader
import worker
import zstandard

DAY = int(datetime(2026, 11, 24, tzinfo=worker.DEMO_TIMEZONE).timestamp())


class KeywordPagingIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="keyword-integration-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.factory = SyntheticSourceFactory(self.root)
        self.budget = search._SearchMemoryBudget()
        ledger = patch.object(search, "_QUERY_MEMORY", self.budget)
        ledger.start()
        self.addCleanup(ledger.stop)
        self.context = worker.Context(str(ROOT / "demo/conversations.json"))
        self.addCleanup(self.context.search_cache.clear)
        self.addCleanup(worker._clear_keyword_pages, self.context)

    def activate_live(self):
        source = self.factory.source()
        self.addCleanup(source.close)
        self.context.wechat = source
        worker._activate_source(self.context, "wechat4", force=True)
        return source

    def first(self, query="needle", conversation="c", **extra):
        return worker.handle_get_messages_page({
            "source": self.context.active_source, "sourceSessionId": self.context.source_session_id,
            "conversationId": conversation, "query": query, "order": "asc", "limit": 2, **extra,
        }, self.context)

    def more(self, page, *, previous=False, **extra):
        return worker.handle_get_messages_page({
            "source": self.context.active_source, "sourceSessionId": self.context.source_session_id,
            "pageSessionId": page["pageSessionId"],
            "cursor": page["previousCursor" if previous else "nextCursor"], "limit": 2, **extra,
        }, self.context)

    def collect(self, page, **kwargs):
        items = list(page["items"])
        while page["nextCursor"] is not None:
            page = self.more(page, **kwargs)
            items.extend(page["items"])
        return items

    def assert_error(self, code, call, *args, **kwargs):
        with self.assertRaises(Exception) as raised:
            call(*args, **kwargs)
        self.assertEqual(getattr(raised.exception, "code", None), code, str(raised.exception))
        return raised.exception

    def test_complete_media_projection_once_then_no_reader_parser_decode_or_media_work(self):
        calls = Counter()
        card = '<msg><appmsg><title>needle card</title><type>5</type><url>https://invalid.example/</url></appmsg></msg>'
        compressed = zstandard.ZstdCompressor().compress(card.encode())
        rows = [
            {"create_time": DAY + i, "content": f"needle text {i}"} for i in range(4)
        ] + [
            {"create_time": DAY + 4, "content": compressed, "local_type": 49, "compression": 4},
            {"create_time": DAY + 5, "content": '<msg><voicemsg voicelength="1000"/></msg>', "local_type": 34},
            {"create_time": DAY + 6, "content": "a" * 32, "local_type": 3},
            {"create_time": DAY + 7, "content": "unmatched candidate"},
            {"create_time": DAY - 1, "content": "needle outside date", "local_type": 3},
        ]
        self.factory.add_shard("one.db", {"c": rows, "other": [{"create_time": DAY, "content": "needle other chat"}]})
        source = self.activate_live()
        images, voices, transcripts = Mock(), Mock(), Mock()
        images.resolve.return_value = self.root / "needle-image.png"
        voices.resolve.return_value = self.root / "voice.silk"
        transcripts.load.return_value = "needle rendered transcript"
        source._images, source._voices, source._transcripts = images, voices, transcripts
        source.voice_available = source.image_key_available = True
        with patch.object(source, "_row_to_message", wraps=source._row_to_message) as project, \
                patch.object(live, "_decode_content", wraps=live._decode_content) as decode, \
                patch.object(live, "parse_appmsg", wraps=live.parse_appmsg) as parse:
            page = self.first(startDate="2026-11-24", endDate="2026-11-24")
            calls.update(project=project.call_count, decode=decode.call_count, parse=parse.call_count,
                         image=images.resolve.call_count, voice=voices.resolve.call_count,
                         transcript=transcripts.load.call_count)
            self.assertEqual(calls, Counter(project=8, decode=8, parse=1, image=1, voice=1, transcript=1))
            self.assertEqual((page["total"], page["candidateCount"]), (7, 8))
            self.assertEqual(page["mode"], "keyword")
            self.assertEqual(page["sortVersion"], "precise-v1")
            self.assertEqual(page["projectionVersion"], search.PROJECTION_VERSION)
            get_calls = source._databases.get_calls.copy()
            with patch.object(SQLiteShardReader, "__init__", side_effect=AssertionError("continuation reopened SQLite")), \
                    patch.object(source._databases, "get_snapshot", side_effect=AssertionError("continuation reacquired snapshot")), \
                    patch.object(source, "iter_keyword_documents", side_effect=AssertionError("continuation rescanned")):
                items = self.collect(page)
                second = self.more(page)
                previous = self.more(second, previous=True)
            self.assertEqual(previous["items"], page["items"])
            self.assertEqual([item["content"] for item in items[:4]], [f"needle text {i}" for i in range(4)])
            self.assertIn("needle card", items[4]["content"])
            self.assertEqual(items[5]["transcript"], "needle rendered transcript")
            self.assertEqual(items[6]["content"], "[图片]")
            self.assertEqual(items[6]["attachmentPath"], str(self.root / "needle-image.png"))
            self.assertEqual((project.call_count, decode.call_count, parse.call_count), (8, 8, 1))
            self.assertEqual((images.resolve.call_count, voices.resolve.call_count, transcripts.load.call_count), (1, 1, 1))
            self.assertEqual(source._databases.get_calls, get_calls)

    def test_legacy_sender_content_kind_transcript_image_and_literal_matching(self):
        self.factory.add_shard("media.db", {"g@chatroom": [
            {"create_time": DAY, "content": "plain", "real_sender_id": 3},
            {"create_time": DAY + 1, "content": "中文 😀 %_ with space", "real_sender_id": 1},
            {"create_time": DAY + 2, "content": '<msg><voicemsg voicelength="1000"/></msg>', "local_type": 34},
            {"create_time": DAY + 3, "content": "a" * 32, "local_type": 3},
        ]})
        source = self.activate_live()
        source._voices, source._transcripts, source._images = Mock(), Mock(), Mock()
        source._voices.resolve.return_value = self.root / "voice.silk"
        source._transcripts.load.return_value = "rendered needle transcript"
        source._images.resolve.return_value = self.root / "needle-image.png"
        for query in ("STRASSE", "我", "中文 😀", "%_", "VOICE", "needle-image", "rendered needle", " ", ""):
            with self.subTest(query=query):
                reference = source.get_messages("g@chatroom", start_epoch=None, end_epoch_exclusive=None,
                                                query=query, offset=0, limit=100, order="asc")
                actual = self.collect(self.first(query, "g@chatroom"))
                expected = [worker._real_message_json(item) for item in reference.items]
                for item in actual:
                    item["timestamp"] = datetime.fromisoformat(item["timestamp"]).isoformat(timespec="seconds")
                self.assertEqual(actual, expected)
        # Recovery-disabled image/voice messages retain the legacy SYSTEM kind.
        source._voices = source._transcripts = source._images = None
        self.assertEqual(self.first("VOICE", "g@chatroom")["total"], 0)
        self.assertEqual(self.first("system", "g@chatroom")["total"], 2)

    def test_multishard_precise_order_repeated_ids_and_duplicate_locations(self):
        for shard in range(3):
            self.factory.add_shard(f"shard-{shard}.db", {"c": [
                {"create_time": DAY * 1000 + ms, "local_id": 1, "server_id": 7,
                 "content": f"needle {shard}/{index}"} for index, ms in enumerate((999, 0, 0, 500))
            ]})
        source = self.activate_live()
        relative, table = source._locations["c"][0]
        source._locations["c"].extend([(relative, table), (relative.replace("\\", "/"), table)])
        # Same time is ordered by shard/table/physical row, not duplicate IDs.
        expected = [f"needle {shard}/{index}" for indices in ((1, 2), (3,), (0,))
                    for shard in range(3) for index in indices]
        ascending = self.collect(self.first())
        self.assertEqual([item["content"] for item in ascending], expected)
        self.assertEqual(len(ascending), 12)
        descending = self.collect(self.first(order="desc"))
        self.assertEqual([item["content"] for item in descending], list(reversed(expected)))
        self.assertTrue(any(".999000" in item["timestamp"] for item in ascending))

    def test_exact_total_above_a_top_k_and_zero_match_candidate_count(self):
        self.factory.add_shard("dense.db", {"c": [
            {"create_time": DAY + i, "content": f"needle {i}"} for i in range(1005)
        ]})
        self.activate_live()
        first = self.first(limit=100)
        self.assertEqual((first["total"], first["candidateCount"]), (1005, 1005))
        self.assertEqual(len(self.collect(first, limit=100)), 1005)
        empty = self.first("not present")
        self.assertEqual((empty["total"], empty["candidateCount"], empty["items"]), (0, 1005, []))
        self.assertIsNone(empty["nextCursor"])
        self.assertIsNone(empty["previousCursor"])

    def test_generation_change_during_scan_never_publishes_and_existing_session_expires(self):
        path = self.factory.add_shard("changing.db", {"c": [
            {"create_time": DAY + i, "content": f"needle {i}"} for i in range(4)
        ]})
        source = self.activate_live()
        before = self.first()
        project = source._row_to_message
        updated = False

        def changed(*args, **kwargs):
            nonlocal updated
            result = project(*args, **kwargs)
            if not updated:
                updated = True
                # A WAL commit can proceed while the reader's snapshot is open.
                connection = sqlite3.connect(path)
                connection.execute(f'UPDATE "{table_name("c")}" SET message_content=? WHERE rowid=1', ("changed",))
                connection.commit()
                connection.close()
            return result

        connection = sqlite3.connect(path)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.close()
        # The earlier session is already invalid after changing journal mode.
        self.assert_error("PAGE_SOURCE_CHANGED", self.more, before)
        self.assert_error("PAGE_EXPIRED", self.more, before)
        with patch.object(source, "_row_to_message", side_effect=changed):
            self.assert_error("PAGE_SOURCE_CHANGED", self.first)
        self.assertFalse(self.context.keyword_page_cache.sessions)
        self.assertFalse(self.context.keyword_page_states)
        self.assertEqual(self.budget.bytes_used, 0)

    def test_media_configuration_revision_and_projection_changes_are_explicit(self):
        self.factory.add_shard("revisions.db", {"c": [
            {"create_time": DAY + i, "content": "needle"} for i in range(4)
        ]})
        source = self.activate_live()
        page = self.first()
        source._media_revision += 1
        self.assert_error("PAGE_CRITERIA_CHANGED", self.more, page)
        self.assert_error("PAGE_EXPIRED", self.more, page)
        page = self.first()
        source._images = Mock()
        self.assert_error("PAGE_CRITERIA_CHANGED", self.more, page)
        for name in ("SORT_VERSION", "PROJECTION_VERSION"):
            page = self.first()
            with patch.object(search, name, "next-version"):
                self.assert_error("PAGE_EXPIRED", self.more, page)
        self.assertEqual(self.budget.bytes_used, 0)

    def test_ttl_and_cross_query_cursor_tampering_expire_without_rescanning(self):
        self.factory.add_shard("expiry.db", {"c": [
            {"create_time": DAY + i, "content": "needle"} for i in range(5)
        ]})
        source = self.activate_live()
        first, second = self.first(), self.first()
        self.assert_error("PAGE_EXPIRED", self.more, second, cursor=first["nextCursor"])
        self.assert_error("PAGE_EXPIRED", self.more, first, cursor="after.2.forged")
        page = self.first()
        session = self.context.keyword_page_cache.sessions[page["pageSessionId"]]
        with patch.object(source, "iter_keyword_documents", side_effect=AssertionError("rescan")), \
                patch.object(search.time, "monotonic", return_value=session.expires_at):
            self.assert_error("PAGE_EXPIRED", self.more, page)
        self.assertEqual(self.budget.bytes_used, 0)
        self.assertFalse(self.context.keyword_page_states)

    def test_candidate_overflow_and_time_budget_fail_without_partial_publication(self):
        self.factory.add_shard("limits.db", {"c": [
            {"create_time": DAY + i, "content": "no match"} for i in range(4)
        ]})
        self.activate_live()
        with patch.object(worker, "MAX_EXPORT_MESSAGES", 3):
            self.assert_error("RESULT_LIMIT_EXCEEDED", self.first)
        with patch.object(worker, "MAX_EXPORT_MESSAGES", 4):
            self.assertEqual(self.first()["candidateCount"], 4)
        worker._clear_keyword_pages(self.context)
        with patch.object(worker, "SOFT_DEADLINE_SECONDS", -1):
            error = self.assert_error("PAGE_BUDGET_EXCEEDED", self.first)
            self.assertTrue(error.retryable)
        self.assertEqual(self.budget.bytes_used, 0)
        self.assertFalse(self.context.keyword_page_cache.sessions)

    def test_shared_a_b3_ledger_evicts_and_budget_failure_does_not_publish(self):
        self.factory.add_shard("budget.db", {"c": [
            {"create_time": DAY + i, "content": "needle " + "x" * 200} for i in range(4)
        ]})
        self.activate_live()
        self.first()
        exact_keyword_bytes = self.context.keyword_page_cache.bytes_used
        worker._clear_keyword_pages(self.context)
        a = worker.handle_search_messages({
            "source": "wechat4", "sourceSessionId": self.context.source_session_id, "query": "needle",
        }, self.context)
        self.assertIn(a["searchId"], self.context.search_cache.sessions)
        with patch.object(search, "QUERY_CACHE_BYTES", exact_keyword_bytes):
            b = self.first()
        self.assertNotIn(a["searchId"], self.context.search_cache.sessions)
        self.assertIn(b["pageSessionId"], self.context.keyword_page_cache.sessions)
        self.assertEqual(self.budget.bytes_used, self.context.keyword_page_cache.bytes_used)
        worker._clear_keyword_pages(self.context)
        with patch.object(search, "QUERY_CACHE_BYTES", 6000):
            self.assert_error("SEARCH_CACHE_LIMIT", self.first)
        self.assertFalse(self.context.keyword_page_cache.sessions)
        self.assertEqual(self.budget.bytes_used, 0)

    def test_response_bytes_adjust_both_directions_without_clipping_or_skipping_messages(self):
        self.factory.add_shard("response.db", {"c": [
            {"create_time": DAY + i, "content": f"needle {i} " + "x" * 700} for i in range(7)
        ]})
        self.activate_live()
        with patch.object(search, "MAX_RESPONSE_BYTES", 6500):
            first = self.first(limit=100)
            self.assertLess(len(first["items"]), 7)
            items = self.collect(first, limit=100)
            self.assertEqual([item["content"][7] for item in items], list("0123456"))
            second = self.more(first, limit=100)
            previous = self.more(second, previous=True, limit=100)
            self.assertEqual(previous["items"], first["items"])
            for item in items:
                self.assertTrue(item["content"].endswith("x" * 700))
        worker._clear_keyword_pages(self.context)
        with patch.object(search, "MAX_RESPONSE_BYTES", 4500):
            self.assert_error("RESPONSE_LIMIT_EXCEEDED", self.first)
        self.assertFalse(self.context.keyword_page_cache.sessions)
        self.assertEqual(self.budget.bytes_used, 0)

    def test_invalid_conditions_and_continuation_replacements_are_rejected(self):
        self.factory.add_shard("validation.db", {"c": [
            {"create_time": DAY + i, "content": "needle"} for i in range(4)
        ]})
        self.activate_live()
        for overrides in ({"query": 3}, {"query": False}, {"query": []}, {"limit": True}, {"limit": 101},
                          {"query": "x" * 4097}, {"query": "\ud800"}, {"conversationId": ""},
                          {"order": "wrong"}, {"startDate": "2026-11-25", "endDate": "2026-11-24"},
                          {"sourceSessionId": None}, {"source": None}):
            with self.subTest(overrides=overrides):
                expected = "SOURCE_NOT_FOUND" if "source" in overrides else "INVALID_ARGUMENT"
                self.assert_error(expected, self.first, **overrides)
        page = self.first()
        for extra in ({"query": "different"}, {"query": None}, {"startDate": "2026-11-24"},
                      {"endDate": "2026-11-24"}, {"order": "desc"}, {"conversationId": "other"}):
            self.assert_error("PAGE_CRITERIA_CHANGED", self.more, page, **extra)
        self.assert_error("INVALID_ARGUMENT", self.more, page, enableImages=True)
        # Rejected client replacement fields cannot silently alter stored criteria.
        self.assertTrue(self.more(page)["items"])

    def test_unsupported_schema_and_broken_compression_do_not_report_false_zero_results(self):
        for name, rows, without_rowid, expected in (
            ("no-rowid.db", [{"create_time": DAY}], True, "PAGE_UNSUPPORTED_SCHEMA"),
            ("mixed.db", [{"create_time": DAY}, {"create_time": DAY * 1000}], False, "PAGE_UNSUPPORTED_SCHEMA"),
            ("corrupt.db", [{"create_time": DAY, "content": b"not-zstd", "compression": 4}], False, "MESSAGE_DECODE_FAILED"),
        ):
            with self.subTest(name=name):
                factory = SyntheticSourceFactory(self.root)
                factory.add_shard(name, {"c": rows}, without_rowid=without_rowid)
                source = factory.source()
                self.addCleanup(source.close)
                self.context.wechat = source
                worker._activate_source(self.context, "wechat4", force=True)
                self.assert_error(expected, self.first)
                self.assertFalse(self.context.keyword_page_cache.sessions)
                self.assertEqual(self.budget.bytes_used, 0)

    def test_b2_unfiltered_and_complete_export_stay_separate_from_keyword_cache(self):
        self.factory.add_shard("export.db", {"c": [
            {"create_time": DAY + i, "content": f"needle {i}"} for i in range(17)
        ]})
        source = self.activate_live()
        page = self.first()
        with patch.object(source, "iter_keyword_documents", side_effect=AssertionError("B2 used B3 scan")):
            b2 = self.first(None)
        self.assertNotIn("candidateCount", b2)
        self.assertEqual((b2["total"], len(b2["items"])), (17, 2))
        self.assertFalse(b2["pageSessionId"].startswith(search.KEYWORD_SESSION_PREFIX))
        conversation, messages = worker._real_export_models(self.context, {
            "source": "wechat4", "conversationId": "c", "query": "needle",
        })
        self.assertEqual(conversation.id, "c")
        self.assertEqual(len(messages), 17)
        self.assertEqual(len(self.collect(page)), 17)
        self.assertEqual(len(source.get_messages("c", start_epoch=None, end_epoch_exclusive=None,
                                               query="needle", offset=0, limit=200000).items), 17)

    def test_explicit_transcription_clears_snapshots_even_if_partially_failed(self):
        self.factory.add_shard("transcribe.db", {"c": [
            {"create_time": DAY + i, "content": "needle"} for i in range(4)
        ]})
        source = self.activate_live()
        source.voice_available = True
        for failure in (False, True):
            page = self.first()
            with patch.object(worker, "_model_root", return_value=self.root), \
                    patch.object(source, "transcribe", side_effect=live.SourceFailure("VOICE_FAILED", "synthetic")
                                 if failure else None, return_value=[]):
                if failure:
                    self.assert_error("VOICE_FAILED", worker.handle_transcribe_messages,
                                      {"source": "wechat4", "conversationId": "c"}, self.context)
                else:
                    worker.handle_transcribe_messages({"source": "wechat4", "conversationId": "c"}, self.context)
            self.assertFalse(self.context.keyword_page_cache.sessions)
            self.assertFalse(self.context.keyword_page_states)
            self.assertEqual(self.budget.bytes_used, 0)
            self.assert_error("PAGE_EXPIRED", self.more, page)
        revision = source._media_revision
        with patch.object(live, "VoiceTranscriber") as transcriber:
            transcriber.return_value.transcribe_messages.side_effect = OSError("synthetic")
            self.assert_error("VOICE_FAILED", source.transcribe, [], model_root=self.root)
        self.assertEqual(source._media_revision, revision + 1)

    def offline_fixture(self):
        path = self.root / "offline.json"
        path.write_text(json.dumps({"version": 1, "conversations": [{
            "id": "c", "name": "offline Straße", "kind": "direct", "messages": [
                {"id": "before", "sender": "sender", "type": "text", "content": "needle before",
                 "timestamp": "2026-11-23T23:59:59.999999+08:00"},
                {"id": "start", "sender": "sender", "type": "text", "content": "needle start",
                 "timestamp": "2026-11-24T00:00:00+08:00"},
                {"id": "image", "sender": "sender", "type": "image", "content": str(self.root / "needle-never-open.png"),
                 "timestamp": "2026-11-24T12:00:00+08:00"},
                {"id": "voice", "sender": "sender", "type": "voice", "content": "voice placeholder",
                 "transcript": "hidden needle transcript", "reply_to": "start",
                 "timestamp": "2026-11-24T23:59:59.999999+08:00"},
                {"id": "after", "sender": "sender", "type": "text", "content": "needle after",
                 "timestamp": "2026-11-25T00:00:00+08:00"},
            ],
        }]}), encoding="utf-8")
        return path

    def test_offline_preserves_legacy_matching_display_dates_and_no_path_io(self):
        path = self.offline_fixture()
        worker.handle_import_json({"path": str(path)}, self.context)
        path.unlink()
        with patch.object(Path, "stat", side_effect=AssertionError("offline media path touched")), \
                patch.object(Path, "open", side_effect=AssertionError("offline reference opened")):
            page = self.first(startDate="2026-11-24", endDate="2026-11-24")
            self.assertEqual((page["total"], page["candidateCount"]), (2, 3))
            self.assertEqual([item["id"] for item in page["items"]], ["start", "image"])
            self.assertEqual(page["items"][1]["content"], "[图片]")
            self.assertNotIn("attachmentPath", page["items"][1])
            self.assertEqual(self.first("hidden needle")["total"], 0)
            voice = self.first("voice placeholder")["items"][0]
            self.assertEqual(voice["transcript"], "hidden needle transcript")
            self.assertEqual(voice["replyTo"], "start")
            all_items = self.collect(self.first("", limit=1), limit=1)
            self.assertEqual(len(all_items), 5)
        # Existing A intentionally searches transcript metadata independently.
        a = worker.handle_search_messages({"source": "offline", "sourceSessionId": self.context.source_session_id,
                                          "query": "hidden needle"}, self.context)
        self.assertEqual(a["total"], 1)

    def test_successful_switch_disconnect_and_reimport_clear_failed_operations_preserve(self):
        path = self.offline_fixture()
        worker.handle_import_json({"path": str(path)}, self.context)
        page = self.first("", limit=1)
        old_sid = self.context.source_session_id
        bad = self.root / "bad.json"
        bad.write_text("not json", encoding="utf-8")
        self.assert_error("SOURCE_INVALID", worker.handle_import_json, {"path": str(bad)}, self.context)
        self.assertEqual(self.context.source_session_id, old_sid)
        self.assertTrue(self.more(page)["items"])
        candidate = Mock()
        candidate.connect.side_effect = live.SourceFailure("WECHAT_CONNECT_FAILED", "synthetic")
        with patch.object(live, "WeChat4Source", return_value=candidate):
            self.assert_error("WECHAT_CONNECT_FAILED", worker.handle_connect, {"source": "wechat4"}, self.context)
        self.assertEqual(self.context.source_session_id, old_sid)
        self.assertTrue(self.more(page)["items"])
        worker.handle_import_json({"path": str(path)}, self.context)
        self.assertNotEqual(self.context.source_session_id, old_sid)
        self.assertFalse(self.context.keyword_page_cache.sessions)
        self.assertFalse(self.context.keyword_page_states)
        page = self.first("", limit=1)
        worker.handle_disconnect({}, self.context)
        self.assertFalse(self.context.keyword_page_cache.sessions)
        self.assertEqual(self.budget.bytes_used, 0)
        # Demo supports both keyword snapshots and ordinary immutable B2 slices.
        hello = worker.handle_hello({}, self.context)
        self.assertEqual(hello["sourceCapabilities"]["demo"],
                         ["message_cursor_pagination", "message_keyword_pagination"])
        conversation = worker._memory_search_conversations(self.context, "demo")[0]
        demo = self.first("", conversation=conversation.id, limit=1)
        self.assertTrue(self.more(demo)["items"])
        cursor = self.first(None, conversation=conversation.id, limit=1)
        self.assertEqual(cursor["total"], len(conversation.messages))
        self.assertFalse(cursor["pageSessionId"].startswith(search.KEYWORD_SESSION_PREFIX))
        self.assertTrue(self.more(cursor)["items"])

    def test_queries_do_not_modify_synthetic_database_or_journal_files(self):
        path = self.factory.add_shard("readonly.db", {"c": [
            {"create_time": DAY + i, "content": "needle"} for i in range(5)
        ]})
        self.activate_live()
        before = (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
        self.collect(self.first())
        after = (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
        self.assertEqual(after, before)
        self.assertFalse(Path(str(path) + "-wal").exists())
        self.assertFalse(Path(str(path) + "-journal").exists())


if __name__ == "__main__":
    unittest.main()
