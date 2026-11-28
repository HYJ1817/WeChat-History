"""Global Top-K, pure projection, physical identity, and lifecycle contracts."""
from __future__ import annotations

import dataclasses
import gc
import json
import shutil
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from synthetic_messages import ROOT, SyntheticSourceFactory, table_name
from wechat_context_exporter.sources import wechat4_crypto as crypto
from wechat_memory import global_search as search
from wechat_memory import wechat4_source as adapter
from wechat_memory.message_reader import SQLiteShardReader
import worker

STAMP = 1_795_478_400


def document(index, *, conversation="c", content="needle", sender="合成发送者", kind="text", **extra):
    message = {"id": str(index), "sender": sender, "timestamp": "2026-11-24T00:00:00+08:00",
               "content": content, "kind": kind, **extra}
    return search.SearchDocument({"id": conversation, "title": "虚构会话", "kind": "direct"},
                                 message, (index, conversation, "s", "t", index), index, str(index))


class SearchCoreTests(unittest.TestCase):
    def scan(self, documents, query="needle", order="desc", **kwargs):
        return search.scan_documents(documents, query, order, deadline=time.monotonic() + 20, **kwargs)

    def test_top_k_counts_every_match_and_keeps_latest_from_last_scanned_shard(self):
        for count in (999, 1000, 1001):
            with self.subTest(count=count):
                hits, total, coverage = self.scan(document(i) for i in range(count))
                self.assertEqual(total, count)
                self.assertEqual(len(hits), min(1000, count))
                self.assertEqual(hits[0].item["messageId"], str(count - 1))
                self.assertEqual(coverage["scannedMessages"], count)
                self.assertTrue(coverage["complete"])
                cache = search.SearchCache()
                session = cache.create("demo", "source-session", hits, total, coverage, ())
                page = search.search_page(session, 950, 100)
                self.assertEqual(len(page["items"]), min(1000, count) - 950)
                self.assertIsNone(page["nextOffset"])
                self.assertEqual(page["truncated"], count > 1000)
        hits, total, _ = self.scan((document(i) for i in reversed(range(1001))), order="asc")
        self.assertEqual(total, 1001)
        self.assertEqual([hit.item["messageId"] for hit in hits[:3]], ["0", "1", "2"])

    def test_unicode_sender_content_kind_transcript_and_literal_percent_underscore(self):
        docs = [document(0, sender="Straße", content="中文 😀 %_"),
                document(1, kind="voice", content="占位", transcript="已有转写 needle")]
        for query, expected, field in (("STRASSE", "0", "sender"), ("中文", "0", "content"),
                                       ("😀", "0", "content"), ("%_", "0", "content"),
                                       ("VOICE", "1", "kind"), ("已有转写", "1", "transcript")):
            with self.subTest(query=query):
                hits, total, _ = self.scan(docs, query)
                self.assertEqual(total, 1)
                self.assertEqual(hits[0].item["messageId"], expected)
                self.assertIn(field, hits[0].item["matchedFields"])

    def test_snippet_clipping_happens_after_full_content_matching(self):
        hits, total, _ = self.scan([document(1, content="前" * 2000 + "needle" + "后" * 2000)])
        self.assertEqual(total, 1)
        self.assertIn("needle", hits[0].item["snippet"])
        self.assertLess(len(hits[0].item["snippet"]), 250)
        self.assertNotIn("content", hits[0].item)

    def test_partial_coverage_is_reported_even_for_zero_hits(self):
        item = dataclasses.replace(document(1, content="no match"), parse_status="partial", content_truncated=True)
        hits, total, coverage = self.scan([item])
        self.assertEqual((hits, total), ((), 0))
        self.assertEqual(coverage["partialMessages"], 1)
        self.assertEqual(coverage["truncatedMessages"], 1)
        self.assertFalse(coverage["complete"])
        self.assertTrue(coverage["warnings"])

    def test_incomplete_scan_errors_and_closes_generator_instead_of_publishing_zero(self):
        closed = []
        def documents():
            try:
                for index in range(10):
                    yield document(index, content="no match")
            finally:
                closed.append(True)
        with self.assertRaises(search.SearchError) as raised:
            self.scan(documents(), max_scan_rows=5)
        self.assertEqual(raised.exception.code, "SEARCH_BUDGET_EXCEEDED")
        self.assertEqual(closed, [True])
        with self.assertRaises(search.SearchError):
            search.scan_documents([document(1)], "needle", "desc", deadline=time.monotonic() - 1)

    def test_cache_ttl_eviction_source_and_projection_versions(self):
        hits, total, coverage = self.scan([document(1)])
        cache = search.SearchCache()
        first = cache.create("demo", "sid", hits, total, coverage, ())
        with self.assertRaises(search.SearchError) as wrong:
            cache.get(first.search_id, "offline", "sid")
        self.assertEqual(wrong.exception.code, "SOURCE_SESSION_CHANGED")
        with patch.object(search, "PROJECTION_VERSION", "future-v2"):
            with self.assertRaises(search.SearchError) as changed:
                cache.get(first.search_id, "demo", "sid")
        self.assertEqual(changed.exception.code, "SEARCH_EXPIRED")
        self.assertEqual(cache.bytes_used, 0)
        for _ in range(search.MAX_SEARCH_SESSIONS + 1):
            cache.create("demo", "sid", hits, total, coverage, ())
        self.assertEqual(len(cache.sessions), search.MAX_SEARCH_SESSIONS)
        now = time.monotonic()
        with patch.object(search.time, "monotonic", return_value=now + 601):
            cache.purge()
        self.assertEqual(cache.bytes_used, 0)
        self.assertEqual(len(cache.sessions), 0)

    def test_scan_and_cache_have_explicit_memory_budget(self):
        with patch.object(search, "QUERY_CACHE_BYTES", 8000):
            with self.assertRaises(search.SearchError) as raised:
                self.scan(document(i, sender="x" * 1000) for i in range(100))
        self.assertEqual(raised.exception.code, "SEARCH_CACHE_LIMIT")

    def test_search_response_budget_trims_rows_and_next_offset_not_content(self):
        hits, total, coverage = self.scan([document(i, sender="x" * 100) for i in range(10)])
        session = search.SearchCache().create("demo", "sid", hits, total, coverage, ())
        with patch.object(search, "MAX_RESPONSE_BYTES", 6000):
            page = search.search_page(session, 0, 10)
        self.assertGreater(len(page["items"]), 0)
        self.assertLess(len(page["items"]), 10)
        self.assertEqual(page["nextOffset"], len(page["items"]))
        self.assertEqual(page["items"][0]["sender"], "x" * 100)

    def test_context_budget_keeps_target_and_correct_focus_or_fails_explicitly(self):
        conversation = {"id": "c", "title": "name", "kind": "direct"}
        target = {"id": "target", "content": "target text"}
        before = [{"id": str(i), "content": "x" * 5000} for i in range(20)]
        after = [{"id": str(i + 20), "content": "x" * 5000} for i in range(20)]
        with patch.object(search, "MAX_RESPONSE_BYTES", 16000):
            result = search.bounded_context(conversation, before, target, after, False, False)
        self.assertEqual(result["items"][result["focusIndex"]]["id"], "target")
        self.assertTrue(result["hasEarlier"])
        self.assertTrue(result["hasLater"])
        self.assertLess(len(result["items"]), 41)
        with self.assertRaises(search.SearchError) as raised:
            search.bounded_context(conversation, [], {"content": "x" * search.MAX_RESPONSE_BYTES}, [], False, False)
        self.assertEqual(raised.exception.code, "RESPONSE_LIMIT_EXCEEDED")


class SharedSearchMemoryTests(unittest.TestCase):
    def setUp(self):
        self.budget = search._SearchMemoryBudget()
        replacement = patch.object(search, "_QUERY_MEMORY", self.budget)
        replacement.start()
        self.addCleanup(replacement.stop)

    def scan(self, documents, *, order="desc", **kwargs):
        return search.scan_documents(documents, "needle", order, deadline=time.monotonic() + 20, **kwargs)

    def retain(self, cache, index=0):
        return cache.create("demo", "sid", *self.scan([document(index)]), ())

    def test_scan_shares_retained_budget_evicts_oldest_and_keeps_exact_top_k(self):
        for order, indices, expected in (("desc", [2, 3, 4, 0], [4, 3]), ("asc", [3, 2, 1, 9], [1, 2])):
            with self.subTest(order=order):
                cache = search.SearchCache()
                oldest, newer = self.retain(cache, 0), self.retain(cache, 1)
                hit_bytes = search._hit_cache_bytes(oldest.hits[0])
                overhead = 4096
                limit = cache.bytes_used + overhead + hit_bytes
                observations = []

                def documents():
                    for index, value in enumerate(indices):
                        yield document(value)
                        heap_count = min(index + 1, 2)
                        self.assertEqual(self.budget.bytes_used, cache.bytes_used + overhead + heap_count * hit_bytes)
                        self.assertLessEqual(self.budget.bytes_used, limit)
                        observations.append(self.budget.bytes_used)
                        if index == 0:
                            self.assertIn(oldest.search_id, cache.sessions)
                        else:
                            self.assertNotIn(oldest.search_id, cache.sessions)
                            self.assertIn(newer.search_id, cache.sessions)

                with patch.object(search, "QUERY_CACHE_BYTES", limit):
                    hits, total, coverage = self.scan(documents(), order=order, result_limit=2)
                    self.assertEqual(total, len(indices))
                    self.assertEqual(coverage["scannedMessages"], len(indices))
                    self.assertEqual([int(hit.item["messageId"]) for hit in hits], expected)
                    self.assertEqual(len(observations), len(indices))
                    session = cache.create("demo", "sid", hits, total, coverage, ())
                    self.assertEqual(self.budget.bytes_used, cache.bytes_used)
                    self.assertLessEqual(cache.bytes_used, limit)
                    self.assertEqual(len(session.hits), 2)
                cache.clear()
                self.assertEqual(self.budget.bytes_used, 0)

    def test_replacement_releases_size_difference_and_discarded_matches_do_not_consume_budget(self):
        for order, indices in (("desc", [1, 2, 0]), ("asc", [2, 1, 3])):
            with self.subTest(order=order):
                observed = []

                def documents():
                    yield document(indices[0], sender="x" * 1000)
                    observed.append(self.budget.bytes_used)
                    yield document(indices[1])
                    observed.append(self.budget.bytes_used)
                    # This match is worse than the sole retained result. Its
                    # large summary must not evict anything or fail the scan.
                    yield document(indices[2], sender="x" * 10000)
                    observed.append(self.budget.bytes_used)

                with patch.object(search, "QUERY_CACHE_BYTES", 16000):
                    hits, total, coverage = self.scan(documents(), order=order, result_limit=1)
                self.assertEqual((total, coverage["scannedMessages"]), (3, 3))
                self.assertEqual(hits[0].item["messageId"], str(indices[1]))
                self.assertGreater(observed[0], observed[1])
                self.assertEqual(observed[1], observed[2])
                del hits
                self.assertEqual(self.budget.bytes_used, 0)

    def test_large_locator_fails_during_retention_and_releases_the_scan(self):
        closed = []

        def documents():
            try:
                yield dataclasses.replace(document(0), locator=("physical-row", "x" * 8_000))
            finally:
                closed.append(True)

        with patch.object(search, "QUERY_CACHE_BYTES", 16_000):
            with self.assertRaises(search.SearchError) as raised:
                self.scan(documents())
        self.assertEqual(raised.exception.code, "SEARCH_CACHE_LIMIT")
        self.assertEqual(closed, [True])
        self.assertEqual(self.budget.bytes_used, 0)

    def test_projection_sidecars_share_the_ledger_with_existing_a_and_b3_sessions(self):
        a_cache, b_cache = search.SearchCache(), search.KeywordPageCache()
        oldest = a_cache.create("demo", "sid", *self.scan([document(0, sender="x" * 2_000)]), ())
        entries, total, candidates = search.scan_keyword_documents(
            [document(1)], "needle", "asc", deadline=time.monotonic() + 20,
        )
        criteria = search.KeywordSearchCriteria("needle", None, None, "asc")
        b_session = b_cache.create("demo", "sid", entries, total, candidates, (), criteria)
        del entries
        projections = []

        def projected(index):
            projection = {"displayKind": "forward", "forwardItems": [
                {"content": "needle " + "x" * 1_000, "forwardItems": []},
            ]}
            projections.append(projection)
            return dataclasses.replace(document(index), locator=adapter._LiveLocator(
                "c", "message.db", "table", index, index, adapter._ProjectionSlot(projection),
            ))

        first, second = projected(2), projected(3)
        # The limit admits existing A+B3 and one full sidecar. Retaining the
        # second must charge its projection immediately and evict old A only.
        summary, _, _ = self.scan([first])
        hit_bytes = search._hit_cache_bytes(summary[0])
        del summary
        limit = self.budget.bytes_used + search._SESSION_OVERHEAD_BYTES + hit_bytes
        observations = []

        def documents():
            yield first
            observations.append(self.budget.bytes_used)
            self.assertGreaterEqual(self.budget.bytes_used, a_cache.bytes_used + b_cache.bytes_used
                                    + search._SESSION_OVERHEAD_BYTES
                                    + 4 * search.encoded_size(projections[0]))
            self.assertIn(oldest.search_id, a_cache.sessions)
            yield second
            self.assertNotIn(oldest.search_id, a_cache.sessions)
            self.assertIn(b_session.page_session_id, b_cache.sessions)
            self.assertLessEqual(self.budget.bytes_used, limit)

        with patch.object(search, "QUERY_CACHE_BYTES", limit):
            hits, total, coverage = self.scan(documents(), result_limit=2)
            self.assertEqual((total, coverage["scannedMessages"]), (2, 2))
            self.assertTrue(coverage["complete"])
            self.assertEqual([hit.item["messageId"] for hit in hits], ["3", "2"])
            worker._attach_live_search_projections(hits)
            session = a_cache.create("demo", "sid", hits, total, coverage, ())
            self.assertEqual(self.budget.bytes_used, a_cache.bytes_used + b_cache.bytes_used)
            self.assertLessEqual(self.budget.bytes_used, limit)
            self.assertTrue(all(hit.locator.projection_slot.projection is None for hit in session.hits))
            self.assertEqual(session.hits[0].item["forwardItems"], projections[1]["forwardItems"])
        self.assertEqual(len(observations), 1)
        a_cache.clear()
        b_cache.clear()
        self.assertEqual(self.budget.bytes_used, 0)

    def test_publication_transfers_reservation_without_double_charging_across_caches(self):
        first_cache, second_cache = search.SearchCache(), search.SearchCache()
        first = self.retain(first_cache)
        hits, total, coverage = self.scan([document(1)])
        exact_limit = self.budget.bytes_used + 4 * len(repr(()))
        with patch.object(search, "QUERY_CACHE_BYTES", exact_limit):
            second = second_cache.create("demo", "sid", hits, total, coverage, ())
        self.assertIn(first.search_id, first_cache.sessions)
        self.assertEqual(self.budget.bytes_used, exact_limit)
        self.assertEqual(self.budget.bytes_used, first.cache_bytes + second.cache_bytes)
        second_cache.discard(second.search_id)
        second_cache.discard(second.search_id)
        self.assertEqual(self.budget.bytes_used, first_cache.bytes_used)
        first_cache.clear()
        self.assertEqual(self.budget.bytes_used, 0)

    def test_reusing_published_hits_acquires_a_separate_reservation(self):
        cache = search.SearchCache()
        hits, total, coverage = self.scan([document(1)])
        first = cache.create("demo", "sid", hits, total, coverage, ())
        second = cache.create("demo", "sid", hits, total, coverage, ())
        self.assertEqual(self.budget.bytes_used, first.cache_bytes + second.cache_bytes)
        cache.discard(first.search_id)
        self.assertEqual(self.budget.bytes_used, second.cache_bytes)
        cache.clear()
        self.assertEqual(self.budget.bytes_used, 0)

    def test_completed_unpublished_results_remain_charged_until_abandoned(self):
        hits, _, _ = self.scan([document(1)])
        first_size = self.budget.bytes_used
        closed = []

        def documents():
            try:
                yield document(2)
            finally:
                closed.append(True)

        with patch.object(search, "QUERY_CACHE_BYTES", first_size * 2 - 1):
            with self.assertRaises(search.SearchError) as raised:
                self.scan(documents())
        self.assertEqual(raised.exception.code, "SEARCH_CACHE_LIMIT")
        self.assertEqual(closed, [True])
        self.assertEqual(self.budget.bytes_used, first_size)
        del hits
        gc.collect()
        self.assertEqual(self.budget.bytes_used, 0)

    def test_failed_scans_release_charge_and_close_iterators(self):
        cache = search.SearchCache()
        existing = self.retain(cache)
        for failure in ("source", "rows", "memory", "close"):
            with self.subTest(failure=failure):
                closed = []

                def documents():
                    try:
                        yield document(1)
                        if failure == "source":
                            raise RuntimeError("synthetic source failure")
                        yield document(2, sender="x" * 10000 if failure == "memory" else "合成发送者")
                    finally:
                        closed.append(True)
                        if failure == "close":
                            raise RuntimeError("synthetic close failure")

                kwargs = {"max_scan_rows": 1} if failure in {"rows", "close"} else {}
                with patch.object(search, "QUERY_CACHE_BYTES", existing.cache_bytes + 12000):
                    with self.assertRaises((search.SearchError, RuntimeError)):
                        self.scan(documents(), **kwargs)
                self.assertEqual(closed, [True])
                self.assertEqual(self.budget.bytes_used, cache.bytes_used)
                self.assertIn(existing.search_id, cache.sessions)
        cache.clear()
        self.assertEqual(self.budget.bytes_used, 0)

    def test_failed_publication_releases_scan_charge_including_generation_overhead(self):
        for failure in ("generations", "session"):
            with self.subTest(failure=failure):
                cache = search.SearchCache()
                hits, total, coverage = self.scan([document(1)])
                scan_bytes = self.budget.bytes_used
                if failure == "generations":
                    with patch.object(search, "QUERY_CACHE_BYTES", scan_bytes):
                        with self.assertRaises(search.SearchError) as raised:
                            cache.create("demo", "sid", hits, total, coverage, ())
                    self.assertEqual(raised.exception.code, "SEARCH_CACHE_LIMIT")
                else:
                    with patch.object(search.secrets, "token_urlsafe", side_effect=RuntimeError("synthetic token failure")):
                        with self.assertRaises(RuntimeError):
                            cache.create("demo", "sid", hits, total, coverage, ())
                self.assertFalse(cache.sessions)
                self.assertEqual(cache.bytes_used, 0)
                self.assertEqual(self.budget.bytes_used, 0)

    def test_expiry_and_abandoned_caches_release_the_shared_allowance(self):
        expired_cache, new_cache = search.SearchCache(), search.SearchCache()
        expired = self.retain(expired_cache)
        with patch.object(search.time, "monotonic", return_value=expired.expires_at + 1), \
             patch.object(search, "QUERY_CACHE_BYTES", expired.cache_bytes):
            hits, total, coverage = self.scan([document(1)])
            self.assertFalse(expired_cache.sessions)
            self.assertEqual(expired_cache.bytes_used, 0)
            current = new_cache.create("demo", "sid", hits, total, coverage, ())
            self.assertEqual(self.budget.bytes_used, current.cache_bytes)
        with patch.object(search.time, "monotonic", return_value=current.expires_at):
            with self.assertRaises(search.SearchError) as raised:
                new_cache.get(current.search_id, "demo", "sid")
        self.assertEqual(raised.exception.code, "SEARCH_EXPIRED")
        self.assertEqual(self.budget.bytes_used, 0)
        abandoned_cache = search.SearchCache()
        self.retain(abandoned_cache)
        self.assertGreater(self.budget.bytes_used, 0)
        del abandoned_cache
        gc.collect()
        self.assertEqual(self.budget.bytes_used, 0)


class LiveSearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="synthetic-search-")
        self.addCleanup(self.temp.cleanup)
        self.factory = SyntheticSourceFactory(Path(self.temp.name))
        self.budget = search._SearchMemoryBudget()
        replacement = patch.object(search, "_QUERY_MEMORY", self.budget)
        replacement.start()
        self.addCleanup(replacement.stop)
        self.context = worker.Context(str(ROOT / "demo/conversations.json"))
        self.addCleanup(self.context.search_cache.clear)
        self.addCleanup(worker._clear_keyword_pages, self.context)

    def activate(self):
        self.source = self.factory.source()
        self.addCleanup(self.source.close)
        self.context.wechat = self.source
        worker._activate_source(self.context, "wechat4", force=True)
        return self.source

    def payload(self, **extra):
        return {"source": "wechat4", "sourceSessionId": self.context.source_session_id, **extra}

    def search(self, **extra):
        return worker.handle_search_messages(self.payload(query="needle", **extra), self.context)

    def test_precise_order_preserves_ms_and_microseconds_and_never_deduplicates_ids(self):
        self.factory.add_shard("message_0.db", {"c": [
            {"create_time": STAMP * 1000 + 900, "local_id": 1, "server_id": 77, "content": "needle late-ms"},
            {"create_time": STAMP * 1000 + 100, "local_id": 9, "server_id": 77, "content": "needle early-ms"},
        ]})
        self.factory.add_shard("message_1.db", {"a": [
            {"create_time": STAMP * 1_000_000 + 100001, "local_id": 1, "server_id": 77, "content": "needle microsecond"},
        ]})
        self.factory.add_shard("message_2.db", {"b": [
            {"create_time": STAMP, "local_id": 1, "server_id": 77, "content": "needle seconds"},
        ]})
        source = self.activate()
        result = self.search(order="asc")
        self.assertEqual(result["total"], 4)
        self.assertEqual([item["snippet"] for item in result["items"]],
                         ["needle seconds", "needle early-ms", "needle microsecond", "needle late-ms"])
        self.assertEqual({item["messageId"] for item in result["items"]}, {"77"})
        self.assertEqual(len({item["hitId"] for item in result["items"]}), 4)
        self.assertTrue(result["items"][2]["timestamp"].endswith("100001+08:00"))
        self.assertTrue(all(count == 1 for count in source._databases.get_calls.values()))

    def test_exact_context_across_shards_ignores_search_dates_and_keywords(self):
        self.factory.add_shard("message_0.db", {"c": [
            {"create_time": STAMP + i * 2, "local_id": 1, "server_id": 7,
             "content": "needle" if i == 20 else f"even {i}"} for i in range(30)
        ]})
        self.factory.add_shard("message_1.db", {"c": [
            {"create_time": STAMP + i * 2 + 1, "local_id": 1, "server_id": 7,
             "content": f"odd {i}"} for i in range(30)
        ]})
        source = self.activate()
        result = self.search(order="desc")
        source._images = Mock()
        source._images.resolve.side_effect = AssertionError("must not recover media")
        source._voices = Mock()
        source._voices.resolve.side_effect = AssertionError("must not read audio")
        source._transcripts = Mock()
        source._transcripts.load.side_effect = AssertionError("must not read transcript cache")
        context = worker.handle_get_message_context(self.payload(
            searchId=result["searchId"], hitId=result["items"][0]["hitId"], before=20, after=20), self.context)
        self.assertEqual(context["focusIndex"], 20)
        self.assertEqual(len(context["items"]), 40)
        self.assertEqual(context["items"][20]["content"], "needle")
        self.assertTrue(context["hasEarlier"])
        self.assertFalse(context["hasLater"])
        instants = [datetime.fromisoformat(item["timestamp"]) for item in context["items"]]
        self.assertEqual(instants, sorted(instants))
        source._images.resolve.assert_not_called()
        source._voices.resolve.assert_not_called()
        source._transcripts.load.assert_not_called()

    def test_pure_search_never_reads_live_media_or_transcript_cache(self):
        self.factory.add_shard("message_0.db", {"g@chatroom": [
            {"create_time": STAMP, "local_type": 3, "content": "a" * 32, "real_sender_id": 3},
            {"create_time": STAMP + 1, "local_type": 34, "content": '<msg><voicemsg voicelength="1000"/></msg>'},
        ]})
        source = self.activate()
        source._images, source._voices, source._transcripts = Mock(), Mock(), Mock()
        for method in (source._images.resolve, source._voices.resolve, source._transcripts.load):
            method.side_effect = AssertionError("media/cache I/O is forbidden")
        result = worker.handle_search_messages(self.payload(query="IMAGE"), self.context)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["kind"], "image")
        self.assertNotIn("attachmentPath", json.dumps(result))
        by_sender = worker.handle_search_messages(self.payload(query="strasse"), self.context)
        self.assertEqual(by_sender["total"], 1)
        cached = worker.handle_search_messages(self.payload(query="only in live transcript"), self.context)
        self.assertEqual(cached["total"], 0)

    def test_complex_projection_reply_and_forward_text_are_searchable_and_locate_outer_row(self):
        quote = '<msg><appmsg><type>57</type><title>回复 needle</title><refermsg><type>1</type><svrid>987</svrid><displayname>被引人</displayname><content>' + "前" * 100 + 'deep引用</content></refermsg></appmsg></msg>'
        forward = '<msg><appmsg><type>19</type><title>聊天记录</title><recorditem><recordinfo><datalist><dataitem datatype="1"><sourcename>内部发送者</sourcename><datadesc>转发子项 needle</datadesc></dataitem></datalist></recordinfo></recorditem></appmsg></msg>'
        self.factory.add_shard("message_0.db", {"c": [
            {"create_time": STAMP, "local_type": 49, "content": quote},
            {"create_time": STAMP + 1, "local_type": 49, "content": forward},
            {"create_time": STAMP + 2, "local_type": 49, "content": '<msg><appmsg><type>999</type><title>unknown</title></appmsg></msg>'},
        ]})
        self.activate()
        result = worker.handle_search_messages(self.payload(query="deep引用"), self.context)
        self.assertEqual(result["total"], 1)
        quote_hit = result["items"][0]
        self.assertEqual((quote_hit["displayKind"], quote_hit["replyTo"]), ("quote", "987"))
        self.assertIn("deep引用", quote_hit["quoteSummary"])
        self.assertIn("deep引用", quote_hit["snippet"])
        self.assertNotIn("<msg>", quote_hit["snippet"])
        context = worker.handle_get_message_context(self.payload(searchId=result["searchId"],
            hitId=quote_hit["hitId"], before=0, after=0), self.context)
        self.assertEqual(context["items"][0]["replyTo"], "987")
        self.assertEqual(context["items"][0]["quoteSummary"], quote_hit["quoteSummary"])
        forward_result = worker.handle_search_messages(self.payload(query="转发子项"), self.context)
        self.assertEqual(forward_result["total"], 1)
        forward_hit = forward_result["items"][0]
        self.assertEqual(forward_hit["conversationId"], "c")
        self.assertEqual(forward_hit["displayKind"], "forward")
        self.assertEqual(forward_hit["forwardItems"][0]["content"], "转发子项 needle")
        self.assertEqual(forward_hit["omittedCount"], 0)
        self.assertGreater(forward_result["coverage"]["partialMessages"], 0)
        self.assertFalse(forward_result["coverage"]["complete"])
        self.assertTrue(forward_result["coverage"]["warnings"])
        forward_context = worker.handle_get_message_context(self.payload(searchId=forward_result["searchId"],
            hitId=forward_hit["hitId"], before=0, after=0), self.context)
        self.assertEqual(forward_context["items"][0]["forwardItems"], forward_hit["forwardItems"])
        self.assertEqual(self.search()["total"], 2)

    def test_default_32_mib_limit_rejects_large_forward_sidecars_before_scan_finishes(self):
        body = "needle " + "x" * 60_000
        card = ('<msg><appmsg><type>19</type><title>合成转发</title><recorditem><recordinfo>'
                '<datalist><dataitem datatype="1"><sourcename>合成发送者</sourcename><datadesc>'
                + body + '</datadesc></dataitem></datalist></recordinfo></recorditem></appmsg></msg>')
        self.factory.add_shard("large-cards.db", {"c": [
            {"create_time": STAMP + index, "local_type": 49, "content": card} for index in range(1_000)
        ]})
        source = self.activate()
        original_documents = source.iter_search_documents
        scanned = 0
        projected_bytes = 0
        closed = []

        def documents(*args, **kwargs):
            nonlocal scanned, projected_bytes
            iterator = original_documents(*args, **kwargs)
            try:
                for doc in iterator:
                    scanned += 1
                    projected_bytes += search.encoded_size(doc.message["forwardItems"])
                    yield doc
                    # All these rows would belong to K=1,000. Their complete
                    # sidecars must be charged while the iterator is suspended,
                    # not first counted after the entire scan in cache.create.
                    self.assertGreaterEqual(self.budget.bytes_used, 4 * projected_bytes)
                    self.assertLessEqual(self.budget.bytes_used, 32 * 1024 * 1024)
            finally:
                iterator.close()
                closed.append(True)

        self.assertEqual(search.QUERY_CACHE_BYTES, 32 * 1024 * 1024)
        with patch.object(source, "iter_search_documents", side_effect=documents), patch.object(
            self.context.search_cache, "create", wraps=self.context.search_cache.create,
        ) as publish:
            with self.assertRaises(search.SearchError) as raised:
                self.search(order="asc")
            publish.assert_not_called()
        self.assertEqual(raised.exception.code, "SEARCH_CACHE_LIMIT")
        self.assertGreater(scanned, 1)
        self.assertLess(scanned, 1_000)
        self.assertEqual(closed, [True])
        self.assertFalse(self.context.search_cache.sessions)
        self.assertEqual(self.context.search_cache.bytes_used, 0)
        self.assertEqual(self.budget.bytes_used, 0)
        # A soft budget failure leaves the worker usable for a narrower range.
        refreshed = self.search(startDate="2026-11-25", endDate="2026-11-25")
        self.assertEqual((refreshed["total"], refreshed["coverage"]["scannedMessages"]), (0, 0))
        self.assertTrue(refreshed["coverage"]["complete"])

    def test_forward_projection_budget_preserves_normal_top_k_and_full_coverage(self):
        card = ('<msg><appmsg><type>19</type><title>合成转发 needle</title><recorditem><recordinfo>'
                '<datalist><dataitem datatype="1"><sourcename>合成发送者</sourcename>'
                '<datadesc>needle child</datadesc></dataitem></datalist></recordinfo></recorditem></appmsg></msg>')
        self.factory.add_shard("normal-cards.db", {"c": [
            {"create_time": STAMP + index, "local_type": 49, "content": card} for index in range(1_005)
        ] + [{"create_time": STAMP + 1_005, "local_type": 49,
              "content": '<msg><appmsg><type>999</type><title>unmatched</title></appmsg></msg>'}]})
        self.activate()
        for order, first, last in (("asc", 0, 999), ("desc", 1_004, 5)):
            with self.subTest(order=order):
                result = self.search(order=order, limit=1)
                session = self.context.search_cache.sessions[result["searchId"]]
                self.assertEqual((result["total"], len(session.hits), result["truncated"]), (1_005, 1_000, True))
                self.assertEqual(session.hits[0].sort_key[0], (STAMP + first) * 1_000_000)
                self.assertEqual(session.hits[-1].sort_key[0], (STAMP + last) * 1_000_000)
                self.assertEqual(result["coverage"]["scannedMessages"], 1_006)
                self.assertEqual(result["coverage"]["partialMessages"], 1)
                self.assertFalse(result["coverage"]["complete"])
                self.assertEqual(result["items"][0]["forwardItems"][0]["content"], "needle child")
                self.assertTrue(all(hit.locator.projection_slot.projection is None for hit in session.hits))
                self.assertEqual(self.budget.bytes_used, self.context.search_cache.bytes_used)
                self.context.search_cache.clear()
                self.assertEqual(self.budget.bytes_used, 0)

    def test_pages_slice_fixed_results_without_rescanning(self):
        self.factory.add_shard("message_0.db", {"c": [{"create_time": STAMP + i, "content": "needle"} for i in range(60)]})
        source = self.activate()
        result = self.search()
        with patch.object(source, "iter_search_documents", side_effect=AssertionError("must not rescan")):
            next_page = worker.handle_search_messages(self.payload(searchId=result["searchId"], offset=50, limit=50), self.context)
        self.assertEqual(len(next_page["items"]), 10)
        self.assertIsNone(next_page["nextOffset"])
        self.assertEqual(source._databases.get_calls["message\\message_0.db"], 1)

    def test_source_db_update_and_wal_update_invalidate_without_refreshing_snapshot(self):
        path = self.factory.add_shard("message_0.db", {"c": [{"create_time": STAMP, "content": "needle"}]})
        source = self.activate()
        result = self.search()
        # A normal source update, even when the rowid is reused, must not let a
        # stored locator land on a different message.
        connection = sqlite3.connect(path)
        connection.execute(f'UPDATE "{table_name("c")}" SET message_content=?', ("replacement",))
        connection.commit()
        connection.close()
        with self.assertRaises(adapter.SourceFailure) as changed:
            worker.handle_get_message_context(self.payload(searchId=result["searchId"], hitId=result["items"][0]["hitId"]), self.context)
        self.assertEqual(changed.exception.code, "SEARCH_SOURCE_CHANGED")
        self.assertEqual(len(self.context.search_cache.sessions), 0)
        result = worker.handle_search_messages(self.payload(query="replacement"), self.context)
        Path(str(path) + "-wal").write_bytes(b"synthetic changed wal")
        with self.assertRaises(adapter.SourceFailure) as wal:
            worker.handle_search_messages(self.payload(searchId=result["searchId"], offset=0), self.context)
        self.assertEqual(wal.exception.code, "SEARCH_SOURCE_CHANGED")

    def test_content_identity_is_verified_even_if_a_generation_provider_claims_unchanged(self):
        path = self.factory.add_shard("message_0.db", {"c": [{"create_time": STAMP, "content": "needle"}]})
        source = self.activate()
        result = self.search()
        connection = sqlite3.connect(path)
        connection.execute(f'DELETE FROM "{table_name("c")}"')
        connection.execute(f'INSERT INTO "{table_name("c")}" VALUES (1, ?, 1, 2, ?, 0, 0, NULL)', (STAMP, "other row with reused rowid"))
        connection.commit()
        connection.close()
        with patch.object(source._databases, "is_snapshot_current", return_value=True):
            with self.assertRaises(adapter.SourceFailure) as changed:
                worker.handle_get_message_context(self.payload(searchId=result["searchId"], hitId=result["items"][0]["hitId"]), self.context)
        self.assertEqual(changed.exception.code, "CONTEXT_TARGET_CHANGED")

    def test_declared_name2id_rowid_is_conservatively_rejected_even_with_unique_values(self):
        for index, (declaration, values) in enumerate((
            ("rowid INTEGER UNIQUE", [2]),
            ('"RoWiD" TEXT UNIQUE', ["2", "02"]),
            ("rowid INTEGER PRIMARY KEY", [2]),
        )):
            with self.subTest(declaration=declaration):
                self.factory = SyntheticSourceFactory(Path(self.temp.name))
                path = self.factory.add_shard(f"declared-rowid-{index}.db", {"c": [
                    {"create_time": STAMP, "content": "needle"},
                ]})
                with sqlite3.connect(path) as connection:
                    connection.execute("DROP TABLE Name2Id")
                    connection.execute(f"CREATE TABLE Name2Id ({declaration}, user_name TEXT)")
                    connection.executemany("INSERT INTO Name2Id VALUES (?, 'wxid_friend')", [(value,) for value in values])
                connection.close()
                with SQLiteShardReader(path) as reader:
                    schema = reader.inspect_table(table_name("c"))
                    self.assertIsNone(schema.rowid_column)
                    # SQL-distinct text identities can both join sender integer
                    # 2; uniqueness of the declared values is not sufficient.
                    self.assertEqual(reader.count_rows(table_name("c"), schema, None, None), len(values))
                self.activate()
                with self.assertRaises(adapter.SourceFailure) as raised:
                    self.search()
                self.assertEqual(raised.exception.code, "SEARCH_UNSUPPORTED_SCHEMA")
                self.assertFalse(self.context.search_cache.sessions)
                self.assertEqual(self.context.search_cache.bytes_used, 0)

    def test_failed_snapshot_refresh_invalidates_old_search_generations_and_releases_results(self):
        path = self.factory.add_shard("message_0.db", {"c": [{"create_time": STAMP, "content": "needle"}]})
        original = Path(self.temp.name) / "original"
        (original / "message").mkdir(parents=True)
        shutil.copyfile(path, original / "message/message_0.db")
        cache = crypto.DecryptedDatabaseCache(original, keys={"message\\message_0.db": b"0" * 32},
                                               temp_root=Path(self.temp.name) / "private")
        self.addCleanup(cache.close)
        source = self.activate()
        source._databases = cache

        def synthetic_decrypt(source_path, destination, _key):
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_path, destination)

        def failed_decrypt(_source_path, destination, _key):
            destination.write_bytes(b"synthetic partially recreated plaintext")
            raise crypto.SourceError("synthetic refresh failure")

        with patch.object(crypto, "decrypt_database", side_effect=synthetic_decrypt):
            page_result, context_result = self.search(), self.search()
            snapshot = self.context.search_cache.sessions[page_result["searchId"]].generations["message\\message_0.db"]
            snapshot.path.unlink()
            with patch.object(crypto, "decrypt_database", side_effect=failed_decrypt):
                with self.assertRaises(crypto.SourceError):
                    cache.get_snapshot("message\\message_0.db")
            self.assertTrue(snapshot.path.is_file())
            self.assertFalse(cache.is_snapshot_current("message\\message_0.db", snapshot))
            with self.assertRaises(adapter.SourceFailure) as page_changed:
                worker.handle_search_messages(self.payload(searchId=page_result["searchId"]), self.context)
            self.assertEqual(page_changed.exception.code, "SEARCH_SOURCE_CHANGED")
            with self.assertRaises(adapter.SourceFailure) as context_changed:
                worker.handle_get_message_context(self.payload(searchId=context_result["searchId"],
                    hitId=context_result["items"][0]["hitId"]), self.context)
            self.assertEqual(context_changed.exception.code, "SEARCH_SOURCE_CHANGED")
            self.assertFalse(self.context.search_cache.sessions)
            self.assertEqual(self.context.search_cache.bytes_used, 0)
            refreshed = cache.get_snapshot("message\\message_0.db")
            self.assertGreater(refreshed.generation, snapshot.generation)
            self.assertEqual(refreshed.signature, snapshot.signature)
            self.assertEqual(self.search()["total"], 1)

    def test_unreadable_shard_and_unlocatable_or_mixed_schema_fail_whole_scan(self):
        path = self.factory.add_shard("message_0.db", {"c": [{"create_time": STAMP, "content": "needle"}]})
        self.factory.add_shard("message_1.db", {"d": [{"create_time": STAMP, "content": "needle"}]})
        self.activate()
        path.unlink()
        with self.assertRaises(adapter.SourceFailure) as unreadable:
            self.search()
        self.assertEqual(unreadable.exception.code, "SEARCH_SOURCE_UNREADABLE")
        self.assertFalse(self.context.search_cache.sessions)
        for name, rows, without_rowid in (
            ("unlocatable.db", [{"create_time": STAMP, "content": "needle"}], True),
            ("mixed.db", [{"create_time": STAMP, "content": "needle"},
                          {"create_time": STAMP * 1000 + 1, "content": "needle"}], False),
        ):
            with self.subTest(name=name):
                factory = SyntheticSourceFactory(Path(self.temp.name))
                factory.add_shard(name, {"c": rows}, without_rowid=without_rowid)
                source = factory.source()
                self.addCleanup(source.close)
                self.context.wechat = source
                worker._activate_source(self.context, "wechat4", force=True)
                with self.assertRaises(adapter.SourceFailure) as unsupported:
                    self.search()
                self.assertEqual(unsupported.exception.code, "SEARCH_UNSUPPORTED_SCHEMA")
                self.assertFalse(self.context.search_cache.sessions)

    def test_raw_blob_and_unknown_size_decompression_bombs_are_explicit_failures(self):
        import zstandard
        for name, content, compression in (
            ("raw.db", b"x" * (2 * 1024 * 1024 + 1), 0),
            ("compressed.db", zstandard.ZstdCompressor(write_content_size=False).compress(b"x" * (2 * 1024 * 1024 + 1)), 4),
            ("bad-compressed.db", b"invalid compressed data", 4),
        ):
            with self.subTest(name=name):
                factory = SyntheticSourceFactory(Path(self.temp.name))
                factory.add_shard(name, {"c": [{"create_time": STAMP, "content": content, "compression": compression}]})
                source = factory.source()
                self.addCleanup(source.close)
                self.context.wechat = source
                worker._activate_source(self.context, "wechat4", force=True)
                with self.assertRaises(adapter.SourceFailure) as failure:
                    self.search()
                self.assertIn(failure.exception.code, {"MESSAGE_TOO_LARGE", "MESSAGE_DECODE_FAILED"})
                self.assertFalse(self.context.search_cache.sessions)


class MemorySearchLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="synthetic-memory-search-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.context = worker.Context(str(ROOT / "demo/conversations.json"))

    def payload(self, **extra):
        return {"source": self.context.active_source, "sourceSessionId": self.context.source_session_id, **extra}

    def import_messages(self, messages, *, conversation="offline"):
        path = self.root / "data.json"
        path.write_text(json.dumps({"version": 1, "conversations": [{"id": conversation, "name": "离线虚构会话",
            "kind": "direct", "messages": messages}]}, ensure_ascii=False), encoding="utf-8")
        worker.handle_import_json({"path": str(path)}, self.context)
        return path

    def message(self, index, **extra):
        return {"id": str(index), "sender": "Straße", "timestamp": f"2026-11-{23 + index:02d}T23:59:59.123456+08:00",
                "type": "text", "content": "needle", **extra}

    def test_search_scope_ignores_conversation_name_filter_and_context_ignores_dates(self):
        listed = worker.handle_list_conversations({"source": "demo", "query": "does not exist"}, self.context)
        self.assertEqual(listed["items"], [])
        result = worker.handle_search_messages(self.payload(query="text"), self.context)
        self.assertGreater(result["total"], 0)
        self.import_messages([self.message(0), self.message(1), self.message(2)])
        result = worker.handle_search_messages(self.payload(query="needle", startDate="2026-11-24", endDate="2026-11-24"), self.context)
        self.assertEqual(result["total"], 1)
        context = worker.handle_get_message_context(self.payload(searchId=result["searchId"], hitId=result["items"][0]["hitId"]), self.context)
        self.assertEqual(len(context["items"]), 3)
        self.assertEqual(context["focusIndex"], 1)
        self.assertEqual(context["items"][1]["timestamp"], "2026-11-24T23:59:59.123456+08:00")

    def test_offline_transcript_and_media_references_never_trigger_file_or_network_io(self):
        path = self.import_messages([
            self.message(0, type="voice", content="C:\\private\\voice.silk", transcript="已有 needle 转写"),
            self.message(1, type="image", content="C:\\private\\needle.png"),
            self.message(2, type="file", content="C:\\private\\attachment.txt"),
        ])
        path.unlink()
        with patch.object(Path, "open", side_effect=AssertionError("no file reads")), \
             patch.object(Path, "is_file", side_effect=AssertionError("no media stats")):
            result = worker.handle_search_messages(self.payload(query="needle"), self.context)
            self.assertEqual(result["total"], 1)
            self.assertIn("transcript", result["items"][0]["matchedFields"])
            context = worker.handle_get_message_context(self.payload(searchId=result["searchId"], hitId=result["items"][0]["hitId"]), self.context)
        serialized = json.dumps(result) + json.dumps(context)
        self.assertNotIn("private", serialized)
        self.assertNotIn("attachmentPath", serialized)
        self.assertNotIn("voice.silk", serialized)

    def test_blank_queries_new_conditions_in_continuation_and_forged_tokens_rejected(self):
        for extra in ({"query": "  \t "}, {"query": "x", "offset": 1}, {"query": "x", "order": "newest"},
                      {"query": "x", "limit": True}, {"query": "x", "querySql": "anything"},
                      {"query": "x", "sourceSessionId": "forged"}):
            with self.subTest(extra=extra), self.assertRaises((worker.WorkerError, search.SearchError)):
                worker.handle_search_messages(self.payload(**extra), self.context)
        result = worker.handle_search_messages(self.payload(query="text"), self.context)
        for key, value in (("query", "text"), ("startDate", None), ("order", "desc"), ("conversationId", "c")):
            with self.subTest(key=key), self.assertRaises(worker.WorkerError) as wrong:
                worker.handle_search_messages(self.payload(searchId=result["searchId"], **{key: value}), self.context)
            self.assertEqual(wrong.exception.code, "INVALID_ARGUMENT")
        with self.assertRaises(worker.WorkerError) as missing:
            worker.handle_get_message_context(self.payload(searchId=result["searchId"], hitId="forged"), self.context)
        self.assertEqual(missing.exception.code, "CONTEXT_TARGET_UNKNOWN")
        with self.assertRaises(worker.WorkerError):
            worker.handle_get_message_context(self.payload(searchId=result["searchId"], hitId=result["items"][0]["hitId"], before=21), self.context)

    def test_failed_import_keeps_search_and_successful_import_switch_disconnect_invalidate(self):
        old_token = self.context.source_session_id
        result = worker.handle_search_messages(self.payload(query="text"), self.context)
        bad = self.root / "bad.json"
        bad.write_text("{invalid", encoding="utf-8")
        with self.assertRaises(worker.WorkerError):
            worker.handle_import_json({"path": str(bad)}, self.context)
        self.assertEqual(self.context.source_session_id, old_token)
        self.assertEqual(worker.handle_search_messages(self.payload(searchId=result["searchId"]), self.context)["total"], result["total"])
        self.import_messages([self.message(0)])
        self.assertNotEqual(self.context.source_session_id, old_token)
        self.assertFalse(self.context.search_cache.sessions)
        with self.assertRaises(worker.WorkerError) as changed:
            worker.handle_search_messages({"source": "demo", "sourceSessionId": old_token, "searchId": result["searchId"]}, self.context)
        self.assertEqual(changed.exception.code, "SOURCE_SESSION_CHANGED")
        result = worker.handle_search_messages(self.payload(query="needle"), self.context)
        offline_token = self.context.source_session_id
        worker.handle_disconnect({}, self.context)
        self.assertFalse(self.context.search_cache.sessions)
        self.assertNotEqual(self.context.source_session_id, offline_token)
        with self.assertRaises(worker.WorkerError):
            worker.handle_search_messages({"source": "offline", "sourceSessionId": offline_token, "searchId": result["searchId"]}, self.context)

    def test_soft_failure_does_not_publish_invalid_state_and_subsequent_query_works(self):
        with patch.object(worker, "SOFT_DEADLINE_SECONDS", -1):
            with self.assertRaises(search.SearchError) as failure:
                worker.handle_search_messages(self.payload(query="text"), self.context)
        self.assertEqual(failure.exception.code, "SEARCH_BUDGET_EXCEEDED")
        self.assertFalse(self.context.search_cache.sessions)
        self.assertGreater(worker.handle_search_messages(self.payload(query="text"), self.context)["total"], 0)


if __name__ == "__main__":
    unittest.main()
