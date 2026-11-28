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
import heapq
import io
import os
import re
import sqlite3
import subprocess
import tempfile
import threading
import time
from contextlib import ExitStack
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
    DatabaseSnapshot,
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

from .appmsg_parser import (
    MAX_FORWARD_ENTRIES,
    MAX_NESTING,
    AppMessageProjection,
    ForwardItem,
    parse_appmsg,
)
from .global_search import (
    MAX_SCAN_ROWS, SOFT_DEADLINE_SECONDS, SearchDocument, SearchError, SearchHit,
    bounded_context, content_fingerprint,
)
from .message_reader import (
    MAX_RAW_MESSAGE_BYTES, MessageReadError, SQLiteShardReader, TableSchema,
    epoch_microseconds, legacy_timestamp_seconds,
)
from .page_sessions import (
    MAX_CURSOR_PAGE_SIZE,
    MAX_PAGE_SESSIONS as MAX_CURSOR_SESSIONS,
    MAX_PAGE_TOKENS_PER_SESSION as MAX_CURSOR_TOKENS_PER_SESSION,
    PAGE_SESSION_TTL_SECONDS as CURSOR_SESSION_TTL_SECONDS,
    CursorPage,
    PageSessionError,
    PageSessionLimits,
    PageSessionManager,
    PageWindow,
    PreparedPage,
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


def app_message_projection_json(projection: AppMessageProjection) -> dict:
    """Serialize the parser's bounded, immutable projection to the C3 wire shape."""
    if not isinstance(projection, AppMessageProjection):
        raise ValueError("invalid app-message projection")
    if (not isinstance(projection.display_kind, str) or not projection.display_kind or
            projection.parse_status not in {"ok", "partial", "unsupported", "invalid", "rejected", "limit_exceeded"} or
            type(projection.truncated) is not bool or
            (projection.omitted_count is not None and
             (type(projection.omitted_count) is not int or projection.omitted_count < 0))):
        raise ValueError("invalid app-message projection metadata")

    text_bytes = 0
    entry_count = 0

    def text(value: str | None) -> str | None:
        nonlocal text_bytes
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("invalid app-message projection text")
        try:
            text_bytes += len(value.encode("utf-8"))
        except UnicodeEncodeError:
            raise ValueError("invalid app-message projection text") from None
        if text_bytes > 128 * 1024:
            raise ValueError("app-message projection exceeds the wire text bound")
        return value

    def forward(item: ForwardItem, depth: int) -> dict:
        nonlocal entry_count
        if not isinstance(item, ForwardItem) or depth > MAX_NESTING:
            raise ValueError("app-message forward projection exceeds the wire depth bound")
        entry_count += 1
        if entry_count > MAX_FORWARD_ENTRIES:
            raise ValueError("app-message forward projection exceeds the wire entry bound")
        if not isinstance(item.display_kind, str) or not item.display_kind or not isinstance(item.forward_items, tuple):
            raise ValueError("invalid app-message forward item")
        return {
            "sender": text(item.sender),
            "timestamp": text(item.timestamp),
            "content": text(item.content) or "",
            "displayKind": item.display_kind,
            "forwardItems": [forward(child, depth + 1) for child in item.forward_items],
        }

    if not isinstance(projection.forward_items, tuple):
        raise ValueError("invalid app-message forward list")
    result = {
        "displayKind": projection.display_kind,
        "replyTo": text(projection.reply_to),
        "quoteSummary": text(projection.quote_summary),
        "quoteSender": text(projection.quote_sender),
        "forwardItems": [forward(item, 1) for item in projection.forward_items],
        "parseStatus": projection.parse_status,
        "truncated": projection.truncated,
        "omittedCount": projection.omitted_count,
    }
    return result


class SourceFailure(PageSessionError):
    """A fixed, user-explainable WeChat failure. Never carries content/paths."""


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
    projections: tuple[AppMessageProjection | None, ...] = ()


@dataclass(frozen=True, slots=True)
class _CursorLocation:
    relative_path: str
    table: str
    location_key: tuple[str, str]
    schema: TableSchema


@dataclass(frozen=True, slots=True)
class _LiveCursorState:
    conversation_id: str
    start_epoch: int | None
    end_epoch_exclusive: int | None
    order: str
    snapshots: dict[str, DatabaseSnapshot]
    locations: tuple[_CursorLocation, ...]


class _LivePageProvider:
    """SQLite traversal only; the common manager owns the cursor lifecycle."""

    def __init__(self, source: WeChat4Source) -> None:
        self.source = source

    @property
    def generation(self) -> int:
        return self.source._page_generation

    def prepare_page(
        self, conversation_id: str, start_epoch: int | None,
        end_epoch_exclusive: int | None, order: str,
    ) -> PreparedPage[_LiveCursorState]:
        source = self.source
        source._conversations_by_id(conversation_id)
        locations = sorted(
            source._unique_locations(conversation_id),
            key=lambda item: (item[0].casefold(), item[1].casefold()),
        )
        if not locations:
            raise SourceFailure("CONVERSATION_NOT_FOUND", "此会话没有可读取的消息表。")
        snapshots, cursor_locations, total = source._prepare_cursor_locations(
            locations, start_epoch, end_epoch_exclusive,
        )
        return PreparedPage(_LiveCursorState(
            conversation_id, start_epoch, end_epoch_exclusive, order, snapshots, cursor_locations,
        ), total)

    def validate_page(self, state: _LiveCursorState) -> None:
        self.source._validate_cursor_generations(state.snapshots)

    def read_page(
        self, state: _LiveCursorState, boundary: tuple[int, str, str, int] | None,
        direction: str, limit: int,
    ) -> PageWindow[Message | _ProjectedMessage, tuple[int, str, str, int]]:
        return self.source._read_cursor_window(state, boundary, direction, limit)


@dataclass(frozen=True, slots=True)
class _ProjectedMessage:
    """Transport a parser projection beside, never inside, the vendor model."""

    message: Message
    projection: AppMessageProjection

    def __getattr__(self, name: str):
        # Keep source-level cursor callers compatible with Message field access.
        return getattr(self.message, name)


@dataclass(frozen=True, slots=True)
class _Row:
    message: Message
    local_id: int
    sort_timestamp: float
    parse_status: str = "ok"
    content_truncated: bool = False
    projection: AppMessageProjection | None = None


@dataclass(slots=True)
class _ProjectionSlot:
    """Temporary bounded wire sidecar drained into a retained search hit."""

    projection: dict | None


@dataclass(frozen=True, slots=True)
class _LiveLocator:
    conversation_id: str
    relative_path: str
    table: str
    physical_rowid: int
    precise_timestamp: int
    projection_slot: _ProjectionSlot | None = None


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
        self._page_generation = 0
        self._media_revision = 0
        self._cursor_sessions: PageSessionManager[_LiveCursorState, tuple[int, str, str, int], Message] = (
            PageSessionManager()
        )
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
        # Keyword matching keeps the legacy media/transcript projection. A
        # SQL LIKE shortcut would change its match set and is not used.
        shard_paths: dict[str, Path] = {}
        if not query:
            page = self._get_messages_unfiltered_page(
                conversation_id, start_epoch, end_epoch_exclusive, offset, limit, order, shard_paths
            )
            if page is not None:
                return page
        return self._get_messages_legacy(
            conversation_id, start_epoch=start_epoch,
            end_epoch_exclusive=end_epoch_exclusive, query=query,
            offset=offset, limit=limit, order=order, _shard_paths=shard_paths,
        )

    def get_messages_page(
        self,
        conversation_id: str,
        *,
        source_session_id: str,
        start_epoch: int | None,
        end_epoch_exclusive: int | None,
        order: str = "desc",
        limit: int = 50,
    ) -> CursorPage:
        """Create a precise-v1, source-generation-bound preview page session."""
        self._require_connected()
        try:
            return self._cursor_sessions.first_page(
                _LivePageProvider(self), conversation_id, source_session_id=source_session_id,
                start_epoch=start_epoch, end_epoch_exclusive=end_epoch_exclusive,
                order=order, limit=limit, limits=self._cursor_limits(),
            )
        except SourceFailure:
            raise
        except PageSessionError as error:
            raise SourceFailure(error.code, error.message, error.retryable) from None

    def get_messages_cursor_page(
        self,
        source_session_id: str,
        page_session_id: str,
        cursor: str,
        *,
        limit: int = 50,
    ) -> CursorPage:
        """Continue a cursor session without accepting replacement filters."""
        self._require_connected()
        try:
            return self._cursor_sessions.cursor_page(
                _LivePageProvider(self), source_session_id, page_session_id, cursor,
                limit=limit, limits=self._cursor_limits(),
            )
        except SourceFailure:
            raise
        except PageSessionError as error:
            raise SourceFailure(error.code, error.message, error.retryable) from None

    @staticmethod
    def _cursor_limits() -> PageSessionLimits:
        # Keep the existing live constants as compatibility/configuration hooks.
        return PageSessionLimits(
            ttl_seconds=CURSOR_SESSION_TTL_SECONDS,
            max_sessions=MAX_CURSOR_SESSIONS,
            max_tokens_per_session=MAX_CURSOR_TOKENS_PER_SESSION,
            max_page_size=MAX_CURSOR_PAGE_SIZE,
        )

    def _prepare_cursor_locations(
        self,
        locations: list[tuple[str, str]],
        start: int | None,
        end: int | None,
    ) -> tuple[dict[str, DatabaseSnapshot], tuple[_CursorLocation, ...], int]:
        cache = self._databases
        if not callable(getattr(cache, "get_snapshot", None)) or not callable(
            getattr(cache, "is_snapshot_current", None)
        ):
            raise SourceFailure("PAGE_SOURCE_UNVERIFIABLE", "此数据源不支持可验证的分页版本。")
        snapshots: dict[str, DatabaseSnapshot] = {}
        try:
            for relative, _table in locations:
                if relative not in snapshots:
                    snapshots[relative] = cache.get_snapshot(relative)  # type: ignore[union-attr]
            deadline = time.monotonic() + SOFT_DEADLINE_SECONDS
            cursor_locations: list[_CursorLocation] = []
            total = 0
            with ExitStack() as stack:
                readers = {
                    relative: stack.enter_context(SQLiteShardReader(snapshot.path, deadline=deadline))
                    for relative, snapshot in snapshots.items()
                }
                for relative, table in locations:
                    reader = readers[relative]
                    schema = reader.inspect_table(table, check_legacy_order=False, verify_precise_order=True)
                    if not schema.precise_cursor_supported:
                        raise SourceFailure(
                            "PAGE_UNSUPPORTED_SCHEMA",
                            "此消息表没有可验证的稳定行身份或统一时间精度，无法使用游标分页。",
                        )
                    total += reader.count_cursor_rows(table, schema, start, end)
                    cursor_locations.append(_CursorLocation(
                        relative, table, (relative.casefold(), table.casefold()), schema,
                    ))
            self._validate_cursor_generations(snapshots)
            return snapshots, tuple(cursor_locations), total
        except MessageReadError as error:
            raise self._cursor_read_failure(error) from None
        except SourceFailure:
            raise
        except SourceError as error:
            raise self._map_source_error(error, phase="query") from None
        except sqlite3.Error:
            raise SourceFailure("PAGE_UNSUPPORTED_SCHEMA", "微信消息表结构不受支持，无法使用游标分页。") from None
        except OSError:
            raise SourceFailure("PAGE_SOURCE_UNAVAILABLE", "微信消息分片暂时无法读取，请重试。", True) from None

    def _validate_cursor_generations(self, snapshots: dict[str, DatabaseSnapshot]) -> None:
        cache = self._databases
        if not callable(getattr(cache, "is_snapshot_current", None)):
            raise SourceFailure("PAGE_SOURCE_UNVERIFIABLE", "此数据源不支持可验证的分页版本。")
        try:
            current = all(cache.is_snapshot_current(relative, snapshot) for relative, snapshot in snapshots.items())
        except (OSError, SourceError):
            current = False
        if not current:
            raise SourceFailure("PAGE_SOURCE_CHANGED", "微信数据已变化，请从首屏刷新。", True)

    def _read_cursor_window(
        self,
        state: _LiveCursorState,
        boundary: tuple[int, str, str, int] | None,
        direction: str,
        limit: int,
    ) -> PageWindow[Message | _ProjectedMessage, tuple[int, str, str, int]]:
        deadline = time.monotonic() + SOFT_DEADLINE_SECONDS
        try:
            with ExitStack() as stack:
                readers = {
                    relative: stack.enter_context(SQLiteShardReader(snapshot.path, deadline=deadline))
                    for relative, snapshot in state.snapshots.items()
                }
                streams = []
                if direction == "first":
                    key_relation = None
                    reverse = state.order == "desc"
                else:
                    assert boundary is not None
                    boundary_key = boundary
                    key_relation = (
                        "gt" if (direction == "after") == (state.order == "asc") else "lt"
                    )
                    reverse = (state.order == "desc") != (direction == "before")
                    boundary_location = (boundary_key[1], boundary_key[2])
                for location in state.locations:
                    if boundary is None:
                        location_relation = 0
                        boundary_time_us = boundary_rowid = None
                    else:
                        location_relation = (location.location_key > boundary_location) - (
                            location.location_key < boundary_location
                        )
                        boundary_time_us = boundary_key[0]
                        boundary_rowid = boundary_key[3]
                    rows = list(readers[location.relative_path].iter_keyset_rows(
                        location.table, location.schema, state.start_epoch, state.end_epoch_exclusive,
                        boundary_time_us=boundary_time_us,
                        boundary_rowid=boundary_rowid,
                        location_relation=location_relation,
                        key_relation=key_relation or "gt",
                        reverse=reverse,
                        limit=limit + 1,
                    ))
                    stream = [
                        (self._cursor_sort_key(location, row), location, int(row["physical_rowid"]))
                        for row in rows
                    ]
                    if stream:
                        streams.append(stream)
                merged = list(heapq.merge(
                    *streams, key=lambda item: item[0], reverse=reverse,
                ))[:limit + 1]
                has_extra = len(merged) > limit
                selected = merged[:limit]
                if direction == "before":
                    selected.reverse()
                own_ids = {
                    relative: reader.own_sender_id(self._own_wxid)
                    for relative, reader in readers.items()
                }
                messages: list[Message] = []
                for sort_key, location, rowid in selected:
                    reader = readers[location.relative_path]
                    row = next(reader.iter_rows(
                        location.table, location.schema, None, None,
                        precise=True, physical_rowid=rowid,
                    ), None)
                    if row is None or epoch_microseconds(int(row["create_time"] or 0)) != sort_key[0]:
                        raise SourceFailure("PAGE_SOURCE_CHANGED", "分页期间消息位置已变化，请从首屏刷新。", True)
                    projected = self._row_to_message(
                        self._conversations_by_id(state.conversation_id), row,
                        own_ids[location.relative_path], location.relative_path, precise=True,
                    )
                    messages.append(
                        _ProjectedMessage(projected.message, projected.projection)
                        if projected.projection is not None else projected.message
                    )
        except MessageReadError as error:
            raise self._cursor_read_failure(error) from None
        except SourceFailure:
            raise
        except SourceError as error:
            raise self._map_source_error(error, phase="query") from None
        except sqlite3.Error:
            raise SourceFailure("PAGE_UNSUPPORTED_SCHEMA", "微信消息表结构不受支持，无法继续分页。") from None
        except OSError:
            raise SourceFailure("PAGE_SOURCE_UNAVAILABLE", "微信消息分片暂时无法读取，请重试。", True) from None

        return PageWindow(
            tuple(messages), selected[0][0] if selected else None,
            selected[-1][0] if selected else None, has_extra,
        )

    @staticmethod
    def _cursor_sort_key(location: _CursorLocation, row: sqlite3.Row) -> tuple[int, str, str, int]:
        return (
            epoch_microseconds(int(row["create_time"] or 0)),
            location.location_key[0], location.location_key[1],
            int(row["physical_rowid"]),
        )

    # Opaque tokens, expiry, and session eviction are owned by PageSessionManager.

    @staticmethod
    def _cursor_read_failure(error: MessageReadError) -> SourceFailure:
        if error.code == "SEARCH_BUDGET_EXCEEDED":
            return SourceFailure("PAGE_BUDGET_EXCEEDED", error.message, True)
        if error.code in {"PAGE_UNSUPPORTED_SCHEMA", "SEARCH_UNSUPPORTED_SCHEMA", "WECHAT_DB_UNSUPPORTED"}:
            return SourceFailure("PAGE_UNSUPPORTED_SCHEMA", error.message)
        return SourceFailure(error.code, error.message)

    def _conversations_by_id(self, conversation_id: str) -> Conversation:
        conversation = next((item for item in self._conversations if item.id == conversation_id), None)
        if conversation is None:
            raise SourceFailure("CONVERSATION_NOT_FOUND", "找不到指定的会话。")
        return conversation

    def _get_messages_legacy(
        self,
        conversation_id: str,
        *,
        start_epoch: int | None,
        end_epoch_exclusive: int | None,
        query: str | None,
        offset: int,
        limit: int,
        order: str = "asc",
        _shard_paths: dict[str, Path] | None = None,
    ) -> Page:
        self._require_connected()
        conversation = next((item for item in self._conversations if item.id == conversation_id), None)
        if conversation is None:
            raise SourceFailure("CONVERSATION_NOT_FOUND", "找不到指定的会话。")
        locations = self._unique_locations(conversation_id)
        if not locations:
            raise SourceFailure("CONVERSATION_NOT_FOUND", "此会话没有可读取的消息表。")
        rows: list[_Row] = []
        shard_paths = _shard_paths if _shard_paths is not None else {}
        for relative_path, table in locations:
            try:
                if relative_path not in shard_paths:
                    shard_paths[relative_path] = self._databases.get(relative_path)  # type: ignore[union-attr]
                db_path = shard_paths[relative_path]
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
            projections=tuple(item.projection for item in page),
        )

    def _unique_locations(self, conversation_id: str) -> list[tuple[str, str]]:
        seen: set[tuple[str, str]] = set()
        result = []
        for relative, table in self._locations.get(conversation_id, []):
            normalized = relative.replace("/", "\\")
            key = (normalized.casefold(), table.casefold())
            if key not in seen:
                seen.add(key)
                result.append((normalized, table))
        return result

    def _get_messages_unfiltered_page(
        self, conversation_id: str, start: int | None, end: int | None,
        offset: int, limit: int, order: str, shard_paths: dict[str, Path],
    ) -> Page | None:
        conversation = next((item for item in self._conversations if item.id == conversation_id), None)
        if conversation is None:
            raise SourceFailure("CONVERSATION_NOT_FOUND", "找不到指定的会话。")
        locations = self._unique_locations(conversation_id)
        if not locations:
            raise SourceFailure("CONVERSATION_NOT_FOUND", "此会话没有可读取的消息表。")
        deadline = time.monotonic() + SOFT_DEADLINE_SECONDS
        try:
            with ExitStack() as stack:
                shards = {}
                tables = []
                for relative, table in locations:
                    if relative not in shards:
                        if relative not in shard_paths:
                            shard_paths[relative] = self._databases.get(relative)  # type: ignore[union-attr]
                        path = shard_paths[relative]
                        reader = stack.enter_context(SQLiteShardReader(path, deadline=deadline))
                        shards[relative] = reader
                    reader = shards[relative]
                    schema = reader.inspect_table(table)
                    if not schema.legacy_fast_path_supported:
                        # Mixed units, ambiguous ties and WITHOUT ROWID tables
                        # retain the correct reference path instead of using an
                        # unproven LIMIT/order comparator.
                        return None
                    tables.append((relative, table, reader, schema))
                total = sum(reader.count_rows(table, schema, start, end) for _, table, reader, schema in tables)
                if total > MAX_MESSAGES:
                    raise SourceFailure("RESULT_LIMIT_EXCEEDED", "所选范围内消息过多，请缩小日期范围。")
                prefix = min(total, offset + limit)
                if offset >= total:
                    return Page((), total, None)
                candidates = []
                for location_index, (relative, table, reader, schema) in enumerate(tables):
                    for row in reader.iter_rows(table, schema, start, end, legacy_order=order,
                                                limit=prefix, metadata_only=True):
                        key = (legacy_timestamp_seconds(row["create_time"]), int(row["local_id"]),
                               location_index, int(row["create_time"]))
                        candidates.append((key, relative, table, reader, schema, int(row["physical_rowid"])))
                candidates.sort(key=lambda candidate: candidate[0], reverse=order == "desc")
                items = []
                projections = []
                own_ids = {relative: reader.own_sender_id(self._own_wxid) for relative, reader in shards.items()}
                for _, relative, table, reader, schema, rowid in candidates[offset:offset + limit]:
                    row = next(reader.iter_rows(table, schema, None, None, physical_rowid=rowid), None)
                    if row is None:
                        raise SourceFailure("WECHAT_DB_UNSUPPORTED", "消息读取期间发生变化，请刷新。")
                    projected = self._row_to_message(conversation, row, own_ids[relative], relative)
                    items.append(projected.message)
                    projections.append(projected.projection)
                next_offset = offset + len(items)
                return Page(
                    tuple(items), total, next_offset if next_offset < total else None, tuple(projections),
                )
        except MessageReadError as error:
            raise SourceFailure(error.code, error.message) from None
        except SourceError as error:
            raise self._map_source_error(error, phase="query") from None

    def _read_table(
        self,
        conversation: Conversation,
        relative_path: str,
        table: str,
        db_path: Path,
        start_epoch: int | None,
        end_epoch_exclusive: int | None,
    ) -> list[_Row]:
        try:
            with SQLiteShardReader(db_path) as reader:
                schema = reader.inspect_table(table)
                own_sender_id = reader.own_sender_id(self._own_wxid)
                result: list[_Row] = []
                for row in reader.iter_rows(table, schema, start_epoch, end_epoch_exclusive):
                    result.append(self._row_to_message(conversation, row, own_sender_id, relative_path))
                    if len(result) > MAX_MESSAGES:
                        raise SourceFailure("RESULT_LIMIT_EXCEEDED", "所选范围内消息过多，请缩小日期范围。")
                return result
        except MessageReadError as error:
            raise SourceFailure(error.code, error.message) from None

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
        *,
        enrich_media: bool = True,
        precise: bool = False,
        strict_decode: bool = False,
    ) -> _Row:
        raw_type = int(row["local_type"] or 0)
        kind = raw_type & 0xFFFF
        content = _decode_content(row["message_content"], row["content_compression"],
                                  strict=strict_decode or not enrich_media)
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
        reply_to: str | None = None
        parse_status = "ok"
        content_truncated = False
        projection: AppMessageProjection | None = None
        if kind == 3:
            identifier_source = _image_identifiers(row["packed_info_data"], content) if enrich_media else []
            image_path = self._images.resolve(identifier_source) if enrich_media and self._images else None
            if image_path is not None:
                model_type = MessageType.IMAGE
                rendered = str(image_path)
            else:
                model_type = MessageType.SYSTEM if enrich_media else MessageType.IMAGE
                rendered = "[图片]"
        elif kind == 49:
            model_type = MessageType.FILE
            projection = parse_appmsg(content, raw_type)
            rendered = projection.canonical_text
            reply_to = projection.reply_to
            parse_status = projection.parse_status
            content_truncated = projection.truncated
        elif kind == 34:
            duration_ms = _voice_duration(content)
            if enrich_media and self._voices is not None:
                audio_path = self._voices.resolve(source_database, conversation.id, local_id, server_id)
            if audio_path is not None:
                model_type = MessageType.VOICE
                transcript = self._transcripts.load(audio_path, "small") if self._transcripts else None
                rendered = transcript or voice_placeholder(duration_ms, True)
            else:
                model_type = MessageType.SYSTEM if enrich_media else MessageType.VOICE
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

        raw_timestamp = int(row["create_time"] or 0)
        timestamp = legacy_timestamp_seconds(raw_timestamp)
        try:
            instant = (
                datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=epoch_microseconds(raw_timestamp))
            ).astimezone(UTC8) if precise else datetime.fromtimestamp(timestamp, UTC8)
        except (ValueError, OverflowError, OSError):
            raise SourceFailure("WECHAT_DB_UNSUPPORTED", "消息时间精度或取值不受当前读取方式支持。") from None
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
            reply_to=reply_to,
        )
        return _Row(message=message, local_id=local_id, sort_timestamp=timestamp,
                    parse_status=parse_status, content_truncated=content_truncated,
                    projection=projection)

    # ------------------------------------------------------------------ #
    # Complete, media-enriched candidates for B3 keyword page snapshots
    # ------------------------------------------------------------------ #

    def keyword_media_selection(self) -> dict:
        """Bind effective media/configuration identities and transcript revision.

        Normal media resolution reuses these objects. Explicit transcription
        advances the revision even on failure, because it may have saved some
        transcripts before failing. These values are server-only metadata.
        """
        self._require_connected()
        return {
            "images": self._images is not None, "voice": self._voices is not None,
            "transcripts": self._transcripts is not None,
            "imageProvider": id(self._images), "voiceProvider": id(self._voices),
            "transcriptProvider": id(self._transcripts), "revision": self._media_revision,
            "imageKeyAvailable": self.image_key_available, "voiceAvailable": self.voice_available,
        }

    def validate_keyword_generations(self, generations: dict) -> None:
        self._require_connected()
        self._validate_cursor_generations(generations)

    @staticmethod
    def _keyword_message_json(
        message: Message, projection: AppMessageProjection | None = None,
    ) -> dict:
        # Same display as worker._real_message_json, but retain exact time for
        # precise-v1 ordering. Legacy matching sees Message.content separately
        # (not the image placeholder substituted for display).
        result = WeChat4Source._plain_message_json(message, projection)
        if message.type is MessageType.IMAGE:
            result["content"] = "[图片]"
            if message.content:
                result["attachmentPath"] = message.content
        if message.transcript:
            result["transcript"] = message.transcript
        return result

    def iter_keyword_documents(
        self, conversation_id: str, start: int | None, end: int | None, *,
        deadline: float, generations: dict,
    ):
        """Scan one complete date range using legacy sender/content/media rules.

        All decompression, app-message parsing, recovery and cached-transcript
        reads happen before yielding. Continuations only validate signatures;
        they never call this iterator or reopen a message database.
        """
        self._require_connected()
        conversation = self._conversations_by_id(conversation_id)
        locations = self._unique_locations(conversation_id)
        if not locations:
            raise SourceFailure("CONVERSATION_NOT_FOUND", "此会话没有可读取的消息表。")
        if not callable(getattr(self._databases, "get_snapshot", None)) or not callable(
            getattr(self._databases, "is_snapshot_current", None)
        ):
            raise SourceFailure("PAGE_SOURCE_UNVERIFIABLE", "此数据源不支持可验证的分页版本。")
        scanned = 0
        try:
            with ExitStack() as stack:
                readers = {}
                for relative, table in locations:
                    if relative not in readers:
                        snapshot = self._databases.get_snapshot(relative)
                        generations[relative] = snapshot
                        readers[relative] = stack.enter_context(SQLiteShardReader(snapshot.path, deadline=deadline))
                    reader = readers[relative]
                    schema = reader.inspect_table(table, check_legacy_order=False, verify_precise_order=True)
                    if not schema.precise_cursor_supported:
                        raise SourceFailure(
                            "PAGE_UNSUPPORTED_SCHEMA",
                            "此消息表没有可验证的稳定行身份或统一时间精度，无法建立关键词分页。",
                        )
                    own_sender_id = reader.own_sender_id(self._own_wxid)
                    for row in reader.iter_rows(table, schema, start, end, precise=True):
                        scanned += 1
                        if scanned > MAX_MESSAGES:
                            raise SourceFailure("RESULT_LIMIT_EXCEEDED", "所选范围内消息过多，请缩小日期范围。")
                        try:
                            projected = self._row_to_message(
                                conversation, row, own_sender_id, relative, precise=True, strict_decode=True,
                            )
                            message = self._keyword_message_json(projected.message, projected.projection)
                            stamp = epoch_microseconds(row["create_time"])
                            rowid = int(row["physical_rowid"])
                        except (ValueError, TypeError, OverflowError):
                            raise SourceFailure("MESSAGE_DECODE_FAILED", "部分消息字段无法转换，关键词检索未完成。") from None
                        yield SearchDocument(
                            {"id": conversation.id, "title": conversation.name, "kind": conversation.kind.value},
                            message, (stamp, conversation.id, relative.casefold(), table.casefold(), rowid),
                            _LiveLocator(conversation.id, relative, table, rowid, stamp),
                            content_fingerprint(
                                *(row[name] for name in ("create_time", "local_id", "server_id", "local_type",
                                                        "real_sender_id", "message_content", "content_compression",
                                                        "packed_info_data", "sender_wxid")),
                                message["content"], message["sender"], message.get("transcript"), message.get("replyTo"),
                            ),
                            projected.parse_status, projected.content_truncated,
                            keyword_content=projected.message.content,
                        )
            self.validate_keyword_generations(generations)
        except MessageReadError as error:
            raise self._cursor_read_failure(error) from None
        except SourceError as error:
            raise self._map_source_error(error, phase="query") from None
        except sqlite3.Error:
            raise SourceFailure("PAGE_UNSUPPORTED_SCHEMA", "微信消息表结构不受支持，关键词检索未完成。") from None
        except OSError:
            raise SourceFailure("PAGE_SOURCE_UNAVAILABLE", "微信消息分片暂时无法读取，请重试。", True) from None

    # ------------------------------------------------------------------ #
    # Pure global-search projection and verified physical context
    # ------------------------------------------------------------------ #

    def _search_locations(self, conversation_id: str | None = None):
        grouped = {}
        owners = {}
        for conversation in self._conversations:
            if conversation_id is not None and conversation.id != conversation_id:
                continue
            for relative, table in self._unique_locations(conversation.id):
                physical = (relative.casefold(), table.casefold())
                if physical in owners and owners[physical] != conversation.id:
                    raise SourceFailure("SEARCH_UNSUPPORTED_SCHEMA", "消息表会话身份不明确，无法完整搜索。")
                owners[physical] = conversation.id
                grouped.setdefault(relative, []).append((conversation, table))
        return grouped

    def validate_search_generations(self, generations: dict) -> None:
        self._require_connected()
        if not hasattr(self._databases, "is_snapshot_current"):
            raise SourceFailure("SEARCH_SOURCE_CHANGED", "搜索数据版本不可验证，请重新搜索。")
        for relative, snapshot in generations.items():
            if not self._databases.is_snapshot_current(relative, snapshot):
                raise SourceFailure("SEARCH_SOURCE_CHANGED", "微信数据已变化，请重新搜索。", True)

    @staticmethod
    def _plain_message_json(
        message: Message, projection: AppMessageProjection | None = None,
    ) -> dict:
        result = {
            "id": message.id, "sender": message.sender,
            "timestamp": message.timestamp.isoformat(timespec="microseconds"),
            "kind": message.type.value, "content": message.content,
            "isOutgoing": message.is_outgoing,
        }
        if message.reply_to:
            result["replyTo"] = message.reply_to
        if message.duration_ms is not None:
            result["durationMs"] = message.duration_ms
        if projection is not None:
            result.update(app_message_projection_json(projection))
        # No media/cache reads or paths are part of this projection.
        return result

    def _search_document(self, conversation, relative, table, row, own_sender_id):
        try:
            projected = self._row_to_message(conversation, row, own_sender_id, relative,
                                             enrich_media=False, precise=True)
        except (ValueError, TypeError, OverflowError):
            raise SourceFailure("MESSAGE_DECODE_FAILED", "部分消息字段无法转换，搜索未完成。") from None
        message = self._plain_message_json(projected.message)
        projection_json = (
            app_message_projection_json(projected.projection)
            if projected.projection is not None else None
        )
        if projection_json is not None:
            message.update(projection_json)
        stamp = epoch_microseconds(row["create_time"])
        rowid = int(row["physical_rowid"])
        key = (stamp, conversation.id, relative, table, rowid)
        fingerprint = content_fingerprint(
            *(row[name] for name in ("create_time", "local_id", "server_id", "local_type",
                                    "real_sender_id", "content_compression", "message_content",
                                    "packed_info_data", "sender_wxid")),
            message["content"], message["sender"], message.get("replyTo"),
        )
        return SearchDocument(
            {"id": conversation.id, "title": conversation.name, "kind": conversation.kind.value,
             "messageCount": 0, "countKnown": False},
            message, key, _LiveLocator(
                conversation.id, relative, table, rowid, stamp,
                _ProjectionSlot(projection_json) if projection_json is not None else None,
            ), fingerprint, projected.parse_status, projected.content_truncated,
        )

    @staticmethod
    def _require_search_schema(schema) -> None:
        if schema.rowid_column is None or not schema.integer_values or schema.mixed_timestamp_units:
            raise SourceFailure("SEARCH_UNSUPPORTED_SCHEMA", "此消息表的时间单位或唯一行身份不明确，无法完整搜索和定位。")

    def iter_search_documents(self, start: int | None, end: int | None, *, deadline: float, generations: dict):
        self._require_connected()
        if not hasattr(self._databases, "get_snapshot"):
            raise SourceFailure("SEARCH_UNSUPPORTED_SCHEMA", "数据源不支持可验证的读取版本。")
        try:
            for relative, tables in self._search_locations().items():
                if time.monotonic() >= deadline:
                    raise SourceFailure("SEARCH_BUDGET_EXCEEDED", "搜索超过时间预算，请缩小日期范围。", True)
                snapshot = self._databases.get_snapshot(relative)
                generations[relative] = snapshot
                with SQLiteShardReader(snapshot.path, deadline=deadline) as reader:
                    own_sender_id = reader.own_sender_id(self._own_wxid)
                    for conversation, table in tables:
                        schema = reader.inspect_table(table)
                        self._require_search_schema(schema)
                        for row in reader.iter_rows(table, schema, start, end, precise=True):
                            yield self._search_document(conversation, relative, table, row, own_sender_id)
            # Each shard is a controlled snapshot, not a claim that every shard
            # was copied atomically at the same instant.
            if generations:
                self.validate_search_generations(generations)
        except MessageReadError as error:
            raise SourceFailure(error.code, error.message) from None
        except SourceError as error:
            raise self._map_source_error(error, phase="query") from None
        except OSError:
            raise SourceFailure("SEARCH_SOURCE_UNREADABLE", "部分消息分片无法读取，搜索未完成。", True) from None

    def get_search_context(self, hit: SearchHit, generations: dict, before: int, after: int) -> dict:
        self.validate_search_generations(generations)
        locator = hit.locator
        if not isinstance(locator, _LiveLocator):
            raise SourceFailure("CONTEXT_TARGET_CHANGED", "搜索定位已失效，请重新搜索。")
        conversation = next((item for item in self._conversations if item.id == locator.conversation_id), None)
        if conversation is None:
            raise SourceFailure("CONTEXT_TARGET_CHANGED", "原会话已不可读取，请重新搜索。")
        deadline = time.monotonic() + SOFT_DEADLINE_SECONDS
        try:
            with ExitStack() as stack:
                readers = {}
                schemas = {}
                own_ids = {}
                for relative, tables in self._search_locations(conversation.id).items():
                    if relative not in generations:
                        raise SourceFailure("SEARCH_SOURCE_CHANGED", "消息分片已变化，请重新搜索。")
                    reader = stack.enter_context(SQLiteShardReader(generations[relative].path, deadline=deadline))
                    readers[relative] = reader
                    own_ids[relative] = reader.own_sender_id(self._own_wxid)
                    for _, table in tables:
                        schema = reader.inspect_table(table)
                        self._require_search_schema(schema)
                        schemas[(relative, table)] = schema
                target_schema = schemas.get((locator.relative_path, locator.table))
                if target_schema is None:
                    raise SourceFailure("CONTEXT_TARGET_CHANGED", "原消息位置已不可读取，请重新搜索。")
                target_reader = readers[locator.relative_path]
                row = next(target_reader.iter_rows(locator.table, target_schema, None, None,
                                                   precise=True, physical_rowid=locator.physical_rowid), None)
                if row is None:
                    raise SourceFailure("CONTEXT_TARGET_CHANGED", "原消息已消失，请重新搜索。")
                target = self._search_document(conversation, locator.relative_path, locator.table, row, own_ids[locator.relative_path])
                if target.fingerprint != hit.fingerprint or target.sort_key != hit.sort_key:
                    raise SourceFailure("CONTEXT_TARGET_CHANGED", "原消息内容或身份已变化，请重新搜索。")
                # Only metadata is examined outside the chosen neighbours; no
                # XML conversion or media work is done for the rest of the chat.
                earlier, later = [], []
                earlier_count = later_count = scanned = 0
                for (relative, table), schema in schemas.items():
                    for candidate in readers[relative].iter_rows(table, schema, None, None, precise=True, metadata_only=True):
                        scanned += 1
                        if scanned > MAX_SCAN_ROWS:
                            raise SourceFailure("SEARCH_BUDGET_EXCEEDED", "上下文扫描超过预算，请缩小数据范围。")
                        rowid = int(candidate["physical_rowid"])
                        key = (epoch_microseconds(candidate["create_time"]), conversation.id, relative, table, rowid)
                        entry = (key, relative, table, rowid)
                        if key < hit.sort_key:
                            earlier_count += 1
                            earlier.append(entry)
                            earlier.sort(reverse=True)
                            del earlier[before:]
                        elif key > hit.sort_key:
                            later_count += 1
                            later.append(entry)
                            later.sort()
                            del later[after:]
                def render(entries):
                    messages = []
                    for _, relative, table, rowid in sorted(entries):
                        raw = next(readers[relative].iter_rows(table, schemas[(relative, table)], None, None,
                                                             precise=True, physical_rowid=rowid), None)
                        if raw is None:
                            raise SourceFailure("CONTEXT_TARGET_CHANGED", "上下文消息已变化，请重新搜索。")
                        messages.append(self._search_document(conversation, relative, table, raw, own_ids[relative]).message)
                    return messages
                response = bounded_context(target.conversation, render(earlier), target.message, render(later),
                                           earlier_count > len(earlier), later_count > len(later))
                self.validate_search_generations(generations)
                return response
        except MessageReadError as error:
            raise SourceFailure(error.code, error.message) from None
        except sqlite3.Error:
            raise SourceFailure("CONTEXT_TARGET_CHANGED", "原消息位置已不可读取，请重新搜索。") from None

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
        finally:
            self._media_revision += 1

    def _require_connected(self) -> None:
        if not self._connected or self._databases is None:
            raise SourceFailure("WECHAT_NOT_CONNECTED", "尚未连接微信。请先选择账户并连接。")

    def close(self) -> None:
        self._page_generation += 1
        self._cursor_sessions.clear()
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


def _decode_content(value: object, compression: object, *, strict: bool = False) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        if len(value) > MAX_RAW_MESSAGE_BYTES or len(value.encode("utf-8")) > MAX_RAW_MESSAGE_BYTES:
            raise SourceFailure("MESSAGE_TOO_LARGE", "单条消息内容超过安全读取上限。")
        return value
    if isinstance(value, memoryview):
        value = value.tobytes()
    if not isinstance(value, bytes):
        return str(value)
    data = value
    if len(data) > MAX_RAW_MESSAGE_BYTES:
        raise SourceFailure("MESSAGE_TOO_LARGE", "单条消息内容超过安全读取上限。")
    if int(compression or 0) == 4 or data.startswith(b"\x28\xb5\x2f\xfd"):
        try:
            # stream_reader also handles unknown-size/concatenated frames. A
            # fixed read budget applies even when the frame advertises a size.
            with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(data), read_across_frames=True) as reader:
                chunks = []
                size = 0
                while True:
                    chunk = reader.read(min(64 * 1024, MAX_RAW_MESSAGE_BYTES + 1 - size))
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_RAW_MESSAGE_BYTES:
                        raise SourceFailure("MESSAGE_TOO_LARGE", "单条消息解压内容超过安全读取上限。")
                    chunks.append(chunk)
                data = b"".join(chunks)
        except zstandard.ZstdError:
            if strict:
                raise SourceFailure("MESSAGE_DECODE_FAILED", "消息解压失败，无法保证完整搜索。") from None
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
    """Compatibility helper; canonical card text has exactly one parser."""
    return parse_appmsg(content, raw_type).canonical_text


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
