"""Source-independent lifecycle tests using a metadata-only synthetic provider."""
from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

WORKER_DIR = Path(__file__).resolve().parents[2] / "src" / "PythonWorker"
if str(WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(WORKER_DIR))

from wechat_memory import page_sessions
from wechat_memory.page_sessions import (
    PageSessionError,
    PageSessionLimits,
    PageSessionManager,
    PageWindow,
    PreparedPage,
)


@dataclass(frozen=True)
class RangeState:
    order: str


class SyntheticRangeProvider:
    """No dataset, SQLite, or JSON import: proves the provider contract alone."""

    def __init__(self) -> None:
        self.generation = 0
        self.reads = 0
        self.prepares = 0
        self.failure: str | None = None
        self.change_on_read = False

    def prepare_page(self, conversation_id, start_epoch, end_epoch_exclusive, order):
        self.prepares += 1
        return PreparedPage(RangeState(order), 10)

    def validate_page(self, state):
        pass

    def read_page(self, state, boundary, direction, limit):
        self.reads += 1
        if self.failure:
            raise PageSessionError(self.failure, "Synthetic page failure", True)
        if self.change_on_read:
            self.generation += 1
        step = 1 if state.order == "asc" else -1
        if boundary is None:
            first = 0 if step == 1 else 9
        else:
            if direction == "before":
                step = -step
            first = boundary + step
        keys = range(first, 10 if step == 1 else -1, step)[:limit + 1]
        selected = keys[:limit]
        if direction == "before":
            selected = selected[::-1]
        return PageWindow(tuple(selected), selected[0] if selected else None,
                          selected[-1] if selected else None, len(keys) > limit)


class PageSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = SyntheticRangeProvider()
        self.manager = PageSessionManager()

    def first(self, *, source_session_id="source", **kwargs):
        arguments = dict(start_epoch=None, end_epoch_exclusive=None, order="asc", limit=2)
        arguments.update(kwargs)
        return self.manager.first_page(
            self.provider, "conversation", source_session_id=source_session_id, **arguments,
        )

    def next(self, page, *, token=None, source_session_id="source", limit=2):
        return self.manager.cursor_page(
            self.provider, source_session_id, page.page_session_id,
            page.next_cursor if token is None else token, limit=limit,
        )

    def assert_error(self, code, callback):
        with self.assertRaises(PageSessionError) as raised:
            callback()
        self.assertEqual(raised.exception.code, code)
        return raised.exception

    def test_navigation_replays_tokens_and_accepts_new_page_sizes_in_both_orders(self):
        for order in ("asc", "desc"):
            with self.subTest(order=order):
                first = self.first(order=order)
                second = self.next(first, limit=3)
                expected = list(range(10)) if order == "asc" else list(reversed(range(10)))
                self.assertEqual(first.items, tuple(expected[:2]))
                self.assertEqual(second.items, tuple(expected[2:5]))
                self.assertEqual(self.next(first, limit=3).items, second.items)
                back = self.next(second, token=second.previous_cursor)
                self.assertEqual(back.items, first.items)
                self.assertIsNone(back.previous_cursor)
                self.assertIsNotNone(back.next_cursor)
                self.assertEqual(first.total, 10)
                self.assertEqual(first.sort_version, "precise-v1")

    def test_sliding_ttl_expires_at_the_boundary_without_provider_work(self):
        self.manager.limits = PageSessionLimits(ttl_seconds=10)
        with patch.object(page_sessions.time, "monotonic", return_value=100):
            first = self.first()
        with patch.object(page_sessions.time, "monotonic", return_value=109):
            self.next(first)
        with patch.object(page_sessions.time, "monotonic", return_value=118):
            self.next(first)
        reads = self.provider.reads
        with patch.object(page_sessions.time, "monotonic", return_value=128):
            error = self.assert_error("PAGE_EXPIRED", lambda: self.next(first))
        self.assertTrue(error.retryable)
        self.assertEqual(self.provider.reads, reads)
        self.assertFalse(self.manager._sessions)

    def test_source_identity_unknown_tokens_and_cross_session_tokens_are_rejected(self):
        first = self.first()
        other = self.first()
        reads = self.provider.reads
        for source_id, session_id, token in (
            ("different-source", first.page_session_id, first.next_cursor),
            ("source", "missing-session", first.next_cursor),
            ("source", first.page_session_id, "missing-token"),
            ("source", first.page_session_id, other.next_cursor),
            ("source", first.page_session_id, None),
            ("source", first.page_session_id, []),
            ("source", [], first.next_cursor),
        ):
            with self.subTest(source=source_id, session=session_id, token=token):
                self.assert_error("PAGE_EXPIRED", lambda: self.manager.cursor_page(
                    self.provider, source_id, session_id, token,
                ))
        self.assertEqual(self.provider.reads, reads)
        self.assertEqual(self.next(first).items, (2, 3))

    def test_session_lru_eviction_and_mapping_compatible_clear(self):
        self.manager.limits = PageSessionLimits(max_sessions=2)
        first = self.first()
        oldest = self.first()
        self.next(first)  # touch the first session; the other session is oldest
        newest = self.first()
        self.assert_error("PAGE_EXPIRED", lambda: self.next(oldest))
        self.assertEqual(self.next(first).items, (2, 3))
        self.manager.clear()
        self.manager.clear()
        self.assert_error("PAGE_EXPIRED", lambda: self.next(newest))
        self.assertFalse(self.manager._sessions)

    def test_token_lru_is_bounded_and_rejects_evicted_boundaries(self):
        self.manager.limits = PageSessionLimits(max_tokens_per_session=3)
        first = self.first()
        second = self.next(first)
        self.next(first)  # replay/touch the original boundary, issuing two more
        self.assertEqual(len(self.manager._sessions[first.page_session_id].cursors), 3)
        self.assert_error("PAGE_EXPIRED", lambda: self.next(second))
        self.assertEqual(self.next(first).items, (2, 3))

    def test_generation_changes_invalidate_without_reading_then_report_expired(self):
        first = self.first()
        reads = self.provider.reads
        self.provider.generation += 1
        error = self.assert_error("PAGE_SOURCE_CHANGED", lambda: self.next(first))
        self.assertTrue(error.retryable)
        self.assertEqual(self.provider.reads, reads)
        self.assert_error("PAGE_EXPIRED", lambda: self.next(first))

    def test_provider_changed_failures_invalidate_but_transient_failures_can_retry(self):
        first = self.first()
        self.provider.failure = "PAGE_BUDGET_EXCEEDED"
        self.assert_error("PAGE_BUDGET_EXCEEDED", lambda: self.next(first))
        self.provider.failure = None
        self.assertEqual(self.next(first).items, (2, 3))
        self.provider.failure = "PAGE_SOURCE_CHANGED"
        self.assert_error("PAGE_SOURCE_CHANGED", lambda: self.next(first))
        self.provider.failure = None
        self.assert_error("PAGE_EXPIRED", lambda: self.next(first))

    def test_failed_first_reads_or_mid_read_generation_changes_are_not_registered(self):
        self.provider.failure = "PAGE_SOURCE_UNAVAILABLE"
        self.assert_error("PAGE_SOURCE_UNAVAILABLE", self.first)
        self.assertFalse(self.manager._sessions)
        self.provider.failure = None
        self.provider.change_on_read = True
        self.assert_error("PAGE_SOURCE_CHANGED", self.first)
        self.assertFalse(self.manager._sessions)
        self.provider.change_on_read = False
        self.assertEqual(self.first().items, (0, 1))

    def test_request_validation_is_shared_and_runs_before_provider_work(self):
        for kwargs in (
            {"source_session_id": " "}, {"source_session_id": "a" * 257},
            {"source_session_id": []}, {"start_epoch": True},
            {"end_epoch_exclusive": 1.5}, {"start_epoch": 10, "end_epoch_exclusive": 10},
            {"order": []}, {"order": "ASC"}, {"limit": True}, {"limit": 0}, {"limit": 101},
        ):
            with self.subTest(kwargs=kwargs):
                self.assert_error("INVALID_ARGUMENT", lambda: self.first(**kwargs))
        self.assertEqual(self.provider.prepares, 0)
        self.assertEqual(self.provider.reads, 0)

    def test_explicit_invalidation_and_invalid_policies(self):
        first = self.first()
        self.manager.invalidate_session(first.page_session_id)
        self.assert_error("PAGE_EXPIRED", lambda: self.next(first))
        first = self.first()
        self.manager.invalidate_all()
        self.assert_error("PAGE_EXPIRED", lambda: self.next(first))
        for options in ({"ttl_seconds": -1}, {"max_sessions": 0},
                        {"max_tokens_per_session": True}, {"max_page_size": 0}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                PageSessionLimits(**options)


if __name__ == "__main__":
    unittest.main()
