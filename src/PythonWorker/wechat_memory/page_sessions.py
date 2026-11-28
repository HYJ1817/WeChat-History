"""Source-independent, opaque message-page sessions and provider contract.

Providers prepare traversal metadata, verify their source generation, and read
one bounded window. Only this module issues/validates tokens, creates previous
and next cursors, expires sessions, and evicts retained lifecycle state. It does
not retain datasets or depend on SQLite, imported JSON, or protocol dispatch.
"""
from __future__ import annotations

import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Generic, Protocol, TypeVar

PAGE_SESSION_TTL_SECONDS = 10 * 60
MAX_PAGE_SESSIONS = 64
MAX_PAGE_TOKENS_PER_SESSION = 512
MAX_CURSOR_PAGE_SIZE = 100

State = TypeVar("State")
Key = TypeVar("Key")
Item = TypeVar("Item")


class PageSessionError(Exception):
    """A fixed, source-independent pagination failure with no data/path detail."""

    def __init__(self, code: str, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class PageSessionLimits:
    ttl_seconds: float = PAGE_SESSION_TTL_SECONDS
    max_sessions: int = MAX_PAGE_SESSIONS
    max_tokens_per_session: int = MAX_PAGE_TOKENS_PER_SESSION
    max_page_size: int = MAX_CURSOR_PAGE_SIZE

    def __post_init__(self) -> None:
        if self.ttl_seconds < 0 or any(
            type(value) is not int or value < 1
            for value in (self.max_sessions, self.max_tokens_per_session, self.max_page_size)
        ):
            raise ValueError("page-session limits must be positive (TTL may be zero)")


@dataclass(frozen=True, slots=True)
class CursorPage(Generic[Item]):
    items: tuple[Item, ...]
    total: int
    page_session_id: str
    previous_cursor: str | None
    next_cursor: str | None
    sort_version: str = "precise-v1"


@dataclass(frozen=True, slots=True)
class PreparedPage(Generic[State]):
    state: State
    total: int


@dataclass(frozen=True, slots=True)
class PageWindow(Generic[Item, Key]):
    """One provider window, in requested order, plus directional lookahead.

    ``has_more`` describes the traversal direction: later for first/after,
    earlier for before. ``first_key`` and ``last_key`` always describe the
    returned items' requested order, including on a backwards traversal.
    """

    items: tuple[Item, ...]
    first_key: Key | None
    last_key: Key | None
    has_more: bool


class PageProvider(Protocol[State, Key, Item]):
    """Implement this contract to reuse the lifecycle for any stable source.

    ``generation`` must change on source switch/re-import/reconnect. Per-query
    generation verification (e.g. SQLite shard signatures) lives in
    ``validate_page``. State and keys must be metadata, not another dataset
    cache. Provider failures use ``PageSessionError`` or a subclass.
    """

    @property
    def generation(self) -> object: ...

    def prepare_page(
        self, conversation_id: str, start_epoch: int | None,
        end_epoch_exclusive: int | None, order: str,
    ) -> PreparedPage[State]: ...

    def validate_page(self, state: State) -> None: ...

    def read_page(
        self, state: State, boundary: Key | None, direction: str, limit: int,
    ) -> PageWindow[Item, Key]: ...


@dataclass(frozen=True, slots=True)
class _Boundary(Generic[Key]):
    value: Key
    direction: str


@dataclass(slots=True)
class _Session(Generic[State, Key]):
    page_session_id: str
    source_session_id: str
    generation: object
    state: State
    total: int
    last_access: float
    cursors: OrderedDict[str, _Boundary[Key]] = field(default_factory=OrderedDict)


class PageSessionManager(Generic[State, Key, Item]):
    """One bounded lifecycle implementation shared by all message providers."""

    def __init__(self, *, limits: PageSessionLimits | None = None) -> None:
        self.limits = limits if limits is not None else PageSessionLimits()
        self._sessions: OrderedDict[str, _Session[State, Key]] = OrderedDict()
        self._lock = threading.RLock()

    def first_page(
        self,
        provider: PageProvider[State, Key, Item],
        conversation_id: str,
        *,
        source_session_id: str,
        start_epoch: int | None,
        end_epoch_exclusive: int | None,
        order: str = "desc",
        limit: int = 50,
        limits: PageSessionLimits | None = None,
    ) -> CursorPage[Item]:
        policy = self.limits if limits is None else limits
        validate_page_request(source_session_id, start_epoch, end_epoch_exclusive, order, limit,
                              max_page_size=policy.max_page_size)
        if not isinstance(conversation_id, str) or not conversation_id:
            raise PageSessionError("INVALID_ARGUMENT", "conversationId 无效。")
        with self._lock:
            self._purge(time.monotonic(), policy.ttl_seconds)
            generation = provider.generation
            prepared = provider.prepare_page(conversation_id, start_epoch, end_epoch_exclusive, order)
            session = _Session(
                secrets.token_urlsafe(18), source_session_id, generation,
                prepared.state, prepared.total, time.monotonic(),
            )
            # Failed reads are never registered or published.
            page = self._read_page(provider, session, None, "first", limit, policy)
            session.last_access = time.monotonic()
            self._sessions[session.page_session_id] = session
            while len(self._sessions) > policy.max_sessions:
                self._sessions.popitem(last=False)
            return page

    def cursor_page(
        self,
        provider: PageProvider[State, Key, Item],
        source_session_id: str,
        page_session_id: str,
        cursor: str,
        *,
        limit: int = 50,
        limits: PageSessionLimits | None = None,
    ) -> CursorPage[Item]:
        policy = self.limits if limits is None else limits
        validate_page_request(source_session_id, None, None, "asc", limit,
                              max_page_size=policy.max_page_size)
        if not isinstance(page_session_id, str) or not page_session_id or not isinstance(cursor, str) or not cursor:
            raise PageSessionError("PAGE_EXPIRED", "分页已失效，请从首屏刷新。", True)
        with self._lock:
            now = time.monotonic()
            self._purge(now, policy.ttl_seconds)
            session = self._sessions.get(page_session_id)
            if session is None or session.source_session_id != source_session_id:
                raise PageSessionError("PAGE_EXPIRED", "分页已失效，请从首屏刷新。", True)
            boundary = session.cursors.get(cursor)
            if boundary is None:
                raise PageSessionError("PAGE_EXPIRED", "分页游标已失效，请从首屏刷新。", True)
            session.cursors.move_to_end(cursor)
            self._sessions.move_to_end(page_session_id)
            try:
                page = self._read_page(provider, session, boundary.value, boundary.direction, limit, policy)
            except PageSessionError as error:
                if error.code == "PAGE_SOURCE_CHANGED":
                    self._sessions.pop(page_session_id, None)
                raise
            session.last_access = time.monotonic()
            return page

    def _read_page(
        self,
        provider: PageProvider[State, Key, Item],
        session: _Session[State, Key],
        boundary: Key | None,
        direction: str,
        limit: int,
        policy: PageSessionLimits,
    ) -> CursorPage[Item]:
        self._validate_generation(provider, session)
        provider.validate_page(session.state)
        window = provider.read_page(session.state, boundary, direction, limit)
        provider.validate_page(session.state)
        self._validate_generation(provider, session)
        if session.total and not window.items:
            raise PageSessionError("PAGE_SOURCE_CHANGED", "分页位置已失效，请从首屏刷新。", True)
        if len(window.items) > limit or (
            window.items and (window.first_key is None or window.last_key is None)
        ):
            raise ValueError("provider returned an invalid page window")
        previous_cursor = next_cursor = None
        if window.items:
            if direction == "after":
                previous_cursor = self._remember_cursor(session, window.first_key, "before", policy)
            elif direction == "before":
                next_cursor = self._remember_cursor(session, window.last_key, "after", policy)
            if window.has_more:
                if direction == "before":
                    previous_cursor = self._remember_cursor(session, window.first_key, "before", policy)
                else:
                    next_cursor = self._remember_cursor(session, window.last_key, "after", policy)
        return CursorPage(
            window.items, session.total, session.page_session_id,
            previous_cursor, next_cursor,
        )

    @staticmethod
    def _validate_generation(
        provider: PageProvider[State, Key, Item], session: _Session[State, Key],
    ) -> None:
        if provider.generation != session.generation:
            raise PageSessionError("PAGE_SOURCE_CHANGED", "分页数据源已变化，请从首屏刷新。", True)

    @staticmethod
    def _remember_cursor(
        session: _Session[State, Key], value: Key, direction: str, policy: PageSessionLimits,
    ) -> str:
        token = secrets.token_urlsafe(24)
        session.cursors[token] = _Boundary(value, direction)
        while len(session.cursors) > policy.max_tokens_per_session:
            session.cursors.popitem(last=False)
        return token

    def invalidate_session(self, page_session_id: str) -> None:
        with self._lock:
            self._sessions.pop(page_session_id, None)

    def invalidate_all(self) -> None:
        with self._lock:
            self._sessions.clear()

    def clear(self) -> None:
        """Release every session; preserves the live source's cleanup API."""
        self.invalidate_all()

    def _purge(self, now: float, ttl_seconds: float) -> None:
        for session_id, session in tuple(self._sessions.items()):
            if now - session.last_access >= ttl_seconds:
                self._sessions.pop(session_id, None)


def validate_page_request(
    source_session_id: str,
    start: int | None,
    end: int | None,
    order: str,
    limit: int,
    *,
    max_page_size: int = MAX_CURSOR_PAGE_SIZE,
) -> None:
    if not isinstance(source_session_id, str) or not source_session_id.strip() or len(source_session_id) > 256:
        raise PageSessionError("INVALID_ARGUMENT", "sourceSessionId 无效。")
    if any(value is not None and type(value) is not int for value in (start, end)):
        raise PageSessionError("INVALID_ARGUMENT", "分页日期无效。")
    if start is not None and end is not None and start >= end:
        raise PageSessionError("INVALID_ARGUMENT", "分页日期范围无效。")
    if not isinstance(order, str) or order not in {"asc", "desc"}:
        raise PageSessionError("INVALID_ARGUMENT", "order 必须是 asc 或 desc。")
    if type(limit) is not int or not 1 <= limit <= max_page_size:
        raise PageSessionError("INVALID_ARGUMENT", f"limit 必须在 1 到 {max_page_size} 之间。")
