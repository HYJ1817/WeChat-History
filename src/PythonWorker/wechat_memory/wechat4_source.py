"""Read-only WeChat 4.x adapter for the native worker.

The upstream Qt application opens a full local source that also materialises
images, voice files and transcription caches. The native app needs a stricter
boundary: this module opens databases read-only, keeps the database key only in
worker memory, places every plaintext copy in an application-private temporary
root, and returns text/system messages unless the caller explicitly enables
images or voice.

No path, account id, key, conversation name or message content is ever written
to stdout/stderr or to a log by this module.
"""
from __future__ import annotations

import hashlib
import html
import os
import re
import sqlite3
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable
from xml.etree import ElementTree

import zstandard

from wechat_context_exporter.models import Conversation, ConversationKind, Message, MessageType
from wechat_context_exporter.sources.base import (
    SourceError,
    safe_mtime_ns,
    sqlite_read_only_uri,
)
from wechat_context_exporter.sources.wechat4_crypto import (
    DecryptedDatabaseCache,
    extract_image_key,
)
from wechat_context_exporter.sources.wechat4_discovery import (
    WeChat4Account,
    discover_wechat4_accounts,
)
from wechat_context_exporter.sources.wechat4_key_capture import capture_account_key
from wechat_context_exporter.sources.wechat4_media import DecryptedImageCache
from wechat_context_exporter.sources.wechat4_voice import WeChatVoiceCache
from wechat_context_exporter.voice import (
    VoiceTranscriptCache,
    VoiceTranscriber,
    voice_placeholder,
)

from .private_temp import (
    LEGACY_STALE_SECONDS,
    SESSION_LEASE_NAME,
    SESSION_PREFIX,
    claim_lease,
    get_worker_temp_root,
    lease_active,
    release_lease,
    remove_tree,
)

UTC8 = timezone(timedelta(hours=8))
MAX_MESSAGES = 200_000
# Keep in sync with MESSAGE_TYPE_LABELS in the upstream source; duplicated here
# because the vendored key-capture module does not own the message labels.
_TYPE_LABELS = {
    42: "[名片]",
    43: "[视频]",
    47: "[表情]",
    48: "[位置]",
    50: "[通话]",
    10000: "[系统消息]",
    10002: "[已撤回消息]",
}


class SourceFailure(Exception):
    """A fixed, user-explainable WeChat failure. Never carries content/paths."""

    def __init__(self, code: str, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class AccountItem:
    token: str
    label: str
    last_used: str
    recent: bool


@dataclass(frozen=True, slots=True)
class Page:
    items: tuple[Message, ...]
    total: int
    next_offset: int | None


@dataclass(frozen=True, slots=True)
class _Row:
    message: Message
    local_id: int
    sort_timestamp: float


def _token(account: WeChat4Account) -> str:
    return hashlib.sha256(("wechat-memory-native\0" + account.id).encode("utf-8")).hexdigest()[:12]


def _now_iso() -> str:
    return datetime.now(UTC8).isoformat(timespec="seconds")


def _find_weixin_executable() -> Path | None:
    """Find Weixin.exe without a full-disk scan.

    Upstream only checks Program Files and ``X:\\Weixin\\Weixin.exe``. Some
    Chinese installs use a nonstandard top-level folder such as
    ``C:\\微信\\Weixin\\Weixin.exe``; check one directory level on each drive,
    matching the discovery module's bounded scan.
    """
    candidates: list[Path] = [
        Path(r"C:\Program Files\Tencent\Weixin\Weixin.exe"),
        Path(r"C:\Program Files (x86)\Tencent\Weixin\Weixin.exe"),
    ]
    if os.name == "nt":
        for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            drive = Path(f"{letter}:\\")
            try:
                if not drive.exists():
                    continue
            except OSError:
                continue
            candidates.append(drive / "Weixin" / "Weixin.exe")
            try:
                children = list(drive.iterdir())
            except OSError:
                continue
            for child in children:
                try:
                    if child.is_dir():
                        candidates.append(child / "Weixin" / "Weixin.exe")
                except OSError:
                    continue
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def _restore_wechat() -> None:
    executable = _find_weixin_executable()
    if executable is None:
        return
    try:
        subprocess.Popen([str(executable)], close_fds=True)
    except OSError:
        pass


class PrivateTempRoot:
    """A per-worker temporary root with a cross-process lease.

    The UI passes a unique root for every worker.  Sessions below it are
    protected by a lock file, so a second instance cannot delete a live
    session merely because it is older than six hours.  Legacy session
    directories without a lock keep the old age-based cleanup.
    """

    def __init__(self, base: Path | None = None) -> None:
        self.base = base if base is not None else get_worker_temp_root()
        self.path: Path | None = None
        self._lease = None

    def open(self) -> Path:
        self.base.mkdir(parents=True, exist_ok=True)
        self._cleanup_stale()
        self.path = Path(tempfile.mkdtemp(prefix=SESSION_PREFIX, dir=self.base))
        self._lease = claim_lease(self.path / SESSION_LEASE_NAME)
        if self._lease is None:
            remove_tree(self.path)
            self.path = None
            raise OSError("cannot claim a private session directory")
        return self.path

    def _cleanup_stale(self) -> None:
        try:
            children = list(self.base.iterdir())
        except OSError:
            return
        for child in children:
            try:
                if not child.is_dir() or not child.name.startswith(SESSION_PREFIX):
                    continue
                state = lease_active(child / SESSION_LEASE_NAME)
                if state is True:
                    continue
                if state is None and child.stat().st_mtime >= time.time() - LEGACY_STALE_SECONDS:
                    continue
                remove_tree(child)
            except OSError:
                continue

    def close(self) -> None:
        release_lease(self._lease)
        self._lease = None
        if self.path is not None:
            remove_tree(self.path)
            self.path = None


class _LoginClicker:
    """WeChat 4.2 ignores UIA Invoke on its login button; a real click works.

    The thread only looks for a visible LoginWindow and clicks its widest
    enabled button. It never reads text and stops as soon as capture finishes.
    """

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        try:
            import pywinauto  # noqa: F401
        except ImportError:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            import subprocess

            from pywinauto import Desktop
        except ImportError:
            return
        while not self._stop.is_set():
            try:
                output = subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     "Get-Process Weixin -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id"],
                    capture_output=True, text=True, check=False,
                )
                for pid_text in output.stdout.split():
                    if not pid_text.isdigit():
                        continue
                    for window in Desktop(backend="uia").windows(process=int(pid_text), visible_only=True):
                        if "LoginWindow" not in window.class_name():
                            continue
                        buttons = [
                            button for button in window.descendants(control_type="Button")
                            if button.is_enabled() and button.rectangle().width() >= 140
                        ]
                        if buttons:
                            max(buttons, key=lambda button: button.rectangle().width()).click_input()
            except Exception:
                pass
            self._stop.wait(1.2)

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)


class WeChat4Source:
    """A connected, read-only WeChat account session."""

    def __init__(self) -> None:
        self._accounts: dict[str, WeChat4Account] = {}
        self._account: WeChat4Account | None = None
        self._own_wxid: str | None = None
        self._account_label = ""
        self._contacts: dict[str, str] = {}
        self._locations: dict[str, list[tuple[str, str]]] = {}
        self._conversations: list[Conversation] = []
        self._raw_key: bytes | None = None
        self._databases: DecryptedDatabaseCache | None = None
        self._images: DecryptedImageCache | None = None
        self._voices: WeChatVoiceCache | None = None
        self._transcripts: VoiceTranscriptCache | None = None
        self._temp = PrivateTempRoot()
        self._connected = False
        self.image_key_available = False
        self.voice_available = False
        self.capabilities = {
            "images": False,
            "voice": False,
        }

    # ------------------------------------------------------------------ #
    # Account discovery and connection
    # ------------------------------------------------------------------ #

    @staticmethod
    def discover_accounts() -> list[AccountItem]:
        try:
            accounts = discover_wechat4_accounts()
        except OSError:
            return []
        items: list[AccountItem] = []
        for index, account in enumerate(accounts, start=1):
            try:
                stamp = datetime.fromtimestamp(safe_mtime_ns(account.account_dir) / 1e9, UTC8)
                last_used = stamp.isoformat(timespec="seconds")
            except (OverflowError, OSError, ValueError):
                last_used = ""
            stamp_label = last_used[5:16].replace("T", " ") if last_used else ""
            label = (
                "微信账户（最近使用）"
                if index == 1
                else f"微信账户（上次使用 {stamp_label}）" if stamp_label
                else "微信账户"
            )
            items.append(AccountItem(
                token=_token(account),
                label=label,
                last_used=last_used,
                recent=index == 1,
            ))
        return items

    def connect(
        self,
        account_token: str | None,
        *,
        enable_images: bool,
        enable_voice: bool,
        progress: Callable[[int, int, str], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, object]:
        if self._connected:
            self.close()
        accounts = discover_wechat4_accounts()
        if not accounts:
            raise SourceFailure("WECHAT_NOT_FOUND", "没有找到本机微信 4.x 数据目录。请先登录微信。")
        self._accounts = {_token(account): account for account in accounts}
        if account_token:
            account = self._accounts.get(account_token)
            if account is None:
                raise SourceFailure("ACCOUNT_NOT_FOUND", "找不到所选微信账户，请刷新账户列表。")
        else:
            account = accounts[0]
        self._account = account
        self._own_wxid = re.sub(r"_[0-9a-fA-F]{4}$", "", account.id)
        root = self._temp.open()

        clicker = _LoginClicker()
        clicker.start()
        try:
            if progress:
                progress(1, 6, "正在重启微信并捕获数据库密钥")
            self._raw_key = capture_account_key(
                account.account_dir,
                executable=_find_weixin_executable(),
                timeout=300.0,
                progress=lambda current, total, label: progress and progress(
                    min(4, 1 + current), 8, self._safe_stage(label)
                ),
                cancelled=cancelled,
            )
        except SourceError as error:
            _restore_wechat()
            raise self._map_source_error(error, phase="key") from None
        finally:
            clicker.close()
        if cancelled and cancelled():
            raise SourceFailure("CANCELLED", "连接已取消。")
        if progress:
            progress(5, 8, "正在读取会话与联系人")
        try:
            self._databases = DecryptedDatabaseCache(
                account.db_dir, raw_key=self._raw_key, temp_root=root
            )
            # The nickname is cosmetic: a missing or unusual contact database
            # must not make an otherwise usable connection fail.
            try:
                self._contacts = self._load_contacts()
            except (SourceFailure, SourceError, sqlite3.Error, OSError, ValueError):
                self._contacts = {}
            self._account_label = (
                self._contacts.get(self._own_wxid or "")
                or self._contacts.get(account.id)
                or ""
            ).strip()
            self._locations = self._index_message_tables(progress)
            self._conversations = self._load_conversations()
        except SourceError as error:
            raise self._map_source_error(error, phase="db") from None
        except sqlite3.Error:
            raise SourceFailure("WECHAT_DB_UNSUPPORTED", "微信数据库结构不受支持，可能是微信版本已更新。") from None

        if enable_images:
            if progress:
                progress(7, 8, "正在准备图片解密")
            try:
                image_key = extract_image_key(
                    account.attachment_dir,
                    progress=lambda current, total, label: progress and progress(7, 8, "正在读取图片密钥"),
                    cancelled=cancelled,
                )
                if image_key:
                    self._images = DecryptedImageCache(
                        account.attachment_dir, image_key, temp_root=root
                    )
                    self.image_key_available = True
            except (SourceError, OSError, ValueError):
                self.image_key_available = False
        if enable_voice:
            self._voices = WeChatVoiceCache(self._databases, account.id, root=root / "voice-audio")
            self._transcripts = VoiceTranscriptCache(root=root / "voice-transcripts")
            self.voice_available = True
        self.capabilities = {"images": self.image_key_available, "voice": self.voice_available}
        self._connected = True
        if progress:
            progress(8, 8, "微信数据已就绪")
        return {
            "accountLabel": self._account_label or next(
                (item.label for item in self.discover_accounts() if item.token == _token(account)),
                "微信账户",
            ),
            "conversationCount": len(self._conversations),
            "messageTableCount": len(self._locations),
            "imageKeyAvailable": self.image_key_available,
            "voiceAvailable": self.voice_available,
        }

    @staticmethod
    def _safe_stage(label: str) -> str:
        # Upstream progress labels are fixed strings; keep only known stages so
        # a future upstream change cannot leak a path into the protocol.
        if "扫码" in label or "登录" in label:
            return "请在微信窗口扫码或确认登录"
        if "密钥" in label:
            return "正在验证本机账户数据库密钥"
        if "已关闭" in label or "安全" in label:
            return "微信已关闭，正在准备安全读取"
        return "正在连接微信"

    @staticmethod
    def _map_source_error(error: SourceError, phase: str) -> SourceFailure:
        text = str(error).casefold()
        if "cancel" in text and "取消" in str(error):
            return SourceFailure("CANCELLED", "连接已取消。")
        if "frida" in text:
            return SourceFailure("WECHAT_KEY_UNAVAILABLE", "缺少本地微信读取组件，无法连接微信。", True)
        if "attach" in text:
            return SourceFailure(
                "WECHAT_KEY_UNAVAILABLE",
                "无法连接微信进程以读取数据库密钥。请确认微信能正常启动，然后重试；如反复失败，请重启电脑后再试。",
                True,
            )
        if "did not close" in text:
            return SourceFailure(
                "WECHAT_CONNECT_FAILED",
                "微信未能及时关闭。请手动退出微信，然后重试。",
                True,
            )
        if "pywinauto" in text:
            return SourceFailure("WECHAT_KEY_UNAVAILABLE", "缺少微信登录辅助组件，请在微信窗口手动登录后重试。", True)
        if "weixin.exe" in text:
            return SourceFailure("WECHAT_EXE_NOT_FOUND", "没有找到微信程序。请先启动微信一次，再重试。", True)
        if "not found" in text or "cannot find" in text:
            return SourceFailure("WECHAT_NOT_FOUND", "没有找到可用的微信数据或微信程序。", True)
        if "timed out" in text or "timeout" in text or "超时" in str(error):
            return SourceFailure("WECHAT_LOGIN_TIMEOUT", "等待微信登录超时。请重新连接并在 5 分钟内完成扫码或手机确认。", True)
        if "schema" in text or "unsupported" in text:
            return SourceFailure("WECHAT_DB_UNSUPPORTED", "微信数据库结构不受支持，可能是微信版本已更新。")
        if "changed repeatedly" in text:
            return SourceFailure("WECHAT_DB_BUSY", "微信数据库正在频繁写入，请稍后重试。", True)
        if "memory" in text or "administrator" in text or "access" in text:
            return SourceFailure("ACCESS_DENIED", "无法读取微信进程内存。请尝试以管理员身份运行。", True)
        return SourceFailure(
            "WECHAT_CONNECT_FAILED",
            "连接微信失败。请确认微信已登录、版本为 4.x，然后重试。", True
        )

    # ------------------------------------------------------------------ #
    # Query helpers
    # ------------------------------------------------------------------ #

    def list_conversations(self, query: str | None = None) -> list[Conversation]:
        self._require_connected()
        if not query:
            return list(self._conversations)
        needle = query.casefold()
        return [
            item for item in self._conversations
            if needle in item.name.casefold() or needle in item.id.casefold()
        ]

    def get_messages(
        self,
        conversation_id: str,
        *,
        start_epoch: int | None,
        end_epoch_exclusive: int | None,
        query: str | None,
        offset: int,
        limit: int,
        order: str = "asc",
    ) -> Page:
        self._require_connected()
        conversation = next((item for item in self._conversations if item.id == conversation_id), None)
        if conversation is None:
            raise SourceFailure("CONVERSATION_NOT_FOUND", "找不到指定的会话。")
        locations = self._locations.get(conversation_id, [])
        if not locations:
            raise SourceFailure("CONVERSATION_NOT_FOUND", "此会话没有可读取的消息表。")
        rows: list[_Row] = []
        for relative_path, table in locations:
            try:
                db_path = self._databases.get(relative_path)  # type: ignore[union-attr]
                rows.extend(self._read_table(conversation, relative_path, table, db_path,
                                             start_epoch, end_epoch_exclusive))
            except SourceError as error:
                raise self._map_source_error(error, phase="query") from None
            except sqlite3.Error:
                raise SourceFailure("WECHAT_DB_UNSUPPORTED", "微信消息表结构不受支持。") from None
            if len(rows) > MAX_MESSAGES:
                raise SourceFailure("RESULT_LIMIT_EXCEEDED", "所选范围内消息过多，请缩小日期范围。")
        rows.sort(key=lambda item: (item.sort_timestamp, item.local_id))
        if query:
            needle = query.casefold()
            rows = [
                item for item in rows
                if needle in item.message.sender.casefold()
                or needle in item.message.content.casefold()
                or needle in item.message.type.value.casefold()
            ]
        if order == "desc":
            rows.reverse()
        total = len(rows)
        page = rows[offset:offset + limit]
        next_offset = offset + len(page)
        return Page(
            items=tuple(item.message for item in page),
            total=total,
            next_offset=next_offset if next_offset < total else None,
        )

    def _read_table(
        self,
        conversation: Conversation,
        relative_path: str,
        table: str,
        db_path: Path,
        start_epoch: int | None,
        end_epoch_exclusive: int | None,
    ) -> list[_Row]:
        connection = sqlite3.connect(sqlite_read_only_uri(db_path), uri=True)
        connection.row_factory = sqlite3.Row
        try:
            columns = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
            required = {"local_id", "create_time", "local_type", "real_sender_id", "message_content"}
            if not required.issubset(columns):
                raise SourceFailure("WECHAT_DB_UNSUPPORTED", "微信消息表缺少必要字段，可能是版本不兼容。")
            selected = [
                "m.local_id", "m.create_time", "m.local_type", "m.real_sender_id",
                "m.message_content", "n.user_name AS sender_wxid",
            ]
            selected.append("m.server_id" if "server_id" in columns else "0 AS server_id")
            selected.append(
                'm."WCDB_CT_message_content" AS content_compression'
                if "WCDB_CT_message_content" in columns else "0 AS content_compression"
            )
            selected.append(
                "m.packed_info_data AS packed_info_data"
                if "packed_info_data" in columns else "NULL AS packed_info_data"
            )
            # Some WeChat builds store create_time in milliseconds.  The date
            # filter runs in SQL before _row_to_message normalises the value,
            # so compare against the table's own unit instead of assuming
            # seconds.
            scale = _timestamp_scale(connection, table)
            where: list[str] = []
            parameters: list[int] = []
            if start_epoch is not None:
                where.append("m.create_time >= ?")
                parameters.append(start_epoch * scale)
            if end_epoch_exclusive is not None:
                where.append("m.create_time < ?")
                parameters.append(end_epoch_exclusive * scale)
            where_sql = " WHERE " + " AND ".join(where) if where else ""
            own_sender_id = self._own_sender_id(connection)
            cursor = connection.execute(
                f'SELECT {", ".join(selected)} FROM "{table}" m '
                f"LEFT JOIN Name2Id n ON n.rowid = m.real_sender_id"
                f"{where_sql} ORDER BY m.create_time ASC, m.local_id ASC",
                parameters,
            )
            # Stream rows instead of fetchall(): even a page request reads the
            # matching range, but raw sqlite rows no longer coexist with the
            # converted Message objects.  The cap still turns a too-broad
            # range into a fixed error before it can exhaust memory.
            result: list[_Row] = []
            for row in cursor:
                result.append(self._row_to_message(conversation, row, own_sender_id, relative_path))
                if len(result) > MAX_MESSAGES:
                    raise SourceFailure("RESULT_LIMIT_EXCEEDED", "所选范围内消息过多，请缩小日期范围。")
        finally:
            connection.close()
        return result

    def _own_sender_id(self, connection: sqlite3.Connection) -> int | None:
        row = connection.execute(
            "SELECT rowid FROM Name2Id WHERE user_name=?", (self._own_wxid,)
        ).fetchone()
        return int(row[0]) if row else None


    def _row_to_message(
        self,
        conversation: Conversation,
        row: sqlite3.Row,
        own_sender_id: int | None,
        source_database: str,
    ) -> _Row:
        raw_type = int(row["local_type"] or 0)
        kind = raw_type & 0xFFFF
        content = _decode_content(row["message_content"], row["content_compression"])
        prefix_sender: str | None = None
        if conversation.kind is ConversationKind.GROUP:
            prefix_sender, content = _strip_group_prefix(content)
        local_id = int(row["local_id"] or 0)
        server_id = int(row["server_id"] or 0)
        sender_wxid = str(row["sender_wxid"] or prefix_sender or "")
        outgoing = own_sender_id is not None and int(row["real_sender_id"] or 0) == own_sender_id
        if outgoing:
            sender = "我"
        elif conversation.kind is ConversationKind.GROUP:
            sender = self._contacts.get(sender_wxid, sender_wxid or "群成员")
        else:
            sender = conversation.name

        model_type = MessageType.TEXT
        rendered = content
        audio_path: Path | None = None
        duration_ms: int | None = None
        transcript: str | None = None
        if kind == 3:
            identifier_source = _image_identifiers(row["packed_info_data"], content)
            image_path = self._images.resolve(identifier_source) if self._images else None
            if image_path is not None:
                model_type = MessageType.IMAGE
                rendered = str(image_path)
            else:
                model_type = MessageType.SYSTEM
                rendered = "[图片]"
        elif kind == 49:
            model_type = MessageType.FILE
            rendered = _type_49_content(content, raw_type)
        elif kind == 34:
            duration_ms = _voice_duration(content)
            if self._voices is not None:
                audio_path = self._voices.resolve(source_database, conversation.id, local_id, server_id)
            if audio_path is not None:
                model_type = MessageType.VOICE
                transcript = self._transcripts.load(audio_path, "small") if self._transcripts else None
                rendered = transcript or voice_placeholder(duration_ms, True)
            else:
                model_type = MessageType.SYSTEM
                rendered = voice_placeholder(duration_ms, False)
        elif kind in _TYPE_LABELS:
            model_type = MessageType.SYSTEM
            rendered = _TYPE_LABELS[kind]
        elif kind != 1:
            model_type = MessageType.SYSTEM
            rendered = f"[消息类型 {kind}]"
        elif not rendered:
            model_type = MessageType.SYSTEM
            rendered = "[空消息]"

        timestamp = int(row["create_time"] or 0)
        if timestamp > 10_000_000_000:
            timestamp //= 1000
        instant = datetime.fromtimestamp(timestamp, UTC8)
        source_id = Path(source_database).stem
        message_id = str(server_id) if server_id else f"{conversation.id}:{source_id}:{local_id}"
        message = Message(
            id=message_id,
            conversation_id=conversation.id,
            sender=sender,
            timestamp=instant,
            type=model_type,
            content=rendered,
            is_outgoing=outgoing,
            audio_path=audio_path,
            duration_ms=duration_ms,
            transcript=transcript,
        )
        return _Row(message=message, local_id=local_id, sort_timestamp=timestamp)

    # ------------------------------------------------------------------ #
    # Database loading
    # ------------------------------------------------------------------ #

    def _load_contacts(self) -> dict[str, str]:
        path = self._databases.get(r"contact\contact.db")  # type: ignore[union-attr]
        connection = sqlite3.connect(sqlite_read_only_uri(path), uri=True)
        connection.row_factory = sqlite3.Row
        try:
            columns = {row[1] for row in connection.execute('PRAGMA table_info("contact")')}
            if "username" not in columns:
                raise SourceFailure("WECHAT_DB_UNSUPPORTED", "微信联系人表结构不受支持。")
            fields = [field for field in ("username", "alias", "nick_name", "remark") if field in columns]
            rows = connection.execute(f'SELECT {", ".join(fields)} FROM contact').fetchall()
        finally:
            connection.close()
        contacts: dict[str, str] = {}
        for row in rows:
            values = dict(row)
            username = str(values.get("username") or "").strip()
            if not username:
                continue
            display = (
                str(values.get("remark") or "").strip()
                or str(values.get("nick_name") or "").strip()
                or str(values.get("alias") or "").strip()
                or username
            )
            contacts[username] = display
        return contacts

    def _load_conversations(self) -> list[Conversation]:
        path = self._databases.get(r"session\session.db")  # type: ignore[union-attr]
        connection = sqlite3.connect(sqlite_read_only_uri(path), uri=True)
        connection.row_factory = sqlite3.Row
        try:
            columns = {row[1] for row in connection.execute('PRAGMA table_info("SessionTable")')}
            if "username" not in columns:
                raise SourceFailure("WECHAT_DB_UNSUPPORTED", "微信会话表结构不受支持。")
            timestamp = "last_timestamp" if "last_timestamp" in columns else "0"
            rows = connection.execute(
                f"SELECT username, {timestamp} AS last_timestamp FROM SessionTable "
                "WHERE username IS NOT NULL AND username <> '' ORDER BY last_timestamp DESC"
            ).fetchall()
        finally:
            connection.close()
        conversations: list[Conversation] = []
        seen: set[str] = set()
        for row in rows:
            username = str(row["username"])
            if username in seen or username not in self._locations:
                continue
            seen.add(username)
            name = self._contacts.get(username) or _fallback_conversation_name(username)
            kind = ConversationKind.GROUP if "@chatroom" in username else ConversationKind.DIRECT
            conversations.append(Conversation(username, name, kind))
        return conversations

    def _index_message_tables(
        self, progress: Callable[[int, int, str], None] | None
    ) -> dict[str, list[tuple[str, str]]]:
        locations: dict[str, list[tuple[str, str]]] = {}
        message_paths = sorted(
            path for path in (self._account.db_dir / "message").glob("message_*.db")  # type: ignore[union-attr]
            if path.stem.removeprefix("message_").isdigit()
        )
        total = len(message_paths)
        for index, source in enumerate(message_paths, start=1):
            if progress:
                progress(5, 8, f"正在索引消息分片 {index}/{total}")
            relative = str(source.relative_to(self._account.db_dir)).replace("/", "\\")  # type: ignore[union-attr]
            db_path = self._databases.get(relative)  # type: ignore[union-attr]
            connection = sqlite3.connect(sqlite_read_only_uri(db_path), uri=True)
            try:
                tables = {
                    str(row[0]) for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
                    )
                }
                has_name_map = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='Name2Id'"
                ).fetchone()
                if not has_name_map:
                    continue
                usernames = connection.execute("SELECT user_name FROM Name2Id").fetchall()
            finally:
                connection.close()
            for row in usernames:
                username = str(row[0] or "").strip()
                if not username:
                    continue
                table = "Msg_" + hashlib.md5(username.encode("utf-8")).hexdigest()
                if table in tables:
                    locations.setdefault(username, []).append((relative, table))
        if not locations:
            raise SourceFailure("WECHAT_NO_MESSAGES", "没有找到可读取的微信消息表。请先在微信中收发消息。")
        return locations

    # ------------------------------------------------------------------ #
    # Voice / lifecycle
    # ------------------------------------------------------------------ #

    def transcribe(
        self,
        messages: Iterable[Message],
        *,
        progress: Callable[[int, int, str], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
        model_root: Path,
    ) -> list[Message]:
        if not self.voice_available:
            raise SourceFailure("VOICE_UNAVAILABLE", "语音读取未启用。请重新连接并开启语音。")
        transcriber = VoiceTranscriber(
            cache=self._transcripts,
            model_root=model_root,
        )
        try:
            return transcriber.transcribe_messages(messages, progress=progress, cancelled=cancelled)
        except RuntimeError as error:
            raise SourceFailure("VOICE_COMPONENT_MISSING", str(error)) from None
        except OSError:
            raise SourceFailure("VOICE_FAILED", "语音转写失败。请检查磁盘空间后重试。", True) from None

    def _require_connected(self) -> None:
        if not self._connected or self._databases is None:
            raise SourceFailure("WECHAT_NOT_CONNECTED", "尚未连接微信。请先选择账户并连接。")

    def close(self) -> None:
        try:
            if self._images is not None:
                self._images.close()
        except Exception:
            pass
        self._images = None
        try:
            if self._databases is not None:
                self._databases.close()
        except Exception:
            pass
        self._databases = None
        self._voices = None
        self._transcripts = None
        self._raw_key = None
        self._contacts = {}
        self._locations = {}
        self._conversations = []
        self._account = None
        self._connected = False
        self.image_key_available = False
        self.voice_available = False
        self._temp.close()


# --------------------------------------------------------------------------- #
# Pure helpers (kept close to upstream semantics, with conservative group prefix)
# --------------------------------------------------------------------------- #

def _timestamp_scale(connection: sqlite3.Connection, table: str) -> int:
    """Return 1000 when ``create_time`` is stored in milliseconds, else 1."""
    try:
        row = connection.execute(f'SELECT MAX(create_time) FROM "{table}"').fetchone()
        value = int(row[0] or 0) if row else 0
    except (sqlite3.Error, TypeError, ValueError):
        return 1
    return 1000 if value > 10_000_000_000 else 1


def _decode_content(value: object, compression: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, memoryview):
        value = value.tobytes()
    if not isinstance(value, bytes):
        return str(value)
    data = value
    if int(compression or 0) == 4 or data.startswith(b"\x28\xb5\x2f\xfd"):
        try:
            data = zstandard.ZstdDecompressor().decompress(data)
        except zstandard.ZstdError:
            return ""
    return data.decode("utf-8", errors="replace")


def _strip_group_prefix(content: str) -> tuple[str | None, str]:
    match = re.match(r"^([^\s:]{1,80}):(?:\n| )", content)
    if not match:
        return None, content
    return match.group(1), content[match.end():]


def _image_identifiers(*values: object) -> list[str]:
    seen: set[str] = set()
    identifiers: list[str] = []
    pattern = re.compile(rb"(?i)(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])")
    for value in values:
        if isinstance(value, memoryview):
            value = value.tobytes()
        if isinstance(value, str):
            data = value.encode("utf-8", errors="ignore")
        elif isinstance(value, bytes):
            data = value
        else:
            continue
        for match in pattern.finditer(data):
            normalized = match.group(0).decode("ascii").casefold()
            if normalized not in seen:
                seen.add(normalized)
                identifiers.append(normalized)
    return identifiers


def _type_49_content(content: str, raw_type: int) -> str:
    subtype = raw_type >> 32
    title = _xml_value(content, "title")
    if subtype == 57:
        quoted_name = _xml_value(content, "displayname")
        quoted_content = _xml_value(content, "content")
        suffix = f"（引用 {quoted_name}: {quoted_content[:80]}）" if quoted_name else ""
        return (title or "[引用消息]") + suffix
    labels = {
        1: "链接", 3: "音乐", 4: "视频链接", 5: "文件", 6: "小程序", 8: "位置",
        17: "实时位置", 19: "聊天记录", 21: "小程序", 33: "小程序", 36: "链接",
    }
    return f"[{labels.get(subtype, '文件/链接')}] {title}".strip()


def _xml_value(content: str, tag: str) -> str:
    if not content:
        return ""
    try:
        root = ElementTree.fromstring(content)
        node = root.find(f".//{tag}")
        if node is not None and node.text:
            return html.unescape(node.text).strip()
    except ElementTree.ParseError:
        pass
    match = re.search(fr"<{tag}>([\s\S]*?)</{tag}>", content, re.IGNORECASE)
    return html.unescape(match.group(1)).strip() if match else ""


def _voice_duration(content: str) -> int | None:
    if not content:
        return None
    try:
        root = ElementTree.fromstring(content)
        node = root.find(".//voicemsg")
        value = node.attrib.get("voicelength") if node is not None else None
    except ElementTree.ParseError:
        match = re.search(r'<voicemsg\b[^>]*\bvoicelength=["\'](\d+)["\']', content, re.IGNORECASE)
        value = match.group(1) if match else None
    try:
        return int(value) if value else None
    except ValueError:
        return None


def _fallback_conversation_name(username: str) -> str:
    if "@chatroom" in username:
        return f"群聊 {username.split('@', 1)[0]}"
    return username


def account_count() -> int:
    return len(discover_wechat4_accounts())
