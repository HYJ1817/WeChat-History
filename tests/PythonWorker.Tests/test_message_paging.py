"""B1 contract tests against the retained legacy reference, using synthetic DBs."""
from __future__ import annotations

import hashlib
import random
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from synthetic_messages import SyntheticSourceFactory, table_name
from wechat_memory import wechat4_source as adapter
from wechat_memory.message_reader import MessageReadError, SQLiteShardReader
import worker

STAMP = 1_795_478_400


class MessagePagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="synthetic-paging-")
        self.addCleanup(self.temp.cleanup)
        self.factory = SyntheticSourceFactory(Path(self.temp.name))

    def source(self):
        source = self.factory.source()
        self.addCleanup(source.close)
        return source

    def page(self, source, **overrides):
        return source.get_messages("c", start_epoch=None, end_epoch_exclusive=None, query=None,
                                   offset=0, limit=5, **overrides)

    def test_app_message_projection_is_consistent_across_ordinary_b2_and_b3_pages(self):
        quote = (
            '<msg><appmsg><type>57</type><title>needle quote</title><refermsg>'
            '<type>1</type><svrid>42</svrid><displayname>引用者</displayname>'
            '<content>引用摘要 needle</content></refermsg></appmsg></msg>'
        )
        entries = "".join(
            f'<dataitem datatype="1"><sourcename>成员{i}</sourcename>'
            f'<datadesc>needle forward {i}</datadesc></dataitem>'
            for i in range(205)
        )
        forward = (
            '<msg><appmsg><type>19</type><title>聊天记录</title><recorditem>'
            '<recordinfo><title>转发标题</title><datalist>' + entries +
            '</datalist></recordinfo></recorditem></appmsg></msg>'
        )
        self.factory.add_shard("cards.db", {"c": [
            {"create_time": STAMP, "local_type": 49, "content": quote},
            {"create_time": STAMP + 1, "local_type": 49, "content": forward},
        ]})
        source = self.source()
        context = worker.Context("unused")
        context.wechat = source
        worker._activate_source(context, "wechat4", force=True)

        ordinary = worker.handle_get_messages({
            "source": "wechat4", "conversationId": "c", "offset": 0, "limit": 2,
        }, context)
        quote_wire, forward_wire = ordinary["items"]
        self.assertEqual(quote_wire["content"], source.get_messages(
            "c", start_epoch=None, end_epoch_exclusive=None, query=None, offset=0, limit=1,
        ).items[0].content)
        self.assertEqual(
            {key: quote_wire[key] for key in ("displayKind", "replyTo", "quoteSummary", "quoteSender",
                                                "forwardItems", "parseStatus", "truncated", "omittedCount")},
            {"displayKind": "quote", "replyTo": "42", "quoteSummary": "引用摘要 needle",
             "quoteSender": "引用者", "forwardItems": [], "parseStatus": "ok",
             "truncated": False, "omittedCount": 0},
        )
        self.assertEqual((forward_wire["displayKind"], len(forward_wire["forwardItems"])), ("forward", 200))
        self.assertEqual((forward_wire["parseStatus"], forward_wire["truncated"], forward_wire["omittedCount"]),
                         ("partial", True, 5))
        self.assertEqual(forward_wire["forwardItems"][0]["content"], "needle forward 0")
        plain_page = source.get_messages("c", start_epoch=None, end_epoch_exclusive=None,
                                         query=None, offset=0, limit=2)
        self.assertFalse(hasattr(plain_page.items[0], "display_kind"))
        self.assertEqual(plain_page.items[0].reply_to, "42")

        global_result = worker.handle_search_messages({
            "source": "wechat4", "sourceSessionId": context.source_session_id, "query": "needle",
        }, context)
        self.assertEqual(global_result["coverage"]["truncatedMessages"], 1)
        global_forward = next(item for item in global_result["items"] if item["displayKind"] == "forward")
        self.assertEqual((len(global_forward["forwardItems"]), global_forward["omittedCount"]), (200, 5))
        global_context = worker.handle_get_message_context({
            "source": "wechat4", "sourceSessionId": context.source_session_id,
            "searchId": global_result["searchId"], "hitId": global_forward["hitId"], "before": 0, "after": 0,
        }, context)
        self.assertEqual(global_context["items"][0]["forwardItems"], global_forward["forwardItems"])

        opaque = adapter.parse_appmsg(
            '<msg><appmsg><type>19</type><recorditem><recordinfo><datalist>'
            '<dataitem datatype="17"><datatitle>opaque nested record</datatitle></dataitem>'
            '</datalist></recordinfo></recorditem></appmsg></msg>'
        )
        self.assertEqual(opaque.omitted_count, None)
        self.assertIsNone(adapter.app_message_projection_json(opaque)["omittedCount"])

        sid = context.source_session_id
        first = worker.handle_get_messages_page({
            "source": "wechat4", "sourceSessionId": sid, "conversationId": "c", "limit": 1,
        }, context)
        self.assertEqual(first["items"][0]["displayKind"], "forward")
        self.assertEqual(first["items"][0]["omittedCount"], 5)
        second = worker.handle_get_messages_page({
            "source": "wechat4", "sourceSessionId": sid, "pageSessionId": first["pageSessionId"],
            "cursor": first["nextCursor"], "limit": 1,
        }, context)
        self.assertEqual(second["items"][0]["displayKind"], "quote")
        self.assertEqual(second["items"][0]["replyTo"], "42")

        keyword = worker.handle_get_messages_page({
            "source": "wechat4", "sourceSessionId": sid, "conversationId": "c",
            "query": "needle", "order": "asc", "limit": 1,
        }, context)
        self.assertEqual(keyword["items"][0]["displayKind"], "quote")
        self.assertEqual(keyword["items"][0]["quoteSummary"], "引用摘要 needle")
        keyword_next = worker.handle_get_messages_page({
            "source": "wechat4", "sourceSessionId": sid, "pageSessionId": keyword["pageSessionId"],
            "cursor": keyword["nextCursor"], "limit": 1,
        }, context)
        self.assertEqual(keyword_next["items"][0]["forwardItems"], forward_wire["forwardItems"])
        self.assertEqual(keyword_next["items"][0]["omittedCount"], 5)

    def test_all_pages_match_legacy_for_interleaved_shards_and_millisecond_ties(self):
        rng = random.Random(702)
        for shard in range(4):
            scale = 1000 if shard % 2 else 1
            rows = []
            for index in range(30):
                # Duplicate local/server IDs across shards, but no exact raw
                # ties within a table. ms messages share their truncated second.
                stamp = (STAMP + index // 3) * scale + (index % 3 if scale == 1000 else 0)
                rows.append({"create_time": stamp, "local_id": index + 1 if scale == 1 else index % 3,
                             "server_id": 42, "content": f"shard={shard}, row={index}"})
            rng.shuffle(rows)
            self.factory.add_shard(f"message_{shard}.db", {"c": rows}, indexed=bool(shard % 2))
        source = self.source()
        for order in ("asc", "desc"):
            for offset in (0, 1, 17, 61, 115, 120, 1000):
                with self.subTest(order=order, offset=offset):
                    arguments = dict(start_epoch=STAMP + 2, end_epoch_exclusive=STAMP + 9,
                                     query=None, offset=offset, limit=7, order=order)
                    reference = source._get_messages_legacy("c", **arguments)
                    actual = source.get_messages("c", **arguments)
                    self.assertEqual(actual, reference)

    def test_only_final_page_is_converted_and_media_enriched(self):
        rows = [{"create_time": STAMP + index, "local_type": 3,
                 "content": "a" * 32, "local_id": index} for index in range(100)]
        self.factory.add_shard("message_0.db", {"c": rows})
        source = self.source()
        images = Mock()
        images.resolve.return_value = Path(self.temp.name) / "synthetic.png"
        source._images = images
        with patch.object(source, "_row_to_message", wraps=source._row_to_message) as project:
            page = source.get_messages("c", start_epoch=None, end_epoch_exclusive=None, query=None,
                                       offset=50, limit=7, order="desc")
        self.assertEqual(page.total, 100)
        self.assertEqual(page.next_offset, 57)
        self.assertEqual(project.call_count, 7)
        self.assertEqual(images.resolve.call_count, 7)
        self.assertEqual(source._databases.get_calls["message\\message_0.db"], 1)

    def test_voice_and_live_transcript_behavior_stays_legacy_for_keyword_matching(self):
        self.factory.add_shard("message_0.db", {"c": [
            {"create_time": STAMP + i, "local_type": 34,
             "content": '<msg><voicemsg voicelength="1000"/></msg>'} for i in range(12)
        ]})
        source = self.source()
        voices, transcripts = Mock(), Mock()
        voices.resolve.return_value = Path(self.temp.name) / "voice.silk"
        transcripts.load.return_value = "已有 needle 转写"
        source._voices, source._transcripts = voices, transcripts
        page = source.get_messages("c", start_epoch=None, end_epoch_exclusive=None, query="needle",
                                   offset=5, limit=3, order="desc")
        self.assertEqual(page.total, 12)
        self.assertEqual(len(page.items), 3)
        self.assertEqual(voices.resolve.call_count, 12)
        self.assertEqual(transcripts.load.call_count, 12)
        voices.reset_mock()
        transcripts.reset_mock()
        source.get_messages("c", start_epoch=None, end_epoch_exclusive=None, query=None,
                            offset=5, limit=3, order="desc")
        self.assertEqual(voices.resolve.call_count, 3)
        self.assertEqual(transcripts.load.call_count, 3)

    def test_duplicate_locations_are_deduplicated_not_duplicate_messages(self):
        self.factory.add_shard("message_0.db", {"c": [
            {"create_time": STAMP, "local_id": 1, "server_id": 7, "content": "same"},
            {"create_time": STAMP + 1, "local_id": 1, "server_id": 7, "content": "same"},
        ]})
        source = self.source()
        relative, table = source._locations["c"][0]
        source._locations["c"].extend([(relative, table), (relative.replace("\\", "/"), table)])
        page = self.page(source)
        self.assertEqual(page.total, 2)
        self.assertEqual([item.id for item in page.items], ["7", "7"])
        self.assertEqual(source._databases.get_calls[relative], 1)

    def test_ambiguous_exact_raw_ties_fall_back_without_inventing_identity_sort(self):
        self.factory.add_shard("message_0.db", {"c": [
            {"create_time": STAMP, "local_id": 1, "content": "first"},
            {"create_time": STAMP, "local_id": 1, "content": "second"},
        ]})
        source = self.source()
        with patch.object(source, "_get_messages_legacy", wraps=source._get_messages_legacy) as slow:
            page = self.page(source, order="desc")
        self.assertEqual([item.content for item in page.items], ["second", "first"])
        slow.assert_called_once()
        self.assertEqual(source._databases.get_calls["message\\message_0.db"], 1)

    def test_without_rowid_and_mixed_units_explicitly_use_reference_path(self):
        for name, rows, without_rowid in (
            ("without.db", [{"create_time": STAMP, "local_id": 1}], True),
            ("mixed.db", [{"create_time": STAMP, "local_id": 1},
                          {"create_time": (STAMP + 1) * 1000, "local_id": 2}], False),
        ):
            with self.subTest(name=name):
                factory = SyntheticSourceFactory(Path(self.temp.name))
                factory.add_shard(name, {"c": rows}, without_rowid=without_rowid)
                source = factory.source()
                self.addCleanup(source.close)
                with patch.object(source, "_get_messages_legacy", wraps=source._get_messages_legacy) as slow:
                    actual = self.page(source)
                self.assertEqual(actual, source._get_messages_legacy(
                    "c", start_epoch=None, end_epoch_exclusive=None, query=None, offset=0, limit=5))
                slow.assert_called_once()

    def test_join_fanout_never_uses_a_nonunique_physical_locator(self):
        path = self.factory.add_shard("message_0.db", {"c": [{"create_time": STAMP}]})
        connection = sqlite3.connect(path)
        connection.execute("DROP TABLE Name2Id")
        connection.execute("CREATE TABLE Name2Id (rowid INTEGER, user_name TEXT)")
        connection.executemany("INSERT INTO Name2Id VALUES (2, ?)", [("a",), ("b",)])
        connection.commit()
        connection.close()
        source = self.source()
        with patch.object(source, "_get_messages_legacy", wraps=source._get_messages_legacy) as slow:
            page = self.page(source)
        self.assertEqual(page.total, 2)
        slow.assert_called_once()

    def test_precise_cursor_reaches_deep_pages_without_the_legacy_candidate_cap(self):
        expected = []
        for shard in range(3):
            rows = []
            for index in range(411):
                content = f"cursor shard={shard} row={index}"
                rows.append({
                    "create_time": STAMP + index // 7 if shard != 1 else (STAMP + index // 7) * 1000,
                    "local_id": index % 5,
                    "server_id": 99,
                    "content": content,
                })
                expected.append(content)
            self.factory.add_shard(f"message_{shard}.db", {"c": rows})
        source = self.source()
        collected = []
        keyset_limits = []
        original_keyset = adapter.SQLiteShardReader.iter_keyset_rows

        def track_keyset(reader, *args, **kwargs):
            keyset_limits.append(kwargs["limit"])
            yield from original_keyset(reader, *args, **kwargs)

        with patch.object(adapter, "MAX_MESSAGES", 5), patch.object(
            adapter.SQLiteShardReader, "iter_keyset_rows", track_keyset,
        ):
            page = source.get_messages_page(
                "c", source_session_id="source-demo", start_epoch=None,
                end_epoch_exclusive=None, order="asc", limit=37,
            )
            page_count = 1
            collected.extend(item.content for item in page.items)
            while page.next_cursor:
                page = source.get_messages_cursor_page(
                    "source-demo", page.page_session_id, page.next_cursor, limit=37,
                )
                page_count += 1
                collected.extend(item.content for item in page.items)
        self.assertEqual(page.total, 1233)
        self.assertEqual(len(collected), 1233)
        self.assertCountEqual(collected, expected)
        self.assertEqual(page_count, (1233 + 36) // 37)
        self.assertEqual(len(keyset_limits), page_count * 3)
        self.assertEqual(set(keyset_limits), {38})

    def test_precise_cursor_orders_timestamp_ties_by_table_and_verified_rowid(self):
        for shard in (1, 0):
            self.factory.add_shard(f"message_{shard}.db", {"c": [
                {"create_time": STAMP, "local_id": 1, "server_id": 9,
                 "content": f"tie {shard}/{row}"}
                for row in range(4)
            ]})
        source = self.source()
        expected_ascending = [f"tie {shard}/{row}" for shard in (0, 1) for row in range(4)]
        first = source.get_messages_page(
            "c", source_session_id="source-ties", start_epoch=None,
            end_epoch_exclusive=None, order="asc", limit=3,
        )
        second = source.get_messages_cursor_page(
            "source-ties", first.page_session_id, first.next_cursor, limit=3,
        )
        back = source.get_messages_cursor_page(
            "source-ties", second.page_session_id, second.previous_cursor, limit=3,
        )
        self.assertEqual([item.content for item in first.items], expected_ascending[:3])
        self.assertEqual([item.content for item in second.items], expected_ascending[3:6])
        self.assertEqual([item.content for item in back.items], expected_ascending[:3])

        descending = source.get_messages_page(
            "c", source_session_id="source-desc", start_epoch=None,
            end_epoch_exclusive=None, order="desc", limit=20,
        )
        self.assertEqual([item.content for item in descending.items], list(reversed(expected_ascending)))

    def test_precise_cursor_interleaves_millisecond_and_microsecond_shards_exactly(self):
        self.factory.add_shard("message_ms.db", {"c": [
            {"create_time": STAMP * 1000 + 10, "content": "ms +10"},
            {"create_time": STAMP * 1000 + 300, "content": "ms +300"},
        ]})
        self.factory.add_shard("message_us.db", {"c": [
            {"create_time": STAMP * 1_000_000 + 250_000, "content": "us +250000"},
            {"create_time": STAMP * 1_000_000 + 500_000, "content": "us +500000"},
        ]})
        source = self.source()
        page = source.get_messages_page(
            "c", source_session_id="source-precision", start_epoch=None,
            end_epoch_exclusive=None, order="asc", limit=1,
        )
        contents = [page.items[0].content]
        while page.next_cursor:
            page = source.get_messages_cursor_page(
                "source-precision", page.page_session_id, page.next_cursor, limit=1,
            )
            contents.extend(item.content for item in page.items)
        self.assertEqual(contents, ["ms +10", "us +250000", "ms +300", "us +500000"])

    def test_cursor_generation_change_invalidates_instead_of_appending_new_rows(self):
        path = self.factory.add_shard("message_0.db", {"c": [
            {"create_time": STAMP + index, "content": f"row {index}"} for index in range(8)
        ]})
        source = self.source()
        page = source.get_messages_page(
            "c", source_session_id="source-change", start_epoch=None,
            end_epoch_exclusive=None, order="asc", limit=3,
        )
        connection = sqlite3.connect(path)
        connection.execute(
            f'INSERT INTO "{table_name("c")}" VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (100, STAMP + 100, 1, 2, "late row", 0, 0, None),
        )
        connection.commit()
        connection.close()
        with self.assertRaises(adapter.SourceFailure) as changed:
            source.get_messages_cursor_page(
                "source-change", page.page_session_id, page.next_cursor, limit=3,
            )
        self.assertEqual(changed.exception.code, "PAGE_SOURCE_CHANGED")

    def test_cursor_session_expires_and_unknown_cursor_is_rejected(self):
        self.factory.add_shard("message_0.db", {"c": [
            {"create_time": STAMP + index} for index in range(6)
        ]})
        source = self.source()
        page = source.get_messages_page(
            "c", source_session_id="source-expiry", start_epoch=None,
            end_epoch_exclusive=None, order="asc", limit=2,
        )
        with self.assertRaises(adapter.SourceFailure) as unknown:
            source.get_messages_cursor_page(
                "source-expiry", page.page_session_id, "not-a-cursor", limit=2,
            )
        self.assertEqual(unknown.exception.code, "PAGE_EXPIRED")
        with patch.object(adapter, "CURSOR_SESSION_TTL_SECONDS", 0):
            with self.assertRaises(adapter.SourceFailure) as expired:
                source.get_messages_cursor_page(
                    "source-expiry", page.page_session_id, page.next_cursor, limit=2,
                )
        self.assertEqual(expired.exception.code, "PAGE_EXPIRED")

    def test_cursor_rejects_missing_nonunique_and_mixed_schema_keys(self):
        cases = (
            ("without-rowid.db", [{"create_time": STAMP, "local_id": 1}], True),
            ("mixed-units.db", [
                {"create_time": STAMP}, {"create_time": (STAMP + 1) * 1000},
            ], False),
        )
        for filename, rows, without_rowid in cases:
            with self.subTest(filename=filename):
                factory = SyntheticSourceFactory(Path(self.temp.name) / filename.replace(".db", ""))
                factory.root.mkdir(parents=True, exist_ok=True)
                factory.add_shard(filename, {"c": rows}, without_rowid=without_rowid)
                source = factory.source()
                self.addCleanup(source.close)
                with self.assertRaises(adapter.SourceFailure) as unsupported:
                    source.get_messages_page(
                        "c", source_session_id="source-schema", start_epoch=None,
                        end_epoch_exclusive=None, order="asc", limit=2,
                    )
                self.assertEqual(unsupported.exception.code, "PAGE_UNSUPPORTED_SCHEMA")

        factory = SyntheticSourceFactory(Path(self.temp.name) / "shadowed-rowid")
        factory.root.mkdir(parents=True, exist_ok=True)
        path = factory.add_shard("shadow.db", {"c": [{"create_time": STAMP}]})
        connection = sqlite3.connect(path)
        for name in ("_rowid_", "rowid", "oid"):
            connection.execute(f'ALTER TABLE "{table_name("c")}" ADD COLUMN "{name}" INTEGER')
        connection.commit()
        connection.close()
        source = factory.source()
        self.addCleanup(source.close)
        with self.assertRaises(adapter.SourceFailure) as shadowed:
            source.get_messages_page(
                "c", source_session_id="source-shadowed", start_epoch=None,
                end_epoch_exclusive=None, order="asc", limit=2,
            )
        self.assertEqual(shadowed.exception.code, "PAGE_UNSUPPORTED_SCHEMA")

    def test_full_export_range_does_not_use_preview_page_size_or_search_top_k(self):
        self.factory.add_shard("message_0.db", {"c": [{"create_time": STAMP + i} for i in range(1234)]})
        source = self.source()
        context = worker.Context("unused")
        context.wechat = source
        _, messages = worker._real_export_models(context, {"conversationId": "c"})
        self.assertEqual(len(messages), 1234)
        self.assertEqual(source.get_messages("c", start_epoch=None, end_epoch_exclusive=None,
                                             query=None, offset=0, limit=50).total, 1234)

    def test_legacy_candidate_cap_counts_before_keyword_filter_and_is_inclusive(self):
        self.factory.add_shard("message_0.db", {"c": [{"create_time": STAMP + i} for i in range(6)]})
        source = self.source()
        with patch.object(adapter, "MAX_MESSAGES", 5):
            for query in (None, "no matching text"):
                with self.subTest(query=query), self.assertRaises(adapter.SourceFailure) as raised:
                    source.get_messages("c", start_epoch=None, end_epoch_exclusive=None,
                                        query=query, offset=0, limit=1)
                self.assertEqual(raised.exception.code, "RESULT_LIMIT_EXCEEDED")
            page = source.get_messages("c", start_epoch=None, end_epoch_exclusive=STAMP + 5,
                                       query=None, offset=0, limit=10)
            self.assertEqual(page.total, 5)

    def test_database_bytes_and_mtime_are_unchanged(self):
        path = self.factory.add_shard("message_0.db", {"c": [{"create_time": STAMP + i} for i in range(10)]})
        before = hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns
        source = self.source()
        self.page(source)
        self.assertEqual((hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns), before)

    def test_reader_sql_deadline_is_cooperative_and_connection_can_be_closed(self):
        path = self.factory.add_shard("message_0.db", {"c": [{"create_time": STAMP}]})
        with SQLiteShardReader(path, deadline=time.monotonic() - 1) as reader:
            with self.assertRaises(MessageReadError) as raised:
                reader.inspect_table(table_name("c"))
            self.assertEqual(raised.exception.code, "SEARCH_BUDGET_EXCEEDED")
        # Closing and opening again is not poisoned by the interrupted query.
        with SQLiteShardReader(path) as reader:
            self.assertEqual(reader.count_rows(table_name("c"), reader.inspect_table(table_name("c")), None, None), 1)


if __name__ == "__main__":
    unittest.main()
