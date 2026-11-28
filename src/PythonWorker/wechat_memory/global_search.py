"""Memory-only global search and complete keyword-pagination snapshots.

Only complete scans publish a session. A keeps bounded Top-K summaries; keyword
pagination retains full matched display messages under the same memory ledger.
Neither writes a plaintext index. The source owns canonical/media projection,
physical identity verification, date-range selection and generation checks.
"""
from __future__ import annotations

import copy
import hashlib
import heapq
import hmac
import json
import secrets
import time
import weakref
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from .appmsg_parser import APPMSG_PARSER_VERSION

SORT_VERSION = "precise-v1"
PROJECTION_VERSION = "canonical-" + APPMSG_PARSER_VERSION
RESULT_LIMIT = 1_000
MAX_SCAN_ROWS = 2_000_000
SESSION_TTL_SECONDS = 600
QUERY_CACHE_BYTES = 32 * 1024 * 1024
MAX_SEARCH_SESSIONS = 4
KEYWORD_CANDIDATE_LIMIT = 200_000
KEYWORD_PAGE_TTL_SECONDS = 600
MAX_KEYWORD_PAGE_SESSIONS = 4
MAX_KEYWORD_PAGE_SIZE = 100
KEYWORD_SESSION_PREFIX = "keyword-"
SOFT_DEADLINE_SECONDS = 20
MAX_RESPONSE_BYTES = 1024 * 1024
RESPONSE_ENVELOPE_RESERVE = 4 * 1024
MAX_TEXT_BYTES = 2 * 1024 * 1024
MAX_QUERY_BYTES = 4 * 1024
SNIPPET_CHARACTERS = 240


class SearchError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class SearchDocument:
    conversation: dict[str, Any]
    message: dict[str, Any]
    sort_key: tuple[int, str, str, str, int]
    locator: Any
    fingerprint: str
    parse_status: str = "ok"
    content_truncated: bool = False
    # Legacy preview predicates can see a recovered image path (or imported
    # metadata) while its wire display is a safe placeholder. This predicate-
    # only value is never retained in A hits or keyword display snapshots.
    keyword_content: str | None = None


@dataclass(frozen=True, slots=True)
class SearchHit:
    item: dict[str, Any]
    locator: Any
    fingerprint: str
    sort_key: tuple[int, str, str, str, int]


@dataclass(frozen=True, slots=True)
class SearchSession:
    search_id: str
    source: str
    source_session_id: str
    hits: tuple[SearchHit, ...]
    total: int
    coverage: dict[str, Any]
    generations: Any
    expires_at: float
    cache_bytes: int
    sort_version: str = SORT_VERSION
    projection_version: str = PROJECTION_VERSION
    criteria_fingerprint: str = ""


@dataclass(frozen=True, slots=True)
class KeywordSearchCriteria:
    """Server-owned inputs that change a keyword match set or projection.

    media_selection must include any transcript/configuration revision, or its
    owner must clear the cache when media is explicitly regenerated. A new
    request's client-supplied conditions must not replace these on continuation.
    """

    query: str
    start_date: str | None
    end_date: str | None
    order: str
    projection_version: str = field(default_factory=lambda: PROJECTION_VERSION)
    media_selection: Any = None
    sort_version: str = field(default_factory=lambda: SORT_VERSION)
    conversation_id: str | None = None


@dataclass(frozen=True, slots=True)
class KeywordPageEntry:
    """A complete display message and the identity needed to verify its row."""

    conversation: dict[str, Any]
    message: dict[str, Any]
    sort_key: tuple[Any, ...]
    locator: Any
    fingerprint: str


@dataclass(frozen=True, slots=True)
class KeywordPageSession:
    page_session_id: str
    source: str
    source_session_id: str
    entries: tuple[KeywordPageEntry, ...]
    total: int
    candidate_count: int
    order: str
    generations_fingerprint: str
    criteria_fingerprint: str
    expires_at: float
    cache_bytes: int
    sort_version: str
    projection_version: str


@dataclass(frozen=True, slots=True)
class KeywordPage:
    page_session_id: str
    source_session_id: str
    items: tuple[KeywordPageEntry, ...]
    offset: int
    total: int
    order: str
    previous_offset: int | None
    next_offset: int | None


def encoded_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def response_fits(value: Any) -> bool:
    return encoded_size(value) <= MAX_RESPONSE_BYTES - RESPONSE_ENVELOPE_RESERVE


def datetime_microseconds(value: datetime) -> int:
    """Integer arithmetic also preserves imported subsecond precision."""
    delta = value.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def content_fingerprint(*values: Any) -> str:
    digest = hashlib.sha256()
    for value in values:
        if isinstance(value, memoryview):
            value = value.tobytes()
        if value is None:
            payload = b"N"
        elif isinstance(value, bytes):
            payload = b"B" + value
        else:
            payload = (type(value).__name__ + ":" + str(value)).encode("utf-8", errors="replace")
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def _criteria_fingerprint(criteria: KeywordSearchCriteria) -> str:
    if not isinstance(criteria.query, str):
        raise SearchError("INVALID_ARGUMENT", "关键词必须是字符串。")
    if criteria.order not in {"asc", "desc"}:
        raise SearchError("INVALID_ARGUMENT", "order 必须是 asc 或 desc。")
    for value in (criteria.start_date, criteria.end_date):
        if value is not None and not isinstance(value, str):
            raise SearchError("INVALID_ARGUMENT", "日期条件必须是字符串或空值。")
    if not isinstance(criteria.projection_version, str) or not criteria.projection_version:
        raise SearchError("INVALID_ARGUMENT", "消息投影版本无效。")
    if not isinstance(criteria.sort_version, str) or not criteria.sort_version:
        raise SearchError("INVALID_ARGUMENT", "消息排序版本无效。")
    if criteria.conversation_id is not None and not isinstance(criteria.conversation_id, str):
        raise SearchError("INVALID_ARGUMENT", "会话身份无效。")
    try:
        media = json.dumps(criteria.media_selection, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), default=lambda value: repr(value))
    except (TypeError, ValueError, RecursionError):
        raise SearchError("INVALID_ARGUMENT", "媒体选择条件无法绑定到分页会话。") from None
    return content_fingerprint(
        criteria.query, criteria.start_date, criteria.end_date, criteria.order,
        criteria.projection_version, criteria.sort_version, criteria.conversation_id, media,
    )


def _generation_fingerprint(generations: Any) -> str:
    try:
        encoded = json.dumps(generations, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), default=lambda value: repr(value))
    except (TypeError, ValueError, RecursionError):
        encoded = repr(generations)
    return content_fingerprint(encoded)


def _snippet(content: str, needle: str) -> str:
    # Matching always happens on the full canonical text before this clipping.
    # casefold can expand characters; the approximate display position is not
    # used as a locator or as proof of a match.
    folded_position = content.casefold().find(needle)
    start = max(0, min(len(content), folded_position) - 60) if folded_position >= 0 else 0
    text = content[start:start + SNIPPET_CHARACTERS]
    return ("…" if start else "") + text + ("…" if start + len(text) < len(content) else "")


class _SearchMemoryBudget:
    """One allowance for retained sessions, Top-K and full keyword snapshots.

    Weak ownership also releases abandoned scan results/caches. The worker
    serializes requests; this ledger does not add a second cache or scan limit.
    """

    def __init__(self) -> None:
        self._reservations: weakref.WeakSet[_MemoryReservation] = weakref.WeakSet()
        # A and keyword-page caches participate in the same 32 MiB ledger.
        self._caches: weakref.WeakSet[Any] = weakref.WeakSet()

    @property
    def bytes_used(self) -> int:
        return sum(reservation.bytes_used for reservation in self._reservations)

    def resize(self, reservation: _MemoryReservation, size: int) -> None:
        if size > QUERY_CACHE_BYTES:
            raise SearchError("SEARCH_CACHE_LIMIT", "搜索结果缓存超过预算，请缩小日期范围。")
        if self.bytes_used - reservation.bytes_used + size > QUERY_CACHE_BYTES:
            for cache in self._caches:
                cache.purge()
            # Other scans/completed-but-unpublished results cannot be evicted.
            # Do not destroy good sessions if even evicting all would not fit.
            reclaimable = sum(cache.bytes_used for cache in self._caches)
            if self.bytes_used - reservation.bytes_used - reclaimable + size > QUERY_CACHE_BYTES:
                raise SearchError("SEARCH_CACHE_LIMIT", "搜索结果缓存超过预算，请缩小日期范围。")
            while self.bytes_used - reservation.bytes_used + size > QUERY_CACHE_BYTES:
                cache = min(
                    (cache for cache in self._caches if cache.sessions),
                    key=lambda cache: next(iter(cache.sessions.values())).expires_at,
                )
                cache.discard(next(iter(cache.sessions)))
        reservation.bytes_used = size


class _MemoryReservation:
    def __init__(self, budget: _SearchMemoryBudget) -> None:
        self.budget = budget
        self.bytes_used = 0
        budget._reservations.add(self)

    def resize(self, size: int) -> None:
        self.budget.resize(self, size)

    def release(self) -> None:
        self.bytes_used = 0


_QUERY_MEMORY = _SearchMemoryBudget()
_SESSION_OVERHEAD_BYTES = 4096


class _ScanHits(tuple):
    """Keep a scan charged until publication, failure, or caller abandonment."""

    reservation: _MemoryReservation | None

    def __new__(cls, hits: Iterable[SearchHit], reservation: _MemoryReservation):
        result = super().__new__(cls, hits)
        result.reservation = reservation
        return result


def _hit_cache_bytes(hit: SearchHit) -> int:
    try:
        payload_bytes = (
            encoded_size(hit.item) + encoded_size(hit.sort_key)
            + len(repr(hit.locator).encode("utf-8", errors="replace"))
            + len(hit.fingerprint.encode("utf-8", errors="replace"))
        )
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise SearchError("INVALID_SEARCH_DOCUMENT", "搜索结果包含不受支持的定位或展示数据。") from None
    # The source's dataclass locator may hold a complete optional C3 projection
    # until publication moves it into item. Charge that sidecar *before* a hit
    # enters/replaces a Top-K slot, using the same conservative Unicode/object
    # allowance as B3. A fixed locator allowance alone misses large forwards.
    return 4 * payload_bytes + 2048


class _HeapEntry:
    __slots__ = ("hit", "ascending", "cache_bytes")

    def __init__(self, hit: SearchHit, ascending: bool) -> None:
        self.hit = hit
        self.ascending = ascending
        self.cache_bytes = _hit_cache_bytes(hit)

    def __lt__(self, other: "_HeapEntry") -> bool:
        # The root is the worst retained result (largest for asc, smallest for
        # desc), so a later shard's better result can replace it.
        return self.hit.sort_key > other.hit.sort_key if self.ascending else self.hit.sort_key < other.hit.sort_key


def scan_documents(
    documents: Iterable[SearchDocument], query: str, order: str, *,
    deadline: float, result_limit: int = RESULT_LIMIT, max_scan_rows: int = MAX_SCAN_ROWS,
) -> tuple[tuple[SearchHit, ...], int, dict[str, Any]]:
    needle = query.casefold()
    heap: list[_HeapEntry] = []
    total = scanned = partial = truncated = retained_bytes = 0
    ascending = order == "asc"
    reservation = _MemoryReservation(_QUERY_MEMORY)
    try:
        iterator = iter(documents)
        try:
            reservation.resize(_SESSION_OVERHEAD_BYTES)
            for document in iterator:
                if time.monotonic() >= deadline:
                    raise SearchError("SEARCH_BUDGET_EXCEEDED", "搜索超过时间预算，请缩小日期范围后重试。", True)
                scanned += 1
                if scanned > max_scan_rows:
                    raise SearchError("SEARCH_BUDGET_EXCEEDED", "搜索消息数量超过预算，请缩小日期范围。")
                if document.parse_status != "ok" or document.content_truncated:
                    partial += 1
                if document.content_truncated:
                    truncated += 1
                message = document.message
                fields = {name: str(message.get(name) or "") for name in ("sender", "content", "kind", "transcript")}
                if any(len(value) > MAX_TEXT_BYTES or len(value.encode("utf-8")) > MAX_TEXT_BYTES for value in fields.values()):
                    raise SearchError("MESSAGE_TOO_LARGE", "单条消息内容超过安全搜索上限。")
                matched = [name for name, value in fields.items() if needle in value.casefold()]
                if not matched:
                    continue
                total += 1
                conversation = document.conversation
                snippet_source = fields["transcript"] if "transcript" in matched and "content" not in matched else fields["content"]
                item = {
                    "hitId": secrets.token_urlsafe(24),
                    "conversationId": conversation["id"],
                    "conversationTitle": conversation["title"],
                    "conversationKind": conversation["kind"],
                    "messageId": message["id"],
                    "sender": message["sender"],
                    "timestamp": message["timestamp"],
                    "kind": message["kind"],
                    "snippet": _snippet(snippet_source, needle),
                    "matchedFields": matched,
                }
                hit = SearchHit(item, document.locator, document.fingerprint, document.sort_key)
                entry = _HeapEntry(hit, ascending)
                if len(heap) < result_limit:
                    reservation.resize(_SESSION_OVERHEAD_BYTES + retained_bytes + entry.cache_bytes)
                    heapq.heappush(heap, entry)
                    retained_bytes += entry.cache_bytes
                elif (hit.sort_key < heap[0].hit.sort_key if ascending else hit.sort_key > heap[0].hit.sort_key):
                    size = retained_bytes + entry.cache_bytes - heap[0].cache_bytes
                    reservation.resize(_SESSION_OVERHEAD_BYTES + size)
                    heapq.heapreplace(heap, entry)
                    retained_bytes = size
        finally:
            close = getattr(iterator, "close", None)
            if close is not None:
                close()
        if time.monotonic() >= deadline:
            raise SearchError("SEARCH_BUDGET_EXCEEDED", "搜索超过时间预算，请缩小日期范围后重试。", True)
        hits = _ScanHits(sorted((entry.hit for entry in heap), key=lambda hit: hit.sort_key, reverse=not ascending), reservation)
        coverage = {
            "complete": partial == 0,
            "scannedMessages": scanned,
            "partialMessages": partial,
            "truncatedMessages": truncated,
            "warnings": ["部分复杂消息未完整展开；搜索仅覆盖规范可读正文。"] if partial else [],
        }
        return hits, total, coverage
    except BaseException:
        reservation.release()
        raise


class SearchCache:
    """Retained sessions share the same allowance as every in-flight scan."""

    def __init__(self) -> None:
        self.sessions: OrderedDict[str, SearchSession] = OrderedDict()
        self._reservations: dict[str, _MemoryReservation] = {}
        self.bytes_used = 0
        _QUERY_MEMORY._caches.add(self)

    def clear(self) -> None:
        for reservation in self._reservations.values():
            reservation.release()
        self._reservations.clear()
        self.sessions.clear()
        self.bytes_used = 0

    def discard(self, search_id: str) -> None:
        old = self.sessions.pop(search_id, None)
        if old is not None:
            self._reservations.pop(search_id).release()
            self.bytes_used -= old.cache_bytes

    def purge(self) -> None:
        now = time.monotonic()
        for key, session in list(self.sessions.items()):
            if session.expires_at <= now:
                self.discard(key)

    def create(self, source: str, source_session_id: str, hits: tuple[SearchHit, ...], total: int,
               coverage: dict[str, Any], generations: Any, *, criteria_fingerprint: str = "") -> SearchSession:
        reservation = hits.reservation if isinstance(hits, _ScanHits) else None
        if reservation is None:
            reservation = _MemoryReservation(_QUERY_MEMORY)
        try:
            self.purge()
            size = sum(_hit_cache_bytes(hit) for hit in hits) + _SESSION_OVERHEAD_BYTES + 4 * len(repr(generations))
            # Resize the scan's existing charge, rather than charging a second
            # copy or releasing it before the retained session is published.
            reservation.resize(size)
            session = SearchSession(secrets.token_urlsafe(24), source, source_session_id, tuple(hits), total, coverage,
                                    generations, time.monotonic() + SESSION_TTL_SECONDS, size,
                                    sort_version=SORT_VERSION, projection_version=PROJECTION_VERSION,
                                    criteria_fingerprint=criteria_fingerprint)
            while self.sessions and len(self.sessions) >= MAX_SEARCH_SESSIONS:
                self.discard(next(iter(self.sessions)))
            self.sessions[session.search_id] = session
            self._reservations[session.search_id] = reservation
            self.bytes_used += size
            if isinstance(hits, _ScanHits):
                # A second publication of these hits must acquire its own
                # reservation instead of silently sharing session ownership.
                hits.reservation = None
            return session
        except BaseException:
            reservation.release()
            raise

    def get(self, search_id: str, source: str, source_session_id: str) -> SearchSession:
        self.purge()
        session = self.sessions.get(search_id)
        if session is None:
            raise SearchError("SEARCH_EXPIRED", "搜索结果已过期或已被释放，请重新搜索。")
        if session.source != source or session.source_session_id != source_session_id:
            raise SearchError("SOURCE_SESSION_CHANGED", "数据源已切换，请重新搜索。")
        if session.sort_version != SORT_VERSION or session.projection_version != PROJECTION_VERSION:
            self.discard(search_id)
            raise SearchError("SEARCH_EXPIRED", "消息投影版本已更新，请重新搜索。")
        return session


class _KeywordEntries(tuple):
    """Keep complete matched display entries charged through publication."""

    reservation: _MemoryReservation | None
    query: str
    order: str
    candidate_count: int

    def __new__(cls, entries: Iterable[KeywordPageEntry], reservation: _MemoryReservation,
                query: str, order: str, candidate_count: int):
        result = super().__new__(cls, entries)
        result.reservation = reservation
        result.query = query
        result.order = order
        result.candidate_count = candidate_count
        return result


def _keyword_matches(message: dict[str, Any], needle: str, content: str | None = None) -> bool:
    """Match legacy fields against a casefolded query, after media projection."""
    if not needle:
        return True
    kind = message.get("kind", message.get("type", ""))
    if hasattr(kind, "value"):
        kind = kind.value
    # A recovered voice transcript is already the rendered content in the
    # existing Message projection. Do not separately search hidden metadata.
    values = (message.get("sender", ""), message.get("content", "") if content is None else content, kind)
    return any(needle in str(value or "").casefold() for value in values)


def _copy_keyword_entry(document: SearchDocument) -> KeywordPageEntry:
    try:
        return KeywordPageEntry(
            copy.deepcopy(document.conversation), copy.deepcopy(document.message),
            tuple(document.sort_key), copy.deepcopy(document.locator), str(document.fingerprint),
        )
    except Exception as error:
        raise SearchError("INVALID_SEARCH_DOCUMENT", "关键词搜索结果无法建立稳定快照。") from error


def _keyword_entry_cache_bytes(entry: KeywordPageEntry) -> int:
    try:
        payload_bytes = (
            encoded_size(entry.conversation) + encoded_size(entry.message)
            + encoded_size(entry.sort_key)
            + len(repr(entry.locator).encode("utf-8", errors="replace"))
            + len(entry.fingerprint.encode("utf-8", errors="replace"))
        )
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise SearchError("INVALID_SEARCH_DOCUMENT", "关键词搜索结果包含不受支持的展示数据。") from None
    # Include conservative Python object/Unicode overhead as well as locator
    # identity; this is deliberately the same ledger as A's summary cache.
    return 4 * payload_bytes + 2048


def scan_keyword_documents(
    documents: Iterable[SearchDocument], query: str, order: str, *, deadline: float,
    max_candidates: int | None = None,
) -> tuple[tuple[KeywordPageEntry, ...], int, int]:
    """Collect the exact legacy keyword match set into a charged full snapshot.

    The candidate limit applies before matching, just like the existing
    get_messages path. Exceeding it fails the entire scan; results are never
    silently clipped to a page or to A's Top-K limit. The caller supplies every
    date-range candidate, already converted using the legacy display/media
    selection, with a stable total-order key. This routine does no source I/O.
    """
    if not isinstance(query, str):
        raise SearchError("INVALID_ARGUMENT", "关键词必须是字符串。")
    if order not in {"asc", "desc"}:
        raise SearchError("INVALID_ARGUMENT", "order 必须是 asc 或 desc。")
    if max_candidates is None:
        max_candidates = KEYWORD_CANDIDATE_LIMIT
    if (isinstance(max_candidates, bool) or not isinstance(max_candidates, int)
            or not 0 <= max_candidates <= KEYWORD_CANDIDATE_LIMIT):
        raise SearchError("INVALID_ARGUMENT", "关键词候选上限无效。")

    needle = query.casefold()
    reservation = _MemoryReservation(_QUERY_MEMORY)
    entries: list[KeywordPageEntry] = []
    retained_bytes = 0
    scanned = 0
    try:
        iterator = iter(documents)
        try:
            reservation.resize(_SESSION_OVERHEAD_BYTES)
            for document in iterator:
                if time.monotonic() >= deadline:
                    raise SearchError("SEARCH_BUDGET_EXCEEDED", "关键词检索超过时间预算，请缩小日期范围。", True)
                scanned += 1
                if scanned > max_candidates:
                    raise SearchError("RESULT_LIMIT_EXCEEDED", "所选范围内消息过多，请缩小日期范围。")
                if not _keyword_matches(document.message, needle, document.keyword_content):
                    continue
                borrowed = KeywordPageEntry(document.conversation, document.message,
                                            document.sort_key, document.locator, document.fingerprint)
                entry_bytes = _keyword_entry_cache_bytes(borrowed)
                reservation.resize(_SESSION_OVERHEAD_BYTES + retained_bytes + entry_bytes)
                entries.append(_copy_keyword_entry(document))
                retained_bytes += entry_bytes
        finally:
            close = getattr(iterator, "close", None)
            if close is not None:
                close()
        if time.monotonic() >= deadline:
            raise SearchError("SEARCH_BUDGET_EXCEEDED", "关键词检索超过时间预算，请缩小日期范围。", True)
        try:
            entries.sort(key=lambda entry: entry.sort_key, reverse=order == "desc")
        except TypeError:
            raise SearchError("INVALID_SEARCH_DOCUMENT", "关键词结果的稳定排序键无效。") from None
        if time.monotonic() >= deadline:
            raise SearchError("SEARCH_BUDGET_EXCEEDED", "关键词检索超过时间预算，请缩小日期范围。", True)
        return _KeywordEntries(entries, reservation, query, order, scanned), len(entries), scanned
    except BaseException:
        reservation.release()
        raise


class KeywordPageCache:
    """Fixed keyword snapshots sharing A's process-wide memory budget.

    The integration owner supplies current generations/criteria on each access
    and calls clear on source replacement/disconnect. Continuation never takes
    a source iterator or a projection/media callback, so it cannot repeat them.
    """

    def __init__(self) -> None:
        self.sessions: OrderedDict[str, KeywordPageSession] = OrderedDict()
        self._reservations: dict[str, _MemoryReservation] = {}
        # Authenticated boundaries require no unmetered, growing cursor map.
        # The signature also binds the boundary to its complete query session.
        self._cursor_key = secrets.token_bytes(32)
        self.bytes_used = 0
        _QUERY_MEMORY._caches.add(self)

    def clear(self) -> None:
        for reservation in self._reservations.values():
            reservation.release()
        self._reservations.clear()
        self.sessions.clear()
        self.bytes_used = 0

    def discard(self, page_session_id: str) -> None:
        old = self.sessions.pop(page_session_id, None)
        if old is not None:
            self._reservations.pop(page_session_id).release()
            self.bytes_used -= old.cache_bytes

    def purge(self) -> None:
        now = time.monotonic()
        for key, session in list(self.sessions.items()):
            if session.expires_at <= now:
                self.discard(key)

    def create(
        self, source: str, source_session_id: str,
        entries: tuple[KeywordPageEntry, ...], total: int, candidate_count: int,
        generations: Any, criteria: KeywordSearchCriteria,
    ) -> KeywordPageSession:
        reservation = entries.reservation if isinstance(entries, _KeywordEntries) else None
        if reservation is None:
            reservation = _MemoryReservation(_QUERY_MEMORY)
        try:
            if (isinstance(total, bool) or not isinstance(total, int) or total != len(entries)
                    or isinstance(candidate_count, bool) or not isinstance(candidate_count, int)
                    or not total <= candidate_count <= KEYWORD_CANDIDATE_LIMIT):
                raise SearchError("INVALID_SEARCH_SNAPSHOT", "关键词结果不完整，不能创建分页会话。")
            criteria_fingerprint = _criteria_fingerprint(criteria)
            if isinstance(entries, _KeywordEntries) and (
                entries.query != criteria.query or entries.order != criteria.order
                or entries.candidate_count != candidate_count
            ):
                raise SearchError("INVALID_SEARCH_SNAPSHOT", "关键词扫描条件与分页会话不一致。")
            if any(entry.sort_key is None for entry in entries):
                raise SearchError("INVALID_SEARCH_SNAPSHOT", "关键词结果缺少稳定排序身份。")
            if criteria.conversation_id is not None and any(
                entry.conversation.get("id") != criteria.conversation_id for entry in entries
            ):
                raise SearchError("INVALID_SEARCH_SNAPSHOT", "关键词结果与指定会话不一致。")
            self.purge()
            generation_fingerprint = _generation_fingerprint(generations)
            size = (
                sum(_keyword_entry_cache_bytes(entry) for entry in entries)
                + _SESSION_OVERHEAD_BYTES
                + 4 * len(repr(generations))
                + 4 * len(criteria_fingerprint)
            )
            # A scan's reservation moves into the retained session; it is not
            # released and reacquired, so A and B3 never double-spend the limit.
            reservation.resize(size)
            session = KeywordPageSession(
                KEYWORD_SESSION_PREFIX + secrets.token_urlsafe(24), source, source_session_id, copy.deepcopy(tuple(entries)), total,
                candidate_count, criteria.order, generation_fingerprint,
                criteria_fingerprint, time.monotonic() + KEYWORD_PAGE_TTL_SECONDS, size,
                sort_version=SORT_VERSION, projection_version=PROJECTION_VERSION,
            )
            while self.sessions and len(self.sessions) >= MAX_KEYWORD_PAGE_SESSIONS:
                self.discard(next(iter(self.sessions)))
            self.sessions[session.page_session_id] = session
            self._reservations[session.page_session_id] = reservation
            self.bytes_used += size
            if isinstance(entries, _KeywordEntries):
                entries.reservation = None
            return session
        except BaseException:
            reservation.release()
            raise

    def get(
        self, page_session_id: str, source: str, source_session_id: str,
        generations: Any, criteria: KeywordSearchCriteria,
    ) -> KeywordPageSession:
        self.purge()
        session = self.sessions.get(page_session_id)
        if session is None:
            raise SearchError("PAGE_EXPIRED", "关键词分页已过期或被释放，请重新查询。", True)
        if session.sort_version != SORT_VERSION or session.projection_version != PROJECTION_VERSION:
            self.discard(page_session_id)
            raise SearchError("PAGE_EXPIRED", "消息投影或排序版本已更新，请重新查询。", True)
        if session.source != source or session.source_session_id != source_session_id:
            self.discard(page_session_id)
            raise SearchError("PAGE_SOURCE_CHANGED", "数据源已变化，请重新查询。", True)
        if session.generations_fingerprint != _generation_fingerprint(generations):
            self.discard(page_session_id)
            raise SearchError("PAGE_SOURCE_CHANGED", "数据源内容已变化，请重新查询。", True)
        if session.criteria_fingerprint != _criteria_fingerprint(criteria):
            self.discard(page_session_id)
            raise SearchError("PAGE_CRITERIA_CHANGED", "关键词、日期、顺序、媒体选项或消息解析版本已变化，请重新查询。", True)
        return session

    def page(
        self, page_session_id: str, source: str, source_session_id: str,
        generations: Any, criteria: KeywordSearchCriteria, offset: int, limit: int,
    ) -> KeywordPage:
        session = self.get(page_session_id, source, source_session_id, generations, criteria)
        return keyword_page(session, offset, limit)

    def _cursor(self, session: KeywordPageSession, direction: str, boundary: int) -> str:
        value = f"{direction}.{boundary}"
        signature = hmac.new(
            self._cursor_key, (session.page_session_id + ":" + value).encode("ascii"), "sha256",
        ).hexdigest()
        return value + "." + signature

    def cursor_page(
        self, page_session_id: str, source: str, source_session_id: str,
        generations: Any, criteria: KeywordSearchCriteria, *, cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """B2-shaped cursors over the completed B3 snapshot, with no source I/O.

        Before-cursors bind an exclusive end, not an assumed previous page size.
        If the byte budget reduces a page, both directions retain the messages
        closest to the boundary and issue cursors for the actual slice.
        """
        session = self.get(page_session_id, source, source_session_id, generations, criteria)
        if type(limit) is not int or not 1 <= limit <= MAX_KEYWORD_PAGE_SIZE:
            raise SearchError("INVALID_ARGUMENT", "分页大小超出上限。")
        direction, boundary = "after", 0
        if cursor is not None:
            try:
                if not isinstance(cursor, str) or len(cursor) > 256:
                    raise ValueError
                direction, value, _signature = cursor.split(".")
                if direction not in {"before", "after"} or not value.isascii() or not value.isdecimal():
                    raise ValueError
                boundary = int(value)
                if not 0 <= boundary <= session.total or not hmac.compare_digest(
                    cursor, self._cursor(session, direction, boundary),
                ):
                    raise ValueError
            except (ValueError, TypeError, UnicodeError):
                raise SearchError("PAGE_EXPIRED", "关键词分页游标已失效，请重新查询。", True) from None
        start = max(0, boundary - limit) if direction == "before" else boundary
        end = boundary if direction == "before" else min(session.total, boundary + limit)
        if session.total and start >= end:
            raise SearchError("PAGE_EXPIRED", "关键词分页位置已失效，请重新查询。", True)
        result = {
            "pageSessionId": session.page_session_id, "sourceSessionId": session.source_session_id,
            "items": [], "total": session.total, "candidateCount": session.candidate_count,
            "order": session.order, "mode": "keyword", "sortVersion": session.sort_version,
            "projectionVersion": session.projection_version,
            # Reserve the largest possible cursor pair before selecting items.
            "previousCursor": self._cursor(session, "before", session.total),
            "nextCursor": self._cursor(session, "after", session.total),
        }
        size = encoded_size(result)
        budget = MAX_RESPONSE_BYTES - RESPONSE_ENVELOPE_RESERVE
        selected: list[KeywordPageEntry] = []
        candidates = session.entries[start:end]
        for entry in reversed(candidates) if direction == "before" else candidates:
            entry_size = encoded_size(entry.message) + 1
            if size + entry_size > budget:
                if not selected:
                    raise SearchError("RESPONSE_LIMIT_EXCEEDED", "单条关键词匹配消息超过响应上限，无法显示。")
                break
            selected.append(entry)
            size += entry_size
        if direction == "before":
            selected.reverse()
            start = end - len(selected)
        else:
            end = start + len(selected)
        result.update(
            items=copy.deepcopy([entry.message for entry in selected]),
            previousCursor=self._cursor(session, "before", start) if start else None,
            nextCursor=self._cursor(session, "after", end) if end < session.total else None,
        )
        if not response_fits(result):
            raise SearchError("RESPONSE_LIMIT_EXCEEDED", "关键词分页响应超过安全上限。")
        return result


def keyword_page(session: KeywordPageSession, offset: int, limit: int) -> KeywordPage:
    """Bound display bytes without clipping messages; validate via cache.page."""
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise SearchError("INVALID_ARGUMENT", "分页位置无效。")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_KEYWORD_PAGE_SIZE:
        raise SearchError("INVALID_ARGUMENT", "分页大小超出上限。")
    previous_offset = max(0, min(offset, session.total) - limit) if offset > 0 else None
    envelope = {
        "pageSessionId": session.page_session_id, "sourceSessionId": session.source_session_id,
        "items": [], "total": session.total, "order": session.order,
        "sortVersion": session.sort_version, "offset": offset,
        "previousOffset": previous_offset, "nextOffset": max(1_000, session.total),
    }
    size = encoded_size(envelope)
    budget = MAX_RESPONSE_BYTES - RESPONSE_ENVELOPE_RESERVE
    retained: list[KeywordPageEntry] = []
    for entry in session.entries[offset:offset + limit]:
        entry_size = encoded_size(entry.message) + 1  # Conservative comma allowance.
        if size + entry_size > budget:
            if not retained:
                raise SearchError("RESPONSE_LIMIT_EXCEEDED", "单条关键词匹配消息超过响应上限，无法显示。")
            break
        retained.append(entry)
        size += entry_size
    if size > budget:
        raise SearchError("RESPONSE_LIMIT_EXCEEDED", "关键词分页响应超过安全上限。")
    # A protocol encoder/UI must not be able to mutate the retained snapshot.
    items = copy.deepcopy(tuple(retained))
    next_offset = offset + len(items) if offset + len(items) < session.total else None
    return KeywordPage(
        session.page_session_id, session.source_session_id, items, offset,
        session.total, session.order, previous_offset, next_offset,
    )


def search_page(session: SearchSession, offset: int, limit: int) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    result = {
        "searchId": session.search_id,
        "sourceSessionId": session.source_session_id,
        "items": items,
        "total": session.total,
        "resultLimit": RESULT_LIMIT,
        "truncated": session.total > RESULT_LIMIT,
        "nextOffset": None,
        "coverage": session.coverage,
    }
    for hit in session.hits[offset:offset + limit]:
        items.append(hit.item)
        result["nextOffset"] = offset + len(items) if offset + len(items) < len(session.hits) else None
        if not response_fits(result):
            items.pop()
            if not items:
                raise SearchError("RESPONSE_LIMIT_EXCEEDED", "单条搜索结果超过响应上限，请缩小查询范围。")
            break
    result["nextOffset"] = offset + len(items) if offset + len(items) < len(session.hits) else None
    if not response_fits(result):
        raise SearchError("RESPONSE_LIMIT_EXCEEDED", "搜索响应超过安全上限。")
    return result


def bounded_context(conversation: dict[str, Any], before: list[dict[str, Any]], target: dict[str, Any],
                    after: list[dict[str, Any]], has_earlier: bool, has_later: bool) -> dict[str, Any]:
    """Keep the target intact; trim furthest neighbours, never target content."""
    before, after = list(before), list(after)
    budget = MAX_RESPONSE_BYTES - RESPONSE_ENVELOPE_RESERVE
    target_bytes = encoded_size(target)
    base = {"conversation": conversation, "items": [], "focusIndex": 0,
            "hasEarlier": has_earlier, "hasLater": has_later}
    if encoded_size(base) + target_bytes > budget:
        raise SearchError("RESPONSE_LIMIT_EXCEEDED", "命中消息本身超过响应上限，无法显示上下文。")
    # Size each neighbour once. Serialising all 41 potentially large messages
    # over and over while trimming would itself defeat the response budget.
    before_sizes = [encoded_size(item) for item in before]
    after_sizes = [encoded_size(item) for item in after]
    while True:
        base.update(focusIndex=len(before), hasEarlier=has_earlier, hasLater=has_later)
        size = encoded_size(base) + target_bytes + sum(before_sizes) + sum(after_sizes) + len(before) + len(after)
        if size <= budget:
            base["items"] = before + [target] + after
            return base
        if not before and not after:
            raise SearchError("RESPONSE_LIMIT_EXCEEDED", "命中消息本身超过响应上限，无法显示上下文。")
        if len(before) >= len(after) and before:
            before.pop(0)
            before_sizes.pop(0)
            has_earlier = True
        else:
            after.pop()
            after_sizes.pop()
            has_later = True
