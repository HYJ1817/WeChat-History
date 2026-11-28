"""Shared M0 snapshot/generation, schema identity and bounded-read contracts."""
from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from synthetic_messages import SyntheticSourceFactory, table_name
from wechat_context_exporter.sources.wechat4_crypto import DecryptedDatabaseCache, _database_signature
from wechat_memory.message_reader import MessageReadError, SQLiteShardReader, epoch_microseconds, legacy_timestamp_seconds


class SnapshotGenerationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="synthetic-generation-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.original = self.root / "original"
        self.original.mkdir()
        (self.original / "message").mkdir()
        self.source = self.original / "message/message_0.db"
        self.source.write_bytes(b"synthetic database bytes")
        self.cache = DecryptedDatabaseCache(self.original, keys={"message\\message_0.db": b"0" * 32},
                                            temp_root=self.root / "private")
        self.addCleanup(self.cache.close)
        # Test only controlled-copy/cache semantics, not encryption algorithms;
        # the fixture bytes and key never belong to a real account.
        def synthetic_decrypt(source, destination, _key):
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        self.decrypt = patch("wechat_context_exporter.sources.wechat4_crypto.decrypt_database",
                             side_effect=synthetic_decrypt)
        self.decrypt.start()
        self.addCleanup(self.decrypt.stop)

    def snapshot(self):
        return self.cache.get_snapshot("message\\message_0.db")

    def test_successful_snapshot_versions_are_reused_and_db_wal_updates_invalidate(self):
        first = self.snapshot()
        self.assertEqual(self.snapshot(), first)
        self.assertTrue(self.cache.is_snapshot_current("message\\message_0.db", first))
        wal = Path(str(self.source) + "-wal")
        wal.write_bytes(b"synthetic wal")
        self.assertFalse(self.cache.is_snapshot_current("message\\message_0.db", first))
        second = self.snapshot()
        self.assertGreater(second.generation, first.generation)
        self.assertNotEqual(second.signature, first.signature)
        self.assertEqual(second.path, first.path)  # a path is not a generation
        self.source.write_bytes(b"changed synthetic database with a different size")
        self.assertFalse(self.cache.is_snapshot_current("message\\message_0.db", second))
        third = self.snapshot()
        self.assertGreater(third.generation, second.generation)

    def test_signature_belongs_to_the_successful_copy_not_the_earlier_cache_probe(self):
        original_copy = self.cache._snapshot_database
        def update_before_snapshot(source, normalized):
            self.source.write_bytes(b"synthetic write after initial signature probe")
            return original_copy(source, normalized)
        with patch.object(self.cache, "_snapshot_database", side_effect=update_before_snapshot):
            snapshot = self.snapshot()
        self.assertEqual(snapshot.signature, _database_signature(self.source))
        self.assertEqual(snapshot.path.read_bytes(), self.source.read_bytes())
        self.assertTrue(self.cache.is_snapshot_current("message\\message_0.db", snapshot))

    def test_recreated_private_copy_gets_new_generation_even_for_unchanged_original(self):
        first = self.snapshot()
        first.path.unlink()
        self.assertFalse(self.cache.is_snapshot_current("message\\message_0.db", first))
        second = self.snapshot()
        self.assertNotEqual(second.generation, first.generation)
        self.assertEqual(second.signature, first.signature)
        self.assertFalse(self.cache.is_snapshot_current("message\\message_0.db", first))
        self.assertTrue(self.cache.is_snapshot_current("message\\message_0.db", second))


class ReaderContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="synthetic-reader-")
        self.addCleanup(self.temp.cleanup)
        self.factory = SyntheticSourceFactory(Path(self.temp.name))

    def test_two_sort_contracts_are_intentionally_different_and_integer_exact(self):
        stamp = 1795478400
        self.assertEqual(legacy_timestamp_seconds(stamp * 1000 + 999), stamp)
        self.assertEqual(epoch_microseconds(stamp * 1000 + 999), stamp * 1_000_000 + 999000)
        self.assertEqual(epoch_microseconds(stamp * 1_000_000 + 999001), stamp * 1_000_000 + 999001)

    def test_unique_rowid_is_verified_without_assuming_local_or_server_ids_are_unique(self):
        path = self.factory.add_shard("identity.db", {"c": [
            {"create_time": 1795478400, "local_id": 1, "server_id": 1},
            {"create_time": 1795478400, "local_id": 1, "server_id": 1},
        ]})
        with SQLiteShardReader(path) as reader:
            schema = reader.inspect_table(table_name("c"))
            self.assertIsNotNone(schema.rowid_column)
            self.assertFalse(schema.legacy_fast_path_supported)  # ambiguous legacy raw tie
            rows = list(reader.iter_rows(table_name("c"), schema, None, None, precise=True))
            self.assertEqual(len({row["physical_rowid"] for row in rows}), 2)

    def test_shadowed_rowid_aliases_and_without_rowid_do_not_become_string_literals(self):
        path = self.factory.add_shard("without.db", {"c": [{"create_time": 1795478400}]}, without_rowid=True)
        with SQLiteShardReader(path) as reader:
            self.assertIsNone(reader.inspect_table(table_name("c")).rowid_column)
        path = self.factory.add_shard("shadowed.db", {"c": [{"create_time": 1795478400}]})
        connection = sqlite3.connect(path)
        for name in ("_rowid_", "rowid", "oid"):
            connection.execute(f'ALTER TABLE "{table_name("c")}" ADD COLUMN "{name}" INTEGER')
        connection.commit()
        connection.close()
        with SQLiteShardReader(path) as reader:
            schema = reader.inspect_table(table_name("c"))
            self.assertIsNone(schema.rowid_column)
            with self.assertRaises(MessageReadError) as unsupported:
                list(reader.iter_rows(table_name("c"), schema, None, None, precise=True, physical_rowid=1))
            self.assertEqual(unsupported.exception.code, "SEARCH_UNSUPPORTED_SCHEMA")

    def test_oversize_content_is_flagged_in_sql_not_materialized_or_silently_skipped(self):
        path = self.factory.add_shard("large.db", {"c": [{"create_time": 1795478400, "content": b"x" * (2 * 1024 * 1024 + 1)}]})
        with SQLiteShardReader(path) as reader:
            schema = reader.inspect_table(table_name("c"))
            self.assertEqual(len(list(reader.iter_rows(table_name("c"), schema, None, None, metadata_only=True))), 1)
            with self.assertRaises(MessageReadError) as oversize:
                list(reader.iter_rows(table_name("c"), schema, None, None))
            self.assertEqual(oversize.exception.code, "MESSAGE_TOO_LARGE")

    def test_table_name_is_quoted_and_checked_not_a_client_sql_fragment(self):
        path = self.factory.add_shard("quotes.db", {"c": [{"create_time": 1795478400}]})
        with SQLiteShardReader(path) as reader:
            with self.assertRaises(MessageReadError):
                reader.inspect_table('x"; DROP TABLE Name2Id; --')
            self.assertEqual(reader.connection.execute("SELECT COUNT(*) FROM Name2Id").fetchone()[0], 3)

    def test_count_and_candidates_observe_the_same_shard_read_transaction(self):
        path = self.factory.add_shard("transaction.db", {"c": [{"create_time": 1795478400}]})
        writer = sqlite3.connect(path)
        writer.execute("PRAGMA journal_mode=WAL")
        self.addCleanup(writer.close)
        with SQLiteShardReader(path) as reader:
            schema = reader.inspect_table(table_name("c"))
            self.assertEqual(reader.count_rows(table_name("c"), schema, None, None), 1)
            writer.execute(f'INSERT INTO "{table_name("c")}" VALUES (2, 1795478401, 1, 2, ?, 0, 0, NULL)', ("new message",))
            writer.commit()
            candidates = list(reader.iter_rows(table_name("c"), schema, None, None,
                                               legacy_order="asc", limit=10, metadata_only=True))
            self.assertEqual(len(candidates), 1)
        with SQLiteShardReader(path) as reader:
            self.assertEqual(reader.count_rows(table_name("c"), reader.inspect_table(table_name("c")), None, None), 2)


if __name__ == "__main__":
    unittest.main()
