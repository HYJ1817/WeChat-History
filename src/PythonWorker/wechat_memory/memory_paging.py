"""Opaque cursor pages over validated immutable demo/offline message tuples.

The host owns validation and source activation. This adapter keeps references to
that exact dataset, not a per-query copy; sessions contain only range indices.
All token, TTL, eviction and generation checks use the common session manager.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Generic, Protocol, TypeVar

from .page_sessions import (
    CursorPage, PageSessionError, PageSessionLimits, PageSessionManager,
    PageWindow, PreparedPage,
)


class _TimedMessage(Protocol):
    @property
    def timestamp(self) -> datetime: ...


Item = TypeVar("Item", bound=_TimedMessage)


class _Conversation(Protocol[Item]):
    @property
    def id(self) -> str: ...

    @property
    def messages(self) -> tuple[Item, ...]: ...


@dataclass(frozen=True, slots=True)
class _MemoryCursorState:
    conversation_id: str
    lower: int
    upper: int
    order: str


class ImmutableMessagePageProvider(Generic[Item]):
    """Slice one host-validated, stably timestamp-sorted immutable version."""

    capabilities = {"message_cursor_pagination": True}

    def __init__(
        self, conversations: tuple[_Conversation[Item], ...], *, limits: PageSessionLimits | None = None,
    ) -> None:
        if not isinstance(conversations, tuple):
            raise TypeError("conversations must be an immutable tuple")
        self._conversations = conversations
        self._generation = 0
        self._page_sessions: PageSessionManager[_MemoryCursorState, int, Item] = (
            PageSessionManager(limits=limits)
        )

    @property
    def generation(self) -> int:
        return self._generation

    def _replace_conversations(self, conversations: tuple[_Conversation[Item], ...]) -> None:
        if not isinstance(conversations, tuple):
            raise TypeError("conversations must be an immutable tuple")
        self._conversations = conversations
        self._generation += 1

    def invalidate(self) -> None:
        self._generation += 1

    def close(self) -> None:
        self._generation += 1
        self._page_sessions.clear()

    def get_messages_page(
        self, conversation_id: str, *, source_session_id: str,
        start_epoch: int | None, end_epoch_exclusive: int | None,
        order: str = "desc", limit: int = 50,
    ) -> CursorPage[Item]:
        return self._page_sessions.first_page(
            self, conversation_id, source_session_id=source_session_id,
            start_epoch=start_epoch, end_epoch_exclusive=end_epoch_exclusive,
            order=order, limit=limit,
        )

    def get_messages_cursor_page(
        self, source_session_id: str, page_session_id: str, cursor: str, *, limit: int = 50,
    ) -> CursorPage[Item]:
        return self._page_sessions.cursor_page(
            self, source_session_id, page_session_id, cursor, limit=limit,
        )

    def prepare_page(
        self, conversation_id: str, start_epoch: int | None,
        end_epoch_exclusive: int | None, order: str,
    ) -> PreparedPage[_MemoryCursorState]:
        conversation = self._find_conversation(conversation_id)
        if conversation is None:
            raise PageSessionError("CONVERSATION_NOT_FOUND", "找不到指定的会话。")
        messages = conversation.messages
        # Integer microseconds avoid float rounding, including epoch extremes.
        lower = 0 if start_epoch is None else _lower_bound(messages, start_epoch * 1_000_000)
        upper = len(messages) if end_epoch_exclusive is None else _lower_bound(
            messages, end_epoch_exclusive * 1_000_000,
        )
        return PreparedPage(_MemoryCursorState(conversation_id, lower, upper, order), upper - lower)

    def validate_page(self, state: _MemoryCursorState) -> None:
        # Frozen source models/tuples cannot change in place. The manager checks
        # our generation before/after each window. No file or content re-read.
        pass

    def read_page(
        self, state: _MemoryCursorState, boundary: int | None, direction: str, limit: int,
    ) -> PageWindow[Item, int]:
        conversation = self._find_conversation(state.conversation_id)
        if conversation is None:
            raise PageSessionError("PAGE_SOURCE_CHANGED", "分页数据源已变化，请从首屏刷新。", True)
        step = 1 if state.order == "asc" else -1
        if direction == "first":
            start = state.lower if step == 1 else state.upper - 1
        else:
            assert boundary is not None
            if direction == "before":
                step = -step
            start = boundary + step
        stop = state.upper if step == 1 else state.lower - 1
        indices = range(start, stop, step)[:limit + 1]
        selected = indices[:limit]
        if direction == "before":
            selected = selected[::-1]
        return PageWindow(
            tuple(conversation.messages[index] for index in selected),
            selected[0] if selected else None, selected[-1] if selected else None,
            len(indices) > limit,
        )

    def _find_conversation(self, conversation_id: str) -> _Conversation[Item] | None:
        return next((item for item in self._conversations if item.id == conversation_id), None)


def _lower_bound(messages: tuple[_TimedMessage, ...], timestamp_microseconds: int) -> int:
    lower, upper = 0, len(messages)
    while lower < upper:
        middle = (lower + upper) // 2
        if _datetime_microseconds(messages[middle].timestamp) < timestamp_microseconds:
            lower = middle + 1
        else:
            upper = middle
    return lower


def _datetime_microseconds(value: datetime) -> int:
    utc_value = value.astimezone(timezone.utc)
    delta = utc_value - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
