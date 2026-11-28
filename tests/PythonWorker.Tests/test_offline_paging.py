"""Parity tests for shared live/offline opaque page-session lifecycle."""
from __future__ import annotations

import gc
import json
import sqlite3
import sys
import tempfile
import unittest
import weakref
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
WORKER_DIR = ROOT / "src" / "PythonWorker"
for directory in (WORKER_DIR / "vendor", WORKER_DIR):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from synthetic_messages import SyntheticSourceFactory, table_name
from wechat_memory import offline_import
from wechat_memory import wechat4_source as live_source
from wechat_memory.offline_import import (
    ImportedJson,
    JsonConversation,
    JsonMessage,
    OfflineJsonPageProvider,
)
from wechat_memory.page_sessions import PageSessionLimits

BASE = datetime(2026, 11, 28, 12, tzinfo=timezone.utc)
OFFSETS = (0, 250, 250, 800, 1_000_000, 1_002_000)


def imported_snapshot(conversation_id: str = "c", *, shift_seconds: int = 0) -> ImportedJson:
    messages = tuple(
        JsonMessage(
            id=f"m-{index}",
            sender="sender",
            timestamp=BASE + timedelta(seconds=shift_seconds, microseconds=offset),
            kind="text",
            content=f"message {index}",
        )
        for index, offset in enumerate(OFFSETS)
    )
    conversation = JsonConversation(conversation_id, "offline conversation", "direct", messages)
    return ImportedJson((conversation,), "wechat-memory-json-v1", len(messages))


class OfflinePagingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="offline-paging-")
        self.addCleanup(self.temp.cleanup)
        self.factory = SyntheticSourceFactory(Path(self.temp.name))
        stamp = int(BASE.timestamp())
        self.factory.add_shard("message_0.db", {"c": [
            {"create_time": stamp * 1_000_000 + offset, "local_id": index + 1, "content": f"message {index}"}
            for index, offset in enumerate(OFFSETS)
        ]})
        self.live = self.factory.source()
        self.addCleanup(self.live.close)
        self.snapshot = imported_snapshot()
        self.provider = OfflineJsonPageProvider(self.snapshot)
        self.addCleanup(self.provider.close)

    def test_precise_timestamp_order_total_and_back_navigation(self) -> None:
        self.assertIs(self.provider.snapshot, self.snapshot)
        page1 = self.provider.get_messages_page(
            "c", source_session_id="offline-session", start_epoch=None,
            end_epoch_exclusive=None, order="asc", limit=2,
        )
        self.assertEqual(page1.total, 6)
        self.assertEqual([item.id for item in page1.items], ["m-0", "m-1"])
        self.assertEqual(page1.sort_version, "precise-v1")

        page2 = self.provider.get_messages_cursor_page(
            "offline-session", page1.page_session_id, page1.next_cursor, limit=2,
        )
        self.assertEqual([item.id for item in page2.items], ["m-2", "m-3"])
        self.assertEqual([item.id for item in self.provider.get_messages_cursor_page(
            "offline-session", page2.page_session_id, page2.previous_cursor, limit=2,
        ).items], ["m-0", "m-1"])

        descending = self.provider.get_messages_page(
            "c", source_session_id="offline-desc", start_epoch=None,
            end_epoch_exclusive=None, order="desc", limit=10,
        )
        self.assertEqual([item.id for item in descending.items], ["m-5", "m-4", "m-3", "m-2", "m-1", "m-0"])
        self.assertEqual(descending.total, 6)

    def test_range_total_is_exact_and_pages_match_the_live_cursor_shape(self) -> None:
        start = int(BASE.timestamp())
        page = self.provider.get_messages_page(
            "c", source_session_id="filtered", start_epoch=start,
            end_epoch_exclusive=start + 1, order="asc", limit=3,
        )
        self.assertEqual(page.total, 4)
        self.assertEqual([item.id for item in page.items], ["m-0", "m-1", "m-2"])
        self.assertIsNotNone(page.next_cursor)
        self.assertIsNone(page.previous_cursor)
        live_page = self.live.get_messages_page(
            "c", source_session_id="live-filtered", start_epoch=start,
            end_epoch_exclusive=start + 1, order="asc", limit=3,
        )
        self.assertEqual(live_page.total, page.total)
        self.assertEqual(live_page.sort_version, page.sort_version)
        self.assertEqual(bool(live_page.next_cursor), bool(page.next_cursor))

    def test_unknown_and_expired_tokens_match_live_error_codes(self) -> None:
        offline_page = self.provider.get_messages_page(
            "c", source_session_id="offline-errors", start_epoch=None,
            end_epoch_exclusive=None, order="asc", limit=2,
        )
        live_page = self.live.get_messages_page(
            "c", source_session_id="live-errors", start_epoch=None,
            end_epoch_exclusive=None, order="asc", limit=2,
        )
        with self.assertRaises(offline_import.PageSessionError) as offline_unknown:
            self.provider.get_messages_cursor_page(
                "offline-errors", offline_page.page_session_id, "unknown", limit=2,
            )
        with self.assertRaises(live_source.SourceFailure) as live_unknown:
            self.live.get_messages_cursor_page(
                "live-errors", live_page.page_session_id, "unknown", limit=2,
            )
        self.assertEqual(offline_unknown.exception.code, live_unknown.exception.code)
        self.assertEqual(offline_unknown.exception.code, "PAGE_EXPIRED")

        offline_page = self.provider.get_messages_page(
            "c", source_session_id="offline-expiry", start_epoch=None,
            end_epoch_exclusive=None, order="asc", limit=2,
        )
        live_page = self.live.get_messages_page(
            "c", source_session_id="live-expiry", start_epoch=None,
            end_epoch_exclusive=None, order="asc", limit=2,
        )
        with patch.object(self.provider._page_sessions, "limits", PageSessionLimits(ttl_seconds=0)), patch.object(
            live_source, "CURSOR_SESSION_TTL_SECONDS", 0,
        ):
            with self.assertRaises(offline_import.PageSessionError) as offline_expired:
                self.provider.get_messages_cursor_page(
                    "offline-expiry", offline_page.page_session_id, offline_page.next_cursor, limit=2,
                )
            with self.assertRaises(live_source.SourceFailure) as live_expired:
                self.live.get_messages_cursor_page(
                    "live-expiry", live_page.page_session_id, live_page.next_cursor, limit=2,
                )
        self.assertEqual(offline_expired.exception.code, live_expired.exception.code)
        self.assertEqual(offline_expired.exception.code, "PAGE_EXPIRED")

    def test_reimport_and_source_switch_advance_generation_and_expire_cursor_as_changed(self) -> None:
        page = self.provider.get_messages_page(
            "c", source_session_id="offline-generation", start_epoch=None,
            end_epoch_exclusive=None, order="asc", limit=2,
        )
        previous_generation = self.provider.generation
        self.provider.replace_snapshot(imported_snapshot(shift_seconds=10))
        self.assertEqual(self.provider.generation, previous_generation + 1)
        with self.assertRaises(offline_import.PageSessionError) as reimported:
            self.provider.get_messages_cursor_page(
                "offline-generation", page.page_session_id, page.next_cursor, limit=2,
            )
        self.assertEqual(reimported.exception.code, "PAGE_SOURCE_CHANGED")

        page = self.provider.get_messages_page(
            "c", source_session_id="offline-switch", start_epoch=None,
            end_epoch_exclusive=None, order="asc", limit=2,
        )
        self.provider.invalidate()
        with self.assertRaises(offline_import.PageSessionError) as switched:
            self.provider.get_messages_cursor_page(
                "offline-switch", page.page_session_id, page.next_cursor, limit=2,
            )
        self.assertEqual(switched.exception.code, "PAGE_SOURCE_CHANGED")

    def test_provider_keeps_only_the_active_snapshot_reference(self) -> None:
        original = imported_snapshot()
        provider = OfflineJsonPageProvider(original)
        self.assertIs(provider.snapshot, original)
        replacement = imported_snapshot(shift_seconds=4)
        provider.replace_snapshot(replacement)
        self.assertIs(provider.snapshot, replacement)
        self.assertEqual(provider.generation, 1)
        provider.close()

    def test_all_pages_and_back_navigation_match_live_for_both_orders_and_ranges(self) -> None:
        epoch = int(BASE.timestamp())
        for order in ("asc", "desc"):
            for start, end in ((None, None), (epoch, epoch + 1), (epoch + 1, epoch + 2), (epoch + 4, None)):
                with self.subTest(order=order, start=start, end=end):
                    pages = [source.get_messages_page(
                        "c", source_session_id="parity", start_epoch=start,
                        end_epoch_exclusive=end, order=order, limit=2,
                    ) for source in (self.live, self.provider)]
                    collected = []
                    while True:
                        self.assertEqual(pages[0].total, pages[1].total)
                        self.assertEqual(bool(pages[0].previous_cursor), bool(pages[1].previous_cursor))
                        self.assertEqual(bool(pages[0].next_cursor), bool(pages[1].next_cursor))
                        self.assertEqual(
                            [(item.content, item.timestamp) for item in pages[0].items],
                            [(item.content, item.timestamp) for item in pages[1].items],
                        )
                        collected.extend(item.content for item in pages[1].items)
                        if pages[0].previous_cursor:
                            back = [source.get_messages_cursor_page(
                                "parity", page.page_session_id, page.previous_cursor, limit=1,
                            ) for source, page in zip((self.live, self.provider), pages)]
                            self.assertEqual(back[0].items[0].content, back[1].items[0].content)
                        if not pages[0].next_cursor:
                            break
                        pages = [source.get_messages_cursor_page(
                            "parity", page.page_session_id, page.next_cursor, limit=2,
                        ) for source, page in zip((self.live, self.provider), pages)]
                    self.assertEqual(len(collected), pages[0].total)
                    self.assertEqual(len(collected), len(set(collected)))

    def test_page_validation_and_source_binding_have_live_offline_parity(self) -> None:
        for arguments in (
            {"conversation_id": "unknown"}, {"conversation_id": []},
            {"source_session_id": []}, {"source_session_id": " "},
            {"start_epoch": True}, {"end_epoch_exclusive": False},
            {"start_epoch": 2, "end_epoch_exclusive": 1},
            {"limit": True}, {"limit": 0}, {"limit": 101}, {"order": []},
        ):
            with self.subTest(arguments=arguments):
                codes = []
                for source in (self.live, self.provider):
                    request = dict(conversation_id="c", source_session_id="validation",
                                   start_epoch=None, end_epoch_exclusive=None, limit=2)
                    request.update(arguments)
                    with self.assertRaises(offline_import.PageSessionError) as raised:
                        source.get_messages_page(**request)
                    codes.append(raised.exception.code)
                self.assertEqual(codes[0], codes[1])
        for source in (self.live, self.provider):
            page = source.get_messages_page(
                "c", source_session_id="bound-source", start_epoch=None,
                end_epoch_exclusive=None, limit=2,
            )
            with self.assertRaises(offline_import.PageSessionError) as raised:
                source.get_messages_cursor_page("another-source", page.page_session_id, page.next_cursor, limit=2)
            self.assertEqual(raised.exception.code, "PAGE_EXPIRED")

    def test_live_mutation_and_offline_reimport_share_changed_then_expired_semantics(self) -> None:
        pages = [source.get_messages_page(
            "c", source_session_id="changed", start_epoch=None,
            end_epoch_exclusive=None, limit=2,
        ) for source in (self.live, self.provider)]
        path = next(iter(self.factory.paths.values()))
        with closing(sqlite3.connect(path)) as connection:
            connection.execute(f'UPDATE "{table_name("c")}" SET message_content=? WHERE rowid=1', ("new content",))
            connection.commit()
        self.provider.replace_snapshot(self.snapshot)  # even re-importing identical data is a new generation
        for source, page in zip((self.live, self.provider), pages):
            for expected in ("PAGE_SOURCE_CHANGED", "PAGE_EXPIRED"):
                with self.assertRaises(offline_import.PageSessionError) as raised:
                    source.get_messages_cursor_page("changed", page.page_session_id, page.next_cursor, limit=2)
                self.assertEqual(raised.exception.code, expected)
                self.assertTrue(raised.exception.retryable)

    def test_live_and_offline_session_and_token_caps_use_the_shared_lru(self) -> None:
        self.provider._page_sessions.limits = PageSessionLimits(max_sessions=2, max_tokens_per_session=3)
        with patch.object(live_source, "MAX_CURSOR_SESSIONS", 2), patch.object(
            live_source, "MAX_CURSOR_TOKENS_PER_SESSION", 3,
        ):
            for source in (self.live, self.provider):
                first = source.get_messages_page("c", source_session_id="bounded", start_epoch=None,
                                                 end_epoch_exclusive=None, order="asc", limit=1)
                second = source.get_messages_cursor_page("bounded", first.page_session_id, first.next_cursor, limit=1)
                source.get_messages_cursor_page("bounded", first.page_session_id, first.next_cursor, limit=1)
                with self.assertRaises(offline_import.PageSessionError) as evicted_token:
                    source.get_messages_cursor_page("bounded", second.page_session_id, second.next_cursor, limit=1)
                self.assertEqual(evicted_token.exception.code, "PAGE_EXPIRED")
                for _ in range(2):
                    source.get_messages_page("c", source_session_id="bounded", start_epoch=None,
                                             end_epoch_exclusive=None, order="asc", limit=1)
                with self.assertRaises(offline_import.PageSessionError) as evicted_session:
                    source.get_messages_cursor_page("bounded", first.page_session_id, first.next_cursor, limit=1)
                self.assertEqual(evicted_session.exception.code, "PAGE_EXPIRED")

    def test_empty_conversation_and_range_return_exact_zero_without_cursors(self) -> None:
        snapshot = ImportedJson((JsonConversation("empty", "Empty", "direct", ()),), "wechat-memory-json-v1", 0)
        provider = OfflineJsonPageProvider(snapshot)
        self.addCleanup(provider.close)
        for order in ("asc", "desc"):
            page = provider.get_messages_page("empty", source_session_id="empty", start_epoch=None,
                                              end_epoch_exclusive=None, order=order, limit=2)
            self.assertEqual(page.items, ())
            self.assertEqual(page.total, 0)
            self.assertIsNone(page.previous_cursor)
            self.assertIsNone(page.next_cursor)

    def test_snapshot_is_not_retained_by_old_sessions_after_reimport(self) -> None:
        class WeakImportedJson(ImportedJson):
            __slots__ = ("__weakref__",)

        base = imported_snapshot()
        original = WeakImportedJson(base.conversations, base.schema, base.message_count)
        reference = weakref.ref(original)
        provider = OfflineJsonPageProvider(original)
        self.addCleanup(provider.close)
        page = provider.get_messages_page("c", source_session_id="references", start_epoch=None,
                                          end_epoch_exclusive=None, order="asc", limit=2)
        self.assertIs(provider.snapshot, original)
        self.assertIs(page.items[0], original.conversations[0].messages[0])
        del original
        provider.replace_snapshot(imported_snapshot(shift_seconds=20))
        gc.collect()
        self.assertIsNone(reference())
        self.assertTrue(provider._page_sessions._sessions)  # metadata still retained to report SOURCE_CHANGED
        for session in provider._page_sessions._sessions.values():
            self.assertEqual(set(session.state.__slots__), {"conversation_id", "lower", "upper", "order"})
        with self.assertRaises(offline_import.PageSessionError) as raised:
            provider.get_messages_cursor_page("references", page.page_session_id, page.next_cursor)
        self.assertEqual(raised.exception.code, "PAGE_SOURCE_CHANGED")

    def test_imported_file_can_disappear_and_media_references_trigger_no_io(self) -> None:
        path = Path(self.temp.name) / "snapshot.json"
        references = ("https://not-used.invalid/image.png", r"C:\not-used\file.txt", r"\\not-used\share\photo.png")
        path.write_text(json.dumps({"version": 1, "conversations": [{
            "id": "c", "name": "Synthetic", "messages": [
                {"id": str(index), "sender": "sender", "timestamp": BASE.isoformat(),
                 "type": "image", "content": content}
                for index, content in enumerate(references)
            ],
        }]}), encoding="utf-8")
        snapshot = offline_import.load_json_file(path)
        path.unlink()
        provider = OfflineJsonPageProvider(snapshot)
        self.addCleanup(provider.close)
        with patch("builtins.open", side_effect=AssertionError("unexpected file read")), patch.object(
            Path, "open", side_effect=AssertionError("unexpected path read"),
        ), patch("socket.socket", side_effect=AssertionError("unexpected network access")), patch(
            "sqlite3.connect", side_effect=AssertionError("unexpected SQLite connection"),
        ):
            first = provider.get_messages_page("c", source_session_id="no-io", start_epoch=None,
                                               end_epoch_exclusive=None, order="asc", limit=2)
            second = provider.get_messages_cursor_page("no-io", first.page_session_id, first.next_cursor, limit=2)
        self.assertEqual([item.content for item in first.items + second.items], list(references))
        self.assertIs(first.items[0], snapshot.conversations[0].messages[0])
        self.assertEqual(second.total, 3)

    def test_precise_extreme_datetime_range_and_outside_epoch_bounds(self) -> None:
        timestamps = (
            datetime(1, 1, 1, tzinfo=timezone.utc),
            datetime(1969, 12, 31, 23, 59, 59, 999999, tzinfo=timezone.utc),
            datetime(9999, 12, 31, 12, 0, 0, 999999, tzinfo=timezone.utc),
        )
        messages = tuple(JsonMessage(str(index), "s", timestamp, "text", str(index))
                         for index, timestamp in enumerate(timestamps))
        provider = OfflineJsonPageProvider(ImportedJson(
            (JsonConversation("c", "Synthetic", "direct", messages),), "wechat-memory-json-v1", len(messages),
        ))
        self.addCleanup(provider.close)
        all_messages = provider.get_messages_page("c", source_session_id="extreme", start_epoch=-(10**30),
                                                  end_epoch_exclusive=10**30, order="asc", limit=10)
        self.assertEqual(all_messages.items, messages)
        pre_epoch = provider.get_messages_page("c", source_session_id="pre-epoch", start_epoch=None,
                                               end_epoch_exclusive=0, order="asc", limit=10)
        self.assertEqual(pre_epoch.items, messages[:2])
        empty = provider.get_messages_page("c", source_session_id="outside", start_epoch=10**30,
                                           end_epoch_exclusive=None, order="desc", limit=10)
        self.assertEqual(empty.total, 0)
        self.assertEqual(empty.items, ())

    def test_failed_snapshot_replacement_preserves_generation_and_existing_cursor(self) -> None:
        first = self.provider.get_messages_page("c", source_session_id="valid", start_epoch=None,
                                                end_epoch_exclusive=None, order="asc", limit=2)
        generation = self.provider.generation
        with self.assertRaises(TypeError):
            self.provider.replace_snapshot(None)
        self.assertEqual(self.provider.generation, generation)
        self.assertIs(self.provider.snapshot, self.snapshot)
        second = self.provider.get_messages_cursor_page("valid", first.page_session_id, first.next_cursor, limit=2)
        self.assertEqual([item.id for item in second.items], ["m-2", "m-3"])


if __name__ == "__main__":
    unittest.main()
