using System.Globalization;
using System.Text;
using System.Text.Json;
using WechatMemory.NativeApp.Models;

namespace WechatMemory.NativeApp.Services;

public sealed record MessageCursorPage(
    string PageSessionId, string SourceSessionId, IReadOnlyList<ChatMessage> Items,
    long Total, long StartIndex, string? PreviousCursor, string? NextCursor, long DisplayBytes,
    string Mode = "cursor", long? CandidateCount = null, string? Order = null,
    string? ProjectionVersion = null)
{
    public long EndIndex => StartIndex + Items.Count;
    public bool IsKeyword => Mode == "keyword";
}

/// <summary>Only the visible window owns message bodies; no unbounded page/history cache.</summary>
public sealed record MessagePageWindow(IReadOnlyList<MessageCursorPage> Pages)
{
    public MessageCursorPage First => Pages[0];
    public MessageCursorPage Last => Pages[^1];
    public long Total => First.Total;
    public long DisplayBytes => Pages.Sum(page => page.DisplayBytes);
    public IEnumerable<ChatMessage> Items => Pages.SelectMany(page => page.Items);
}

/// <summary>Precise-v1 protocol validation and bounded, bidirectional preview navigation.</summary>
public sealed class MessagePagingWorkflow
{
    public const int PageSize = 50;
    public const int MaxVisiblePages = 20;
    public const long MaxDisplayBytes = 8 * 1024 * 1024;
    public const int MaxResponseBytes = 1024 * 1024;
    private const string SortVersion = "precise-v1";

    public async Task<MessagePageWindow> FirstPageAsync(IWorkerRequestClient worker, string source,
        string sourceSessionId, string conversationId, DateRange range)
    {
        ValidateSource(source, sourceSessionId);
        if (string.IsNullOrWhiteSpace(conversationId)) throw ProtocolError("缺少会话身份。");
        var (response, limit) = await RequestCursorPageAsync(worker, requestedLimit => new
        {
            source, sourceSessionId, conversationId, startDate = range.StartDate,
            endDate = range.EndDate, query = (string?)null, order = "desc", limit = requestedLimit,
        }).ConfigureAwait(false);
        return new MessagePageWindow([ParsePage(response, sourceSessionId, null, null, false, requestedLimit: limit)]);
    }

    /// <summary>
    /// Creates the B3 keyword snapshot. An empty string is deliberately distinct
    /// from null: the worker owns the legacy literal predicate for every string
    /// query, while the native UI normally converts blank input to null.
    /// </summary>
    public async Task<MessagePageWindow> KeywordFirstPageAsync(IWorkerRequestClient worker, string source,
        string sourceSessionId, string conversationId, DateRange range, string query, string order = "desc")
    {
        ValidateSource(source, sourceSessionId);
        if (string.IsNullOrWhiteSpace(conversationId)) throw ProtocolError("缺少会话身份。");
        if (query is null) throw new WorkerException("INVALID_ARGUMENT", "关键词必须是字符串。");
        if (order is not ("asc" or "desc")) throw new WorkerException("INVALID_ARGUMENT", "消息顺序无效。");
        var response = await worker.RequestAsync("get_messages_page", new
        {
            source, sourceSessionId, conversationId, startDate = range.StartDate,
            endDate = range.EndDate, query, order, limit = PageSize,
        }, TimeSpan.FromSeconds(60)).ConfigureAwait(false);
        return new MessagePageWindow([ParsePage(response, sourceSessionId, null, null, false,
            expectedMode: "keyword", expectedOrder: order)]);
    }

    public async Task<MessagePageWindow> NavigateAsync(IWorkerRequestClient worker, string source,
        MessagePageWindow window, bool previous)
    {
        var edge = previous ? window.First : window.Last;
        ValidateSource(source, edge.SourceSessionId);
        string cursor = (previous ? edge.PreviousCursor : edge.NextCursor)
            ?? throw ProtocolError("没有可加载的消息批次。");
        var (response, limit) = await RequestCursorPageAsync(worker, requestedLimit => new
        {
            source, sourceSessionId = edge.SourceSessionId, pageSessionId = edge.PageSessionId,
            cursor, limit = requestedLimit,
        }, allowSizeRetry: !edge.IsKeyword).ConfigureAwait(false);
        var page = ParsePage(response, edge.SourceSessionId, edge.PageSessionId, edge, previous, cursor,
            expectedMode: edge.Mode, expectedOrder: edge.Order, expectedProjectionVersion: edge.ProjectionVersion,
            expectedCandidateCount: edge.CandidateCount, requestedLimit: limit);
        var pages = window.Pages.ToList();
        if (previous) pages.Insert(0, page);
        else pages.Add(page);
        long bytes = pages.Sum(item => item.DisplayBytes);
        while (pages.Count > MaxVisiblePages || bytes > MaxDisplayBytes)
        {
            int discard = previous ? pages.Count - 1 : 0;
            bytes -= pages[discard].DisplayBytes;
            pages.RemoveAt(discard);
        }
        return new MessagePageWindow(pages.ToArray());
    }

    private static async Task<(JsonElement Response, int Limit)> RequestCursorPageAsync(
        IWorkerRequestClient worker, Func<int, object> payload, bool allowSizeRetry = true)
    {
        // B2 cursors bind an exclusive physical boundary and direction, not a
        // page length. Reusing that same token with a smaller limit reads the
        // nearest rows on either side without consuming or skipping the edge.
        // First-page retries keep the original criteria and publish only the
        // successful session. No candidate page mutates the caller's window.
        int limit = PageSize;
        while (true)
        {
            try
            {
                return (await worker.RequestAsync("get_messages_page", payload(limit),
                    TimeSpan.FromSeconds(60)).ConfigureAwait(false), limit);
            }
            catch (WorkerException error) when (allowSizeRetry && error.Code == "PAGE_RESPONSE_TOO_LARGE")
            {
                if (limit == 1)
                    throw new WorkerException(error.Code,
                        "每页仅 1 条消息时仍超过协议大小上限，无法显示；正文未截断，请调整筛选范围后刷新首屏。", error);
                // At most six attempts: 50, 25, 12, 6, 3, 1. Budget, decoding,
                // expiry, malformed-response and B3 snapshot failures are not
                // size negotiation; retrying those can invalidate the session.
                limit = Math.Max(1, limit / 2);
            }
        }
    }

    private static MessageCursorPage ParsePage(JsonElement response, string sourceSessionId,
        string? pageSessionId, MessageCursorPage? edge, bool previous, string? usedCursor = null,
        string expectedMode = "cursor", string? expectedOrder = null,
        string? expectedProjectionVersion = null, long? expectedCandidateCount = null,
        int requestedLimit = PageSize)
    {
        RequireObject(response);
        if (Encoding.UTF8.GetByteCount(response.GetRawText()) > MaxResponseBytes ||
            !Text(response, "pageSessionId", out var session) || session.Length > 256 ||
            !Text(response, "sortVersion", out var sort) || sort != SortVersion ||
            !response.TryGetProperty("total", out var totalElement) || totalElement.ValueKind != JsonValueKind.Number ||
            !totalElement.TryGetInt64(out long total) || total < 0 ||
            !response.TryGetProperty("items", out var itemsElement) || itemsElement.ValueKind != JsonValueKind.Array ||
            itemsElement.GetArrayLength() > requestedLimit)
            throw ProtocolError("消息游标页格式、排序版本或大小不正确。");

        string mode = "cursor";
        long? candidateCount = null;
        string? order = null;
        string? projectionVersion = null;
        if (expectedMode == "keyword")
        {
            if (!Text(response, "mode", out mode) || mode != "keyword" ||
                !Text(response, "sourceSessionId", out var echoedSource) || echoedSource != sourceSessionId ||
                !Text(response, "order", out var parsedOrder) || parsedOrder != expectedOrder ||
                !Text(response, "projectionVersion", out var parsedProjection) || parsedProjection.Length > 256 ||
                !response.TryGetProperty("candidateCount", out var candidateElement) ||
                candidateElement.ValueKind != JsonValueKind.Number || !candidateElement.TryGetInt64(out var parsedCandidate) ||
                parsedCandidate < 0 || parsedCandidate < total || parsedCandidate > 200_000)
                throw ProtocolError("关键词消息页缺少有效的匹配总数、候选数或投影身份。");
            order = parsedOrder;
            projectionVersion = parsedProjection;
            candidateCount = parsedCandidate;
            if (expectedProjectionVersion is not null && parsedProjection != expectedProjectionVersion ||
                expectedCandidateCount is not null && parsedCandidate != expectedCandidateCount)
                throw ProtocolError("关键词分页会话的投影、候选数或排序发生变化，请重新查询。");
        }
        else if (response.TryGetProperty("mode", out var unexpectedMode) &&
                 unexpectedMode.ValueKind != JsonValueKind.Null)
        {
            throw ProtocolError("普通消息页返回了不支持的分页模式。");
        }

        var previousCursor = Cursor(response, "previousCursor");
        var nextCursor = Cursor(response, "nextCursor");
        if (previousCursor is not null && previousCursor == nextCursor ||
            pageSessionId is not null && session != pageSessionId || edge is not null && total != edge.Total)
            throw ProtocolError("消息分页会话或总数发生变化，请从首屏刷新。");
        // The B3 response always echoes the source identity. B2 remains
        // compatible with workers that predate that optional echo.
        if (response.TryGetProperty("sourceSessionId", out var echo) &&
            (echo.ValueKind != JsonValueKind.String || echo.GetString() != sourceSessionId))
            throw ProtocolError("消息分页响应不属于当前数据源。");

        var items = itemsElement.EnumerateArray().Select(ParseMessage).ToArray();
        long start = edge is null ? 0 : previous ? edge.StartIndex - items.Length : edge.EndIndex;
        long end = start + items.Length;
        if (start < 0 || end > total || (total > 0 && items.Length == 0) ||
            (previousCursor is not null) != (start > 0) || (nextCursor is not null) != (end < total))
            throw ProtocolError("消息游标页边界、数量与总数不一致。");
        long displayBytes = items.Sum(DisplaySize);
        if (displayBytes > MaxDisplayBytes) throw ProtocolError("单页消息超过本机展示预算，请缩小范围。");
        // Duplicate public IDs and equal timestamps are legitimate physical rows.
        // Never deduplicate or re-sort the worker's precise-v1 order.
        return new MessageCursorPage(session, sourceSessionId, items, total, start,
            previousCursor, nextCursor, displayBytes, mode, candidateCount, order, projectionVersion);
    }

    private static ChatMessage ParseMessage(JsonElement value)
    {
        RequireObject(value);
        if (!Text(value, "id", out var id) || !String(value, "sender", out var sender) ||
            !Text(value, "timestamp", out var timestamp) || !String(value, "content", out var content) ||
            !Text(value, "kind", out var kind) || !DateTimeOffset.TryParse(timestamp,
                CultureInfo.InvariantCulture, DateTimeStyles.AssumeUniversal, out _))
            throw ProtocolError("消息游标页包含无效消息。");
        bool outgoing = false;
        if (value.TryGetProperty("isOutgoing", out var outgoingElement))
        {
            if (outgoingElement.ValueKind is not (JsonValueKind.True or JsonValueKind.False))
                throw ProtocolError("消息方向格式不正确。");
            outgoing = outgoingElement.GetBoolean();
        }
        int? duration = null;
        if (value.TryGetProperty("durationMs", out var durationElement) && durationElement.ValueKind != JsonValueKind.Null)
        {
            if (durationElement.ValueKind != JsonValueKind.Number || !durationElement.TryGetInt32(out int parsed) || parsed < 0)
                throw ProtocolError("消息时长格式不正确。");
            duration = parsed;
        }
        return MessageProjectionReader.Read(value, new ChatMessage(id, sender, timestamp, content, kind, outgoing,
            OptionalString(value, "attachmentPath"), duration, OptionalString(value, "transcript")));
    }

    private static long DisplaySize(ChatMessage item) => 128L + 2L *
        ((long)item.Id.Length + item.Sender.Length + item.Timestamp.Length + item.Content.Length +
         item.Kind.Length + (item.AttachmentPath?.Length ?? 0) + (item.Transcript?.Length ?? 0)) +
        MessageProjectionReader.DisplayBytes(item);

    private static string? Cursor(JsonElement value, string name)
    {
        if (!value.TryGetProperty(name, out var cursor)) throw ProtocolError("消息页缺少游标边界。");
        if (cursor.ValueKind == JsonValueKind.Null) return null;
        if (cursor.ValueKind != JsonValueKind.String || string.IsNullOrWhiteSpace(cursor.GetString()) || cursor.GetString()!.Length > 4096)
            throw ProtocolError("消息页游标格式不正确。");
        return cursor.GetString();
    }

    private static string? OptionalString(JsonElement value, string name)
    {
        if (!value.TryGetProperty(name, out var property) || property.ValueKind == JsonValueKind.Null) return null;
        if (property.ValueKind != JsonValueKind.String) throw ProtocolError("消息可选字段格式不正确。");
        return property.GetString();
    }

    private static bool Text(JsonElement value, string name, out string text) =>
        String(value, name, out text) && !string.IsNullOrWhiteSpace(text);

    private static bool String(JsonElement value, string name, out string text)
    {
        text = "";
        if (!value.TryGetProperty(name, out var property) || property.ValueKind != JsonValueKind.String) return false;
        text = property.GetString()!;
        return true;
    }

    private static void RequireObject(JsonElement value)
    {
        if (value.ValueKind != JsonValueKind.Object ||
            value.EnumerateObject().Select(property => property.Name).Distinct(StringComparer.Ordinal).Count() != value.EnumerateObject().Count())
            throw ProtocolError("消息游标页包含无效或重复字段。");
    }

    private static void ValidateSource(string source, string session)
    {
        if (source is not ("wechat4" or "offline" or "demo") || string.IsNullOrWhiteSpace(session) || session.Length > 256)
            throw ProtocolError("当前数据源没有有效的分页身份，请重新连接或导入。");
    }

    private static WorkerException ProtocolError(string message) => new("PROTOCOL_ERROR", message);
}
