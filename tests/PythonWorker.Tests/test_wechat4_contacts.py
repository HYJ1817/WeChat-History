"""Contact table handling used for account nicknames and conversation names."""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKER_DIR = ROOT / "src" / "PythonWorker"
VENDOR_DIR = WORKER_DIR / "vendor"
sys.path.insert(0, str(VENDOR_DIR))
sys.path.insert(0, str(WORKER_DIR))

from wechat_memory.wechat4_source import SourceFailure, WeChat4Source  # noqa: E402


class _FakeDatabases:
    def __init__(self, path: Path) -> None:
        self._path = path

    def get(self, _name: str) -> Path:
        return self._path


class ContactTests(unittest.TestCase):
    def _source_with_contact_db(self, schema: str, rows: list[tuple[str, ...]]) -> WeChat4Source:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        path = Path(self._temp.name) / "contact.db"
        connection = sqlite3.connect(path)
        try:
            connection.execute(schema)
            connection.executemany(
                "INSERT INTO contact VALUES (" + ",".join("?" for _ in rows[0]) + ")",
                rows,
            )
            connection.commit()
        finally:
            connection.close()
        source = WeChat4Source()
        source._databases = _FakeDatabases(path)  # type: ignore[assignment]
        return source

    def test_own_nickname_comes_from_contact_table(self):
        source = self._source_with_contact_db(
            "CREATE TABLE contact (username TEXT, alias TEXT, nick_name TEXT, remark TEXT)",
            [
                ("wxid_self", "", "本人昵称", ""),
                ("wxid_friend", "friend_alias", "好友昵称", "好友备注"),
            ],
        )
        source._own_wxid = "wxid_self"
        contacts = source._load_contacts()
        self.assertEqual(contacts["wxid_self"], "本人昵称")
        self.assertEqual(contacts["wxid_friend"], "好友备注")

    def test_unsupported_contact_schema_has_fixed_error(self):
        source = self._source_with_contact_db(
            "CREATE TABLE contact (id INTEGER, display TEXT)",
            [("1", "x")],
        )
        with self.assertRaises(SourceFailure) as raised:
            source._load_contacts()
        self.assertEqual(raised.exception.code, "WECHAT_DB_UNSUPPORTED")


if __name__ == "__main__":
    unittest.main()
