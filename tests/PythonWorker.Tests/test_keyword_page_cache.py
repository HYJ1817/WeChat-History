"""B3 keyword pagination keeps one bounded, fully projected match snapshot."""
from __future__ import annotations

import gc
import sys
import time
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src/PythonWorker"))

from wechat_memory import global_search as search


def document(index, *, content="needle text", sender="合成发送者", kind="text", **extra):
    message = {
        "id": str(index), "sender": sender, "content": content, "kind": kind,
        "timestamp": f"2026-11-24T00:00:{index % 60:02d}+08:00", **extra,
    }
    return search.SearchDocument(
        {"id": "c", "title": "合成会话", "kind": "direct"}, message,
        (index, "c", "message.db", "message_table", index),
        ("message.db", "message_table", index), f"fingerprint-{index}",
    )


def criteria(**overrides):
    values = dict(query="needle", start_date="2026-11-01", end_date="2026-11-30",
                  order="asc", projection_version=search.PROJECTION_VERSION,
                  media_selection={"recoverVoice": True, "recoverImages": False}, conversation_id="c")
    values.update(overrides)
    return search.KeywordSearchCriteria(**values)


class KeywordPageCacheTests(unittest.TestCase):
    def setUp(self):
        self.budget = search._SearchMemoryBudget()
        replacement = patch.object(search, "_QUERY_MEMORY", self.budget)
        replacement.start()
        self.addCleanup(replacement.stop)

    def scan(self, docs, query="needle", order="asc", **kwargs):
        return search.scan_keyword_documents(
            docs, query, order, deadline=time.monotonic() + 20, **kwargs,
        )

    def publish(self, docs, *, current_criteria=None, generations=None, cache=None, **scan_kwargs):
        entries, total, candidates = self.scan(docs, **scan_kwargs)
        cache = cache or search.KeywordPageCache()
        session = cache.create("wechat4", "source-1", entries, total, candidates,
                               generations if generations is not None else {"message.db": 1},
                               current_criteria or criteria())
        return cache, session

    def test_continuation_slices_the_complete_snapshot_without_reopening_scan(self):
        iter_calls = []

        class Once:
            def __iter__(self):
                iter_calls.append(True)
                if len(iter_calls) > 1:
                    raise AssertionError("page continuation must not rescan")
                yield from (document(index) for index in range(65))

        cache, session = self.publish(Once())
        first = cache.page(session.page_session_id, "wechat4", "source-1", {"message.db": 1},
                           criteria(), 0, 50)
        second = cache.page(session.page_session_id, "wechat4", "source-1", {"message.db": 1},
                            criteria(), first.next_offset, 50)
        self.assertEqual(len(iter_calls), 1)
        self.assertEqual((first.total, len(first.items), first.next_offset), (65, 50, 50))
        self.assertEqual((len(second.items), second.next_offset, second.previous_offset), (15, None, 0))
        self.assertEqual(first.items[0].message["content"], "needle text")
        self.assertEqual(first.items[0].locator, ("message.db", "message_table", 0))
        self.assertEqual(first.items[0].fingerprint, "fingerprint-0")
        cache.clear()
        self.assertEqual(self.budget.bytes_used, 0)

    def test_expensive_projection_runs_for_candidates_once_and_never_for_continuation(self):
        calls = Counter()

        def projected_documents():
            for index in range(12):
                for stage in ("scan", "decompress", "parse", "recoverMedia"):
                    calls[stage] += 1
                yield document(index, content="needle full display" if index % 2 else "no match")

        cache, session = self.publish(projected_documents())
        initial_calls = calls.copy()
        offset = 0
        for _ in range(3):
            page = cache.page(session.page_session_id, "wechat4", "source-1", {"message.db": 1},
                              criteria(), offset, 2)
            offset = page.next_offset
        self.assertIsNone(offset)
        self.assertEqual(session.candidate_count, 12)
        self.assertEqual(session.total, 6)
        self.assertEqual(calls, Counter(scan=12, decompress=12, parse=12, recoverMedia=12))
        self.assertEqual(calls, initial_calls)

    def test_full_match_count_is_not_clipped_to_a_page_or_a_top_k(self):
        docs = [document(index, content=f"needle row {index}") for index in range(1_005)]
        cache, session = self.publish(docs, max_candidates=1_005)
        self.assertEqual(session.total, 1_005)
        self.assertEqual(len(session.entries), 1_005)
        page = cache.page(session.page_session_id, "wechat4", "source-1", {"message.db": 1},
                          criteria(), 1_000, 100)
        self.assertEqual(len(page.items), 5)
        self.assertIsNone(page.next_offset)
        self.assertEqual(page.items[-1].message["id"], "1004")

    def test_order_is_fixed_and_stable_for_both_directions(self):
        docs = [document(index) for index in (3, 0, 4, 1, 2)]
        for order, expected in (("asc", ["0", "1", "2", "3", "4"]),
                                ("desc", ["4", "3", "2", "1", "0"])):
            with self.subTest(order=order):
                current = criteria(order=order)
                cache, session = self.publish(docs, current_criteria=current, order=order)
                page = cache.page(session.page_session_id, "wechat4", "source-1", {"message.db": 1},
                                  current, 0, 10)
                self.assertEqual([entry.message["id"] for entry in page.items], expected)
                self.assertEqual(page.order, order)
                cache.clear()

    def test_matching_preserves_casefold_sender_content_type_and_rendered_transcript(self):
        docs = [
            document(0, sender="Straße", content="plain", kind="text"),
            document(1, sender="friend", content="语音转写：中文 😀 %_", kind="voice"),
            document(2, sender="friend", content="普通占位", kind="voice", transcript="hidden transcript"),
            document(3, sender="friend", content="plain", kind="needle-card"),
        ]
        cases = (
            ("STRASSE", ["0"]),
            ("中文 😀", ["1"]),
            ("%_", ["1"]),
            ("VOICE", ["1", "2"]),
            ("NEEDLE-CARD", ["3"]),
            ("hidden transcript", []),  # old matching searches rendered content, not hidden metadata
            (" ", ["1"]),  # whitespace remains a literal keyword, not strip()'d
            ("", ["0", "1", "2", "3"]),  # legacy falsy-query semantics
        )
        for query, expected in cases:
            with self.subTest(query=query):
                entries, total, candidates = self.scan(docs, query=query)
                self.assertEqual(total, len(expected))
                self.assertEqual([entry.message["id"] for entry in entries], expected)
                self.assertEqual(candidates, 4)

    def test_cached_display_is_a_snapshot_not_a_view_of_mutable_source_dicts(self):
        doc = document(1, content="needle original")
        cache, session = self.publish([doc])
        doc.message["content"] = "changed after scan"
        doc.conversation["title"] = "changed title"
        page = cache.page(session.page_session_id, "wechat4", "source-1", {"message.db": 1},
                          criteria(), 0, 10)
        self.assertEqual(page.items[0].message["content"], "needle original")
        self.assertEqual(page.items[0].conversation["title"], "合成会话")

    def test_publication_and_page_outputs_cannot_mutate_the_retained_display_snapshot(self):
        entries, total, candidates = self.scan([document(1)])
        cache = search.KeywordPageCache()
        session = cache.create("wechat4", "source-1", entries, total, candidates,
                               {"message.db": 1}, criteria())
        entries[0].message["content"] = "modified scan result"
        first = cache.page(session.page_session_id, "wechat4", "source-1", {"message.db": 1},
                           criteria(), 0, 50)
        self.assertEqual(first.items[0].message["content"], "needle text")
        first.items[0].message["content"] = "modified page response"
        second = cache.page(session.page_session_id, "wechat4", "source-1", {"message.db": 1},
                            criteria(), 0, 50)
        self.assertEqual(second.items[0].message["content"], "needle text")

    def test_candidate_limit_is_inclusive_and_overflow_fails_without_publication(self):
        entries, total, scanned = self.scan([document(i, content="no match") for i in range(3)],
                                            max_candidates=3)
        self.assertEqual((len(entries), total, scanned), (0, 0, 3))
        del entries
        with self.assertRaises(search.SearchError) as raised:
            self.scan([document(i, content="no match") for i in range(4)], max_candidates=3)
        self.assertEqual(raised.exception.code, "RESULT_LIMIT_EXCEEDED")
        self.assertEqual(self.budget.bytes_used, 0)

    def test_default_candidate_limit_tracks_the_shared_legacy_cap(self):
        self.assertEqual(search.KEYWORD_CANDIDATE_LIMIT, 200_000)
        with patch.object(search, "KEYWORD_CANDIDATE_LIMIT", 2):
            with self.assertRaises(search.SearchError) as raised:
                self.scan(document(i, content="no match") for i in range(3))
        self.assertEqual(raised.exception.code, "RESULT_LIMIT_EXCEEDED")
        self.assertEqual(self.budget.bytes_used, 0)

    def test_empty_result_is_complete_and_still_has_a_valid_expiring_session(self):
        cache, session = self.publish([document(1, content="no match")])
        page = cache.page(session.page_session_id, "wechat4", "source-1", {"message.db": 1},
                          criteria(), 0, 50)
        self.assertEqual((page.total, page.items, page.next_offset), (0, (), None))
        self.assertEqual(session.candidate_count, 1)
        self.assertGreater(self.budget.bytes_used, 0)
        cache.clear()
        self.assertEqual(self.budget.bytes_used, 0)

    def test_source_and_generation_changes_explicitly_invalidate_session(self):
        for changed_source, changed_sid, changed_generation in (
            ("offline", "source-1", {"message.db": 1}),
            ("wechat4", "source-2", {"message.db": 1}),
            ("wechat4", "source-1", {"message.db": 2}),
        ):
            with self.subTest(source=changed_source, sid=changed_sid, generation=changed_generation):
                cache, session = self.publish([document(1)])
                with self.assertRaises(search.SearchError) as raised:
                    cache.get(session.page_session_id, changed_source, changed_sid,
                              changed_generation, criteria())
                self.assertEqual(raised.exception.code, "PAGE_SOURCE_CHANGED")
                self.assertNotIn(session.page_session_id, cache.sessions)
                self.assertEqual(self.budget.bytes_used, 0)

    def test_query_date_order_projection_and_media_selection_are_bound(self):
        changed_criteria = (
            criteria(query="other"),
            criteria(conversation_id="different-conversation"),
            criteria(start_date="2026-11-02"),
            criteria(end_date="2026-11-29"),
            criteria(order="desc"),
            criteria(projection_version="canonical-next"),
            criteria(sort_version="precise-next"),
            criteria(media_selection={"recoverVoice": False, "recoverImages": False}),
        )
        for changed in changed_criteria:
            with self.subTest(criteria=changed):
                cache, session = self.publish([document(1)])
                with self.assertRaises(search.SearchError) as raised:
                    cache.get(session.page_session_id, "wechat4", "source-1",
                              {"message.db": 1}, changed)
                self.assertEqual(raised.exception.code, "PAGE_CRITERIA_CHANGED")
                self.assertFalse(cache.sessions)
                self.assertEqual(self.budget.bytes_used, 0)

    def test_mutable_generation_and_media_inputs_are_bound_by_value(self):
        generation = {"message.db": {"generation": 1, "wal": "first"}}
        cache, session = self.publish([document(1)], generations=generation)
        generation["message.db"]["wal"] = "changed"
        with self.assertRaises(search.SearchError) as changed:
            cache.get(session.page_session_id, "wechat4", "source-1", generation, criteria())
        self.assertEqual(changed.exception.code, "PAGE_SOURCE_CHANGED")

        current = criteria()
        cache, session = self.publish([document(2)], current_criteria=current)
        current.media_selection["recoverVoice"] = False
        with self.assertRaises(search.SearchError) as changed:
            cache.get(session.page_session_id, "wechat4", "source-1", {"message.db": 1}, current)
        self.assertEqual(changed.exception.code, "PAGE_CRITERIA_CHANGED")
        self.assertEqual(self.budget.bytes_used, 0)

    def test_runtime_projection_or_sort_upgrade_expires_the_snapshot(self):
        for name in ("PROJECTION_VERSION", "SORT_VERSION"):
            with self.subTest(name=name):
                current = criteria()
                cache, session = self.publish([document(1)], current_criteria=current)
                with patch.object(search, name, "future-v2"):
                    with self.assertRaises(search.SearchError) as raised:
                        cache.get(session.page_session_id, "wechat4", "source-1", {"message.db": 1}, current)
                self.assertEqual(raised.exception.code, "PAGE_EXPIRED")
                self.assertEqual(self.budget.bytes_used, 0)

    def test_ttl_is_fixed_at_ten_minutes_and_access_does_not_extend_it(self):
        with patch.object(search.time, "monotonic", return_value=100):
            cache, session = self.publish([document(1)])
        self.assertEqual(session.expires_at, 700)
        with patch.object(search.time, "monotonic", return_value=699):
            cache.get(session.page_session_id, "wechat4", "source-1", {"message.db": 1}, criteria())
        with patch.object(search.time, "monotonic", return_value=700):
            with self.assertRaises(search.SearchError) as raised:
                cache.get(session.page_session_id, "wechat4", "source-1", {"message.db": 1}, criteria())
        self.assertEqual(raised.exception.code, "PAGE_EXPIRED")
        self.assertEqual(self.budget.bytes_used, 0)

    def test_expiry_and_explicit_discard_release_the_shared_ledger(self):
        with patch.object(search, "KEYWORD_PAGE_TTL_SECONDS", 0):
            cache, session = self.publish([document(1)])
        with self.assertRaises(search.SearchError) as raised:
            cache.get(session.page_session_id, "wechat4", "source-1", {"message.db": 1}, criteria())
        self.assertEqual(raised.exception.code, "PAGE_EXPIRED")
        self.assertEqual(cache.bytes_used, 0)
        self.assertEqual(self.budget.bytes_used, 0)

        cache, session = self.publish([document(2)])
        cache.discard(session.page_session_id)
        self.assertEqual(self.budget.bytes_used, 0)

    def test_keyword_and_a_sessions_compete_for_one_32_mib_reservation(self):
        a_hits, a_total, a_coverage = search.scan_documents(
            [search.SearchDocument({"id": "c", "title": "c", "kind": "direct"},
                                   {"id": "a", "sender": "s", "timestamp": "t", "kind": "text",
                                    "content": "needle", "transcript": ""},
                                   (0, "c", "s", "t", 0), "a-locator", "a-fingerprint")],
            "needle", "desc", deadline=time.monotonic() + 20,
        )
        a_cache = search.SearchCache()
        a_session = a_cache.create("wechat4", "source-1", a_hits, a_total, a_coverage, {})

        b_entries, b_total, b_candidates = self.scan([document(4)])
        b_cache = search.KeywordPageCache()
        b_criteria = criteria()
        generation = {"message.db": 1}
        criteria_bytes = len(search._criteria_fingerprint(b_criteria)) * 4
        expected_b_bytes = (
            sum(search._keyword_entry_cache_bytes(entry) for entry in b_entries)
            + search._SESSION_OVERHEAD_BYTES + 4 * len(repr(generation)) + criteria_bytes
        )
        with patch.object(search, "QUERY_CACHE_BYTES", expected_b_bytes + a_session.cache_bytes - 1):
            b_session = b_cache.create("wechat4", "source-1", b_entries, b_total,
                                       b_candidates, generation, b_criteria)
        self.assertNotIn(a_session.search_id, a_cache.sessions)
        self.assertIn(b_session.page_session_id, b_cache.sessions)
        self.assertEqual(self.budget.bytes_used, b_cache.bytes_used)
        self.assertEqual(self.budget.bytes_used, expected_b_bytes)

    def test_a_scan_can_evict_b3_and_unpublished_b3_results_are_not_free(self):
        cache, session = self.publish([document(1)])
        probe, _, _ = search.scan_documents([document(0)], "needle", "desc", deadline=time.monotonic() + 20)
        a_scan_bytes = probe.reservation.bytes_used
        del probe
        # Either one fits alone, but their combined full identity/display
        # charges do not. A now also meters its complete locator and sort key.
        with patch.object(search, "QUERY_CACHE_BYTES", max(session.cache_bytes, a_scan_bytes) + 1):
            hits, _, _ = search.scan_documents([document(0)], "needle", "desc",
                                               deadline=time.monotonic() + 20)
        self.assertFalse(cache.sessions)
        self.assertEqual(cache.bytes_used, 0)
        self.assertGreater(self.budget.bytes_used, 0)
        with self.assertRaises(search.SearchError) as expired:
            cache.get(session.page_session_id, "wechat4", "source-1", {"message.db": 1}, criteria())
        self.assertEqual(expired.exception.code, "PAGE_EXPIRED")
        del hits
        self.assertEqual(self.budget.bytes_used, 0)

        entries, _, _ = self.scan([document(3)])
        first_size = self.budget.bytes_used
        with patch.object(search, "QUERY_CACHE_BYTES", first_size * 2 - 1):
            with self.assertRaises(search.SearchError) as failed:
                self.scan([document(4)])
        self.assertEqual(failed.exception.code, "SEARCH_CACHE_LIMIT")
        self.assertEqual(self.budget.bytes_used, first_size)
        del entries
        gc.collect()
        self.assertEqual(self.budget.bytes_used, 0)

    def test_publication_transfers_reservation_without_double_charging(self):
        entries, total, candidates = self.scan([document(1)])
        scan_bytes = self.budget.bytes_used
        bound = criteria()
        generations = {"message.db": 1}
        exact_limit = scan_bytes + 4 * len(repr(generations)) + 4 * len(search._criteria_fingerprint(bound))
        cache = search.KeywordPageCache()
        with patch.object(search, "QUERY_CACHE_BYTES", exact_limit):
            session = cache.create("wechat4", "source-1", entries, total, candidates,
                                   generations, bound)
        self.assertEqual(self.budget.bytes_used, session.cache_bytes)
        self.assertEqual(self.budget.bytes_used, exact_limit)
        self.assertIsNone(entries.reservation)
        cache.discard(session.page_session_id)
        self.assertEqual(self.budget.bytes_used, 0)

    def test_failed_scans_close_iterators_and_release_charge(self):
        for failure in ("source", "rows", "close", "deadline"):
            with self.subTest(failure=failure):
                closed = []

                def documents():
                    try:
                        yield document(1)
                        if failure == "source":
                            raise RuntimeError("synthetic projection failed")
                        yield document(2)
                    finally:
                        closed.append(True)
                        if failure == "close":
                            raise RuntimeError("synthetic close failed")

                kwargs = {"max_candidates": 1} if failure == "rows" else {}
                deadline = -1 if failure == "deadline" else time.monotonic() + 20
                with self.assertRaises((search.SearchError, RuntimeError)):
                    search.scan_keyword_documents(documents(), "needle", "asc", deadline=deadline, **kwargs)
                self.assertEqual(closed, [True])
                self.assertEqual(self.budget.bytes_used, 0)

    def test_failed_publication_or_criteria_mismatch_does_not_publish_invalid_state(self):
        for failure in ("budget", "criteria", "count", "token"):
            with self.subTest(failure=failure):
                entries, total, candidates = self.scan([document(1)])
                cache = search.KeywordPageCache()
                bound = criteria(query="other") if failure == "criteria" else criteria()
                if failure == "budget":
                    context = patch.object(search, "QUERY_CACHE_BYTES", self.budget.bytes_used)
                elif failure == "token":
                    context = patch.object(search.secrets, "token_urlsafe", side_effect=RuntimeError("synthetic token error"))
                else:
                    context = patch.object(search, "QUERY_CACHE_BYTES", search.QUERY_CACHE_BYTES)
                with context:
                    with self.assertRaises((search.SearchError, RuntimeError)):
                        cache.create("wechat4", "source-1", entries, total + (failure == "count"),
                                     candidates, {"message.db": 1}, bound)
                self.assertFalse(cache.sessions)
                self.assertEqual(cache.bytes_used, 0)
                self.assertEqual(self.budget.bytes_used, 0)

    def test_session_count_evicts_the_oldest_snapshot(self):
        cache = search.KeywordPageCache()
        with patch.object(search, "MAX_KEYWORD_PAGE_SESSIONS", 2):
            _, first = self.publish([document(1)], cache=cache)
            _, second = self.publish([document(2)], cache=cache)
            _, third = self.publish([document(3)], cache=cache)
        self.assertEqual(list(cache.sessions), [second.page_session_id, third.page_session_id])
        self.assertEqual(self.budget.bytes_used, second.cache_bytes + third.cache_bytes)
        with self.assertRaises(search.SearchError) as expired:
            cache.get(first.page_session_id, "wechat4", "source-1", {"message.db": 1}, criteria())
        self.assertEqual(expired.exception.code, "PAGE_EXPIRED")
        cache.clear()
        self.assertEqual(self.budget.bytes_used, 0)

    def test_cache_budget_failure_never_publishes_a_partial_snapshot(self):
        with patch.object(search, "QUERY_CACHE_BYTES", 10_000):
            with self.assertRaises(search.SearchError) as raised:
                self.scan([document(1, content="needle" + "x" * 5_000)])
        self.assertEqual(raised.exception.code, "SEARCH_CACHE_LIMIT")
        self.assertEqual(self.budget.bytes_used, 0)

    def test_response_budget_reduces_page_count_not_content_or_exact_total(self):
        content = "needle" + "x" * 1_000
        cache, session = self.publish([document(index, content=content) for index in range(4)])
        with patch.object(search, "MAX_RESPONSE_BYTES", 6_000):
            page = cache.page(session.page_session_id, "wechat4", "source-1", {"message.db": 1},
                              criteria(), 0, 50)
        self.assertEqual(page.total, 4)
        self.assertGreater(len(page.items), 0)
        self.assertLess(len(page.items), 4)
        self.assertEqual(page.items[0].message["content"], content)
        self.assertEqual(page.next_offset, len(page.items))
        with patch.object(search, "MAX_RESPONSE_BYTES", 4_200):
            with self.assertRaises(search.SearchError) as raised:
                cache.page(session.page_session_id, "wechat4", "source-1", {"message.db": 1},
                           criteria(), 0, 1)
        self.assertEqual(raised.exception.code, "RESPONSE_LIMIT_EXCEEDED")

    def test_page_arguments_are_bounded_and_reject_booleans(self):
        cache, session = self.publish([document(1)])
        for offset, limit in ((True, 1), (0, True), (-1, 1), (0, 101), (0, 0)):
            with self.subTest(offset=offset, limit=limit), self.assertRaises(search.SearchError) as raised:
                search.keyword_page(session, offset, limit)
            self.assertEqual(raised.exception.code, "INVALID_ARGUMENT")


if __name__ == "__main__":
    unittest.main()
