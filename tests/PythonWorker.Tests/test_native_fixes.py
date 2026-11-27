"""Regression tests for path encoding, temp leases, timestamps and delivery.

All databases, directories and conversations here are synthetic and live under
a TemporaryDirectory. No WeChat process or real chat database is touched.
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKER_DIR = ROOT / "src" / "PythonWorker"
VENDOR_DIR = WORKER_DIR / "vendor"
for _path in (WORKER_DIR, VENDOR_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from wechat_context_exporter.jobs import JobStatus, JobStore, utc_now  # noqa: E402
from wechat_context_exporter.jobs.models import display_date  # noqa: E402
from wechat_context_exporter.models import (  # noqa: E402
    Conversation,
    ConversationKind,
    Message,
    MessageType,
)
from wechat_context_exporter.sources.base import sqlite_read_only_uri  # noqa: E402
from wechat_context_exporter.sources.wechat4_media import DecryptedImageCache  # noqa: E402
from wechat_memory.private_temp import (  # noqa: E402
    ROOT_LEASE_NAME,
    claim_lease,
    cleanup_abandoned_roots,
    release_lease,
)
import worker  # noqa: E402
from wechat_memory.tasks import TaskService  # noqa: E402
from wechat_memory.wechat4_source import PrivateTempRoot, WeChat4Source  # noqa: E402


class _FakeDatabases:
    def __init__(self, path: Path) -> None:
        self._path = path

    def get(self, _name: str) -> Path:
        return self._path


class SqlitePathTests(unittest.TestCase):
    def test_hash_and_percent_in_directory_still_open_the_same_file(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wechat#mem%23-") as temp:
            path = Path(temp) / "sample.db"
            connection = sqlite3.connect(path)
            try:
                connection.execute("CREATE TABLE t (value TEXT)")
                connection.execute("INSERT INTO t VALUES ('expected')")
                connection.commit()
            finally:
                connection.close()

            uri = sqlite_read_only_uri(path)
            self.assertIn("%23", uri)
            self.assertIn("%2523", uri)  # the literal '%' is escaped too
            connection = sqlite3.connect(uri, uri=True)
            try:
                self.assertEqual(connection.execute("SELECT value FROM t").fetchone()[0], "expected")
            finally:
                connection.close()


class TimestampScaleTests(unittest.TestCase):
    def _source_with_table(self, times: list[int]) -> tuple[WeChat4Source, Path, Conversation]:
        temp = tempfile.TemporaryDirectory(prefix="wechat-ms-")
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "message_0.db"
        connection = sqlite3.connect(path)
        try:
            connection.execute(
                "CREATE TABLE Name2Id (user_name TEXT)"
            )
            connection.execute("INSERT INTO Name2Id (rowid, user_name) VALUES (1, 'wxid_self')")
            connection.execute(
                "CREATE TABLE Msg_test (local_id INTEGER, create_time INTEGER, local_type INTEGER, "
                "real_sender_id INTEGER, message_content TEXT, server_id INTEGER)"
            )
            connection.executemany(
                "INSERT INTO Msg_test VALUES (?, ?, 1, 1, ?, 0)",
                [(index, stamp, f"合成消息 {index}") for index, stamp in enumerate(times)],
            )
            connection.commit()
        finally:
            connection.close()
        source = WeChat4Source()
        source._databases = _FakeDatabases(path)  # type: ignore[assignment]
        source._own_wxid = "wxid_self"
        conversation = Conversation("wxid_friend", "虚构会话", ConversationKind.DIRECT)
        return source, path, conversation

    def test_millisecond_column_is_filtered_before_conversion(self) -> None:
        day = datetime(2026, 11, 24, tzinfo=timezone.utc)
        start = int(day.timestamp())
        end = start + 86_400
        stamps = [
            (start - 60) * 1000,
            (start + 3600) * 1000,
            (end - 1) * 1000,
            end * 1000,
        ]
        source, path, conversation = self._source_with_table(stamps)
        rows = source._read_table(conversation, "message_0.db", "Msg_test", path, start, end)
        self.assertEqual([row.message.content for row in rows], ["合成消息 1", "合成消息 2"])
        self.assertEqual(int(rows[0].message.timestamp.timestamp()), start + 3600)

    def test_second_column_still_uses_the_same_boundaries(self) -> None:
        day = datetime(2026, 11, 24, tzinfo=timezone.utc)
        start = int(day.timestamp())
        end = start + 86_400
        stamps = [start - 1, start, end - 1, end]
        source, path, conversation = self._source_with_table(stamps)
        rows = source._read_table(conversation, "message_0.db", "Msg_test", path, start, end)
        self.assertEqual([row.message.content for row in rows], ["合成消息 1", "合成消息 2"])


class PrivateTempLeaseTests(unittest.TestCase):
    def test_active_session_survives_cleanup_and_closed_session_is_removed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wechat-lease-") as temp:
            base = Path(temp) / "worker-temp"
            first = PrivateTempRoot(base=base)
            first_path = first.open()
            second = PrivateTempRoot(base=base)
            second_path = second.open()
            try:
                self.assertTrue(first_path.is_dir())
                third = PrivateTempRoot(base=base)
                third_path = third.open()
                try:
                    # Both live sessions still exist; only the closed one goes.
                    self.assertTrue(first_path.is_dir())
                    self.assertTrue(second_path.is_dir())
                    first.close()
                    fourth = PrivateTempRoot(base=base)
                    fourth_path = fourth.open()
                    try:
                        self.assertFalse(first_path.exists())
                        self.assertTrue(second_path.is_dir())
                        self.assertTrue(fourth_path.is_dir())
                    finally:
                        fourth.close()
                finally:
                    third.close()
            finally:
                second.close()
                first.close()

    def test_abandoned_worker_root_without_a_live_lock_is_removed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wechat-root-") as temp:
            base = Path(temp) / "worker-temp"
            abandoned = base / "worker-abandoned"
            abandoned.mkdir(parents=True)
            (abandoned / ROOT_LEASE_NAME).write_bytes(b"1")
            live = base / "worker-live"
            live.mkdir(parents=True)
            handle = claim_lease(live / ROOT_LEASE_NAME)
            self.assertIsNotNone(handle)
            try:
                cleanup_abandoned_roots(base=base)
                self.assertFalse(abandoned.exists())
                self.assertTrue(live.is_dir())
            finally:
                release_lease(handle)


class ImageIndexRefreshTests(unittest.TestCase):
    class _PassthroughCache(DecryptedImageCache):
        def __init__(self, attachment_dir: Path) -> None:
            super().__init__(attachment_dir, b"0" * 16)
            self.xor_key = 1

        def _decrypt(self, source: Path) -> Path | None:
            return source

    def test_new_image_file_becomes_visible_after_the_refresh_interval(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wechat-images-") as temp:
            attachment = Path(temp)
            (attachment / ("a" * 32 + ".dat")).write_bytes(b"synthetic")
            cache = self._PassthroughCache(attachment)
            try:
                self.assertIsNotNone(cache.resolve(["a" * 32]))
                self.assertIsNone(cache.resolve(["b" * 32]))
                (attachment / ("b" * 32 + ".dat")).write_bytes(b"synthetic")
                self.assertIsNone(cache.resolve(["b" * 32]))  # throttled
                cache._index_built_at = time.monotonic() - 31
                self.assertIsNotNone(cache.resolve(["b" * 32]))
            finally:
                cache.close()


class EmptyMarkdownTests(unittest.TestCase):
    def test_empty_range_export_escapes_chat_controlled_title_and_id(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wechat-empty-md-") as temp:
            output = Path(temp) / "chat.md"
            conversation = Conversation("id`\n# 注入", "标题\n# 注入", ConversationKind.DIRECT)
            worker._write_models_atomic(conversation, [], output, overwrite=False)
            text = output.read_text(encoding="utf-8")
            self.assertNotIn("\n# 注入", text)
            self.assertIn("\\# 注入", text)
            self.assertIn("（所选日期范围内没有消息。）", text)


class DisplayDateTests(unittest.TestCase):
    def test_local_boundary_dates_do_not_shift_to_the_previous_day(self) -> None:
        start = datetime(2026, 11, 23, 16, 0, tzinfo=timezone.utc)  # 24th 00:00 UTC+8
        end = datetime(2026, 11, 24, 15, 59, 59, tzinfo=timezone.utc)  # 24th 23:59:59
        self.assertEqual(display_date(start), "2026-11-24")
        self.assertEqual(display_date(end, end=True), "2026-11-24")

    def test_legacy_exclusive_midnight_end_shows_the_last_included_day(self) -> None:
        legacy_end = datetime(2026, 11, 24, 16, 0, tzinfo=timezone.utc)  # 25th 00:00 UTC+8
        self.assertEqual(display_date(legacy_end, end=True), "2026-11-24")


class JobHistoryTests(unittest.TestCase):
    def test_corrupt_manifest_does_not_hide_other_jobs(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wechat-history-") as temp:
            root = Path(temp)
            service = TaskService(jobs_root=root / "jobs", scenes_file=root / "scenes.json")
            conversation = Conversation("conv", "虚构会话", ConversationKind.DIRECT)
            message = Message(
                id="m1",
                conversation_id="conv",
                sender="我",
                timestamp=datetime(2026, 11, 24, 9, tzinfo=timezone.utc),
                type=MessageType.TEXT,
                content="合成内容",
            )

            def writer(files_dir: Path, formats: list[str]) -> list[Path]:
                path = files_dir / "chat.md"
                path.write_text("# 合成\n", encoding="utf-8")
                return [path]

            def export() -> str:
                result = service.export_job(
                    conversation=conversation,
                    messages=[message],
                    formats=["markdown"],
                    target={"kind": "none"},
                    scene=None,
                    start=None,
                    end=None,
                    query=None,
                    source_kind="demo",
                    source_label="虚构",
                    wechat_version=None,
                    export_fn=writer,
                )
                return result["job"]["jobId"]

            broken = export()
            intact = export()
            store = JobStore(root / "jobs")
            record = store.load(broken)
            (record.directory / "manifest.json").write_text("{broken", encoding="utf-8")

            listed = store.list_jobs()

            self.assertEqual({item.job_id for item in listed}, {intact})
            self.assertTrue(store.last_warnings)
            self.assertIn(broken, store.last_warnings[0])


class PruneTests(unittest.TestCase):
    def test_keep_unfinished_preserves_ready_and_failed_jobs(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wechat-prune-") as temp:
            root = Path(temp)
            service = TaskService(jobs_root=root / "jobs", scenes_file=root / "scenes.json")
            conversation = Conversation("conv", "虚构会话", ConversationKind.DIRECT)
            message = Message(
                id="m1",
                conversation_id="conv",
                sender="我",
                timestamp=datetime(2026, 11, 24, 9, tzinfo=timezone.utc),
                type=MessageType.TEXT,
                content="合成内容",
            )

            def writer(files_dir: Path, formats: list[str]) -> list[Path]:
                path = files_dir / "chat.md"
                path.write_text("# 合成\n", encoding="utf-8")
                return [path]

            def export() -> str:
                result = service.export_job(
                    conversation=conversation,
                    messages=[message],
                    formats=["markdown"],
                    target={"kind": "none"},
                    scene=None,
                    start=None,
                    end=None,
                    query=None,
                    source_kind="demo",
                    source_label="虚构",
                    wechat_version=None,
                    export_fn=writer,
                )
                return result["job"]["jobId"]

            delivered = export()
            failed = export()
            ready = export()
            service.record_delivery(delivered, "delivered", {"kind": "clipboard"})
            store = JobStore(root / "jobs")
            store.set_state(store.load(failed), JobStatus.FAILED)

            removed = store.prune(0, keep_unfinished=True, now=utc_now() + timedelta(days=1))
            self.assertIn(delivered, removed)
            self.assertNotIn(failed, removed)
            self.assertNotIn(ready, removed)


if __name__ == "__main__":
    unittest.main()
