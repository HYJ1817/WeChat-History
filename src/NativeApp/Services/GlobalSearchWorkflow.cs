using System.Globalization;
using System.Text;
using System.Text.Json;
using WechatMemory.NativeApp.Models;

namespace WechatMemory.NativeApp.Services;

/// <summary>Small request boundary so protocol flows can be tested without WinUI.</summary>
public interface IWorkerRequestClient
{
    Task<JsonElement> RequestAsync(string command, object payload, TimeSpan? timeout = null);
}

/// <summary>
/// Executes and validates global-search protocol flows. Continuation requests deliberately
/// omit query filters; the worker pages the fixed result set created by the first request.
/// </summary>
public sealed class GlobalSearchWorkflow
{
    public const int PageSize = 50;
    public const int MaxPageSize = 100;
    public const int MaxResultLimit = 1_000;
    public const int ContextRadius = 20;
    public const int MaxResponseBytes = 1024 * 1024;

    public async Task<GlobalSearchSnapshot> SearchFirstPageAsync(
        IWorkerRequestClient worker, string source, string sourceSessionId,
        GlobalSearchCriteria criteria, int limit = PageSize)
    {
        ArgumentNullException.ThrowIfNull(worker);
        ValidateSource(source, sourceSessionId);
        ValidateCriteria(criteria);
        ValidateLimit(limit);
        var response = await worker.RequestAsync("search_messages", new
        {
            source, sourceSessionId, query = criteria.Query,
            startDate = criteria.StartDate, endDate = criteria.EndDate,
            order = criteria.Order, offset = 0, limit,
        }, TimeSpan.FromSeconds(60)).ConfigureAwait(false);
        return ParsePage(response, sourceSessionId, null, 0, limit, null);
    }

    public async Task<GlobalSearchSnapshot> SearchNextPageAsync(
        IWorkerRequestClient worker, string source, GlobalSearchSnapshot current, int limit = PageSize)
    {
        ArgumentNullException.ThrowIfNull(worker);
        ArgumentNullException.ThrowIfNull(current);
        ValidateSource(source, current.SourceSessionId);
        ValidateLimit(limit);
        int offset = current.NextOffset ?? throw ProtocolError("没有可加载的后续搜索页。");
        if (offset != current.Items.Count || offset <= 0 || offset >= current.ResultLimit)
            throw ProtocolError("全局搜索分页状态不一致，请重新搜索。");
        var response = await worker.RequestAsync("search_messages", new
        {
            source, sourceSessionId = current.SourceSessionId,
            searchId = current.SearchId, offset, limit,
        }, TimeSpan.FromSeconds(60)).ConfigureAwait(false);
        var page = ParsePage(response, current.SourceSessionId, current.SearchId, offset, limit, current);
        return current with { Items = current.Items.Concat(page.Items).ToArray(), NextOffset = page.NextOffset };
    }

    public async Task<MessageContextResult> GetContextAsync(
        IWorkerRequestClient worker, string source, GlobalSearchSnapshot snapshot, GlobalSearchHit hit)
    {
        ArgumentNullException.ThrowIfNull(worker);
        ArgumentNullException.ThrowIfNull(snapshot);
        ArgumentNullException.ThrowIfNull(hit);
        ValidateSource(source, snapshot.SourceSessionId);
        // Only the snapshot owns the trusted display identity for an opaque locator.
        hit = snapshot.Items.FirstOrDefault(item => item.HitId == hit.HitId)
            ?? throw ProtocolError("此搜索结果已失效，请重新搜索。");
        var response = await worker.RequestAsync("get_message_context", new
        {
            source, sourceSessionId = snapshot.SourceSessionId,
            searchId = snapshot.SearchId, hitId = hit.HitId,
            before = ContextRadius, after = ContextRadius,
        }, TimeSpan.FromSeconds(60)).ConfigureAwait(false);
        return ParseContext(response, hit);
    }

    private static GlobalSearchSnapshot ParsePage(
        JsonElement response, string expectedSourceSessionId, string? expectedSearchId,
        int expectedOffset, int requestedLimit, GlobalSearchSnapshot? previous)
    {
        if (response.ValueKind != JsonValueKind.Object ||
            Encoding.UTF8.GetByteCount(response.GetRawText()) > MaxResponseBytes ||
            !Text(response, "searchId", out var searchId) ||
            !Text(response, "sourceSessionId", out var sourceSessionId) ||
            !response.TryGetProperty("items", out var itemsElement) || itemsElement.ValueKind != JsonValueKind.Array ||
            !Int(response, "total", out int total) || total < 0 ||
            !Int(response, "resultLimit", out int resultLimit) || resultLimit != MaxResultLimit ||
            !Bool(response, "truncated", out bool truncated) ||
            !response.TryGetProperty("nextOffset", out var nextElement) ||
            !response.TryGetProperty("coverage", out var coverage) || coverage.ValueKind != JsonValueKind.Object)
            throw ProtocolError("全局搜索结果格式不正确。");

        if (!string.Equals(sourceSessionId, expectedSourceSessionId, StringComparison.Ordinal) ||
            expectedSearchId is not null && !string.Equals(searchId, expectedSearchId, StringComparison.Ordinal))
            throw ProtocolError("全局搜索响应与当前数据源或搜索会话不匹配。");
        ValidateCoverage(coverage);
        if (previous is not null && (total != previous.Total || resultLimit != previous.ResultLimit ||
            truncated != previous.Truncated || !JsonElement.DeepEquals(coverage, previous.Coverage)))
            throw ProtocolError("全局搜索分页元数据发生变化，请重新搜索。");
        if (itemsElement.GetArrayLength() > requestedLimit || itemsElement.GetArrayLength() > MaxPageSize)
            throw ProtocolError("全局搜索页超出允许的结果数量。");
        if (total < itemsElement.GetArrayLength() || truncated != (total > resultLimit))
            throw ProtocolError("全局搜索总数或截断标记不一致。");

        var items = new List<GlobalSearchHit>(itemsElement.GetArrayLength());
        var ids = previous is null
            ? new HashSet<string>(StringComparer.Ordinal)
            : previous.Items.Select(item => item.HitId).ToHashSet(StringComparer.Ordinal);
        foreach (var element in itemsElement.EnumerateArray())
        {
            var hit = ParseHit(element);
            if (!ids.Add(hit.HitId)) throw ProtocolError("全局搜索页包含重复的命中标识。");
            items.Add(hit);
        }

        int? nextOffset = null;
        if (nextElement.ValueKind == JsonValueKind.Number && nextElement.TryGetInt32(out int next))
            nextOffset = next;
        else if (nextElement.ValueKind != JsonValueKind.Null)
            throw ProtocolError("全局搜索下一页位置格式不正确。");
        int expectedNext = expectedOffset + items.Count;
        int available = Math.Min(total, resultLimit);
        if (expectedNext > available || expectedOffset > available ||
            nextOffset != (expectedNext < available ? expectedNext : null) ||
            items.Count == 0 && expectedNext < available)
            throw ProtocolError("全局搜索分页位置不连续。");

        return new GlobalSearchSnapshot(searchId, sourceSessionId, items, total, resultLimit,
            truncated, nextOffset, coverage.Clone());
    }

    private static GlobalSearchHit ParseHit(JsonElement element)
    {
        if (element.ValueKind != JsonValueKind.Object ||
            !Text(element, "hitId", out var hitId) ||
            !Text(element, "conversationId", out var conversationId) ||
            !Text(element, "conversationTitle", out var conversationTitle) ||
            !Text(element, "conversationKind", out var conversationKind) ||
            !Text(element, "messageId", out var messageId) ||
            !Text(element, "sender", out var sender) ||
            !Text(element, "timestamp", out var timestamp) ||
            !Text(element, "kind", out var kind) ||
            !TextAllowEmpty(element, "snippet", out var snippet) || snippet.Length > 2_048 ||
            !element.TryGetProperty("matchedFields", out var fields) || fields.ValueKind != JsonValueKind.Array)
            throw ProtocolError("全局搜索命中格式不正确。");
        if (!DateTimeOffset.TryParse(timestamp, CultureInfo.InvariantCulture, DateTimeStyles.AssumeUniversal, out _))
            throw ProtocolError("全局搜索命中的时间格式不正确。");
        var matchedFields = new List<string>();
        foreach (var field in fields.EnumerateArray())
        {
            if (field.ValueKind != JsonValueKind.String) throw ProtocolError("全局搜索命中字段格式不正确。");
            string value = field.GetString() ?? "";
            if (value is not ("sender" or "content" or "kind" or "transcript"))
                throw ProtocolError("全局搜索命中包含未知字段。");
            if (!matchedFields.Contains(value, StringComparer.Ordinal)) matchedFields.Add(value);
        }
        if (matchedFields.Count == 0) throw ProtocolError("全局搜索命中缺少匹配字段。");
        return MessageProjectionReader.Read(element, new GlobalSearchHit(hitId, conversationId, conversationTitle, conversationKind,
            messageId, sender, timestamp, kind, snippet, matchedFields));
    }

    private static MessageContextResult ParseContext(JsonElement response, GlobalSearchHit hit)
    {
        if (response.ValueKind != JsonValueKind.Object ||
            Encoding.UTF8.GetByteCount(response.GetRawText()) > MaxResponseBytes ||
            !response.TryGetProperty("conversation", out var conversationElement) ||
            !response.TryGetProperty("items", out var itemsElement) || itemsElement.ValueKind != JsonValueKind.Array ||
            !Int(response, "focusIndex", out int focusIndex) ||
            !Bool(response, "hasEarlier", out bool hasEarlier) ||
            !Bool(response, "hasLater", out bool hasLater))
            throw ProtocolError("消息上下文结果格式不正确。");
        var conversation = ParseConversation(conversationElement);
        if (!string.Equals(conversation.Id, hit.ConversationId, StringComparison.Ordinal) ||
            !string.Equals(conversation.Kind, hit.ConversationKind, StringComparison.Ordinal) ||
            itemsElement.GetArrayLength() is < 1 or > (ContextRadius * 2 + 1) ||
            focusIndex < 0 || focusIndex >= itemsElement.GetArrayLength() || focusIndex > ContextRadius ||
            itemsElement.GetArrayLength() - focusIndex - 1 > ContextRadius)
            throw ProtocolError("消息上下文不属于目标会话或超出邻居范围。");

        var items = itemsElement.EnumerateArray().Select(ParseContextMessage).ToArray();
        var focused = items[focusIndex];
        if (!string.Equals(focused.Id, hit.MessageId, StringComparison.Ordinal) ||
            !string.Equals(focused.Timestamp, hit.Timestamp, StringComparison.Ordinal) ||
            !string.Equals(focused.Sender, hit.Sender, StringComparison.Ordinal) ||
            !string.Equals(focused.Kind, hit.Kind, StringComparison.Ordinal))
            throw ProtocolError("消息上下文未将原始命中放在指定位置。");
        items[focusIndex] = items[focusIndex] with { IsContextFocus = true };
        return new MessageContextResult(conversation, items, focusIndex, hasEarlier, hasLater);
    }

    private static Conversation ParseConversation(JsonElement element)
    {
        if (element.ValueKind != JsonValueKind.Object ||
            !Text(element, "id", out var id) || !Text(element, "title", out var title))
            throw ProtocolError("消息上下文会话身份格式不正确。");
        string kind = Text(element, "kind", out var readKind) ? readKind : "direct";
        bool knownCount = Int(element, "messageCount", out int count) && count >= 0;
        if (Bool(element, "countKnown", out bool advertisedCountKnown)) knownCount &= advertisedCountKnown;
        return new Conversation(id, title, knownCount ? count : 0, kind, knownCount);
    }

    private static ChatMessage ParseContextMessage(JsonElement element)
    {
        if (element.ValueKind != JsonValueKind.Object ||
            !Text(element, "id", out var id) || !Text(element, "sender", out var sender) ||
            !Text(element, "timestamp", out var timestamp) || !TextAllowEmpty(element, "content", out var content) ||
            !Text(element, "kind", out var kind) || !DateTimeOffset.TryParse(timestamp, CultureInfo.InvariantCulture, DateTimeStyles.AssumeUniversal, out _))
            throw ProtocolError("消息上下文包含格式不正确的消息。");
        bool outgoing = Bool(element, "isOutgoing", out bool parsedOutgoing) && parsedOutgoing;
        int? duration = Int(element, "durationMs", out int parsedDuration) && parsedDuration >= 0 ? parsedDuration : null;
        string? transcript = Text(element, "transcript", out var parsedTranscript) ? parsedTranscript : null;
        // Media paths are intentionally never accepted from a global-search context response.
        return MessageProjectionReader.Read(element, new ChatMessage(id, sender, timestamp, content, kind, outgoing,
            DurationMs: duration, Transcript: transcript));
    }

    private static void ValidateCoverage(JsonElement coverage)
    {
        if (!Bool(coverage, "complete", out bool complete))
            throw ProtocolError("全局搜索缺少有效的内容覆盖状态。");
        foreach (string name in new[] { "scannedMessages", "partialMessages", "truncatedMessages" })
            if (coverage.TryGetProperty(name, out _) && (!Int(coverage, name, out int count) || count < 0))
                throw ProtocolError("全局搜索内容覆盖数量格式不正确。");
        if (Int(coverage, "partialMessages", out int partial) && complete != (partial == 0) ||
            Int(coverage, "scannedMessages", out int scanned) && Int(coverage, "partialMessages", out partial) && partial > scanned ||
            Int(coverage, "truncatedMessages", out int truncated) && Int(coverage, "partialMessages", out partial) && truncated > partial)
            throw ProtocolError("全局搜索内容覆盖状态不一致。");
        if (coverage.TryGetProperty("warnings", out var warnings) &&
            (warnings.ValueKind != JsonValueKind.Array || warnings.EnumerateArray().Any(value => value.ValueKind != JsonValueKind.String)))
            throw ProtocolError("全局搜索内容覆盖警告格式不正确。");
    }

    private static void ValidateSource(string source, string sourceSessionId)
    {
        if (source is not ("wechat4" or "offline" or "demo") || string.IsNullOrWhiteSpace(sourceSessionId))
            throw ProtocolError("当前数据源不支持全局搜索，请重新连接或导入数据。");
    }

    private static void ValidateCriteria(GlobalSearchCriteria criteria)
    {
        ArgumentNullException.ThrowIfNull(criteria);
        if (string.IsNullOrWhiteSpace(criteria.Query)) throw new WorkerException("INVALID_ARGUMENT", "请输入全局搜索关键词。");
        if (criteria.Order is not ("desc" or "asc")) throw new WorkerException("INVALID_ARGUMENT", "请选择最新或最早排序。");
        _ = DateRange.Create(ParseDate(criteria.StartDate), ParseDate(criteria.EndDate));
    }

    private static DateTimeOffset? ParseDate(string? value)
    {
        if (string.IsNullOrWhiteSpace(value)) return null;
        if (!DateOnly.TryParseExact(value, "yyyy-MM-dd", CultureInfo.InvariantCulture, DateTimeStyles.None, out var date))
            throw new WorkerException("INVALID_ARGUMENT", "全局搜索日期格式不正确。");
        return new DateTimeOffset(date.ToDateTime(TimeOnly.MinValue), DateRange.Offset);
    }

    private static void ValidateLimit(int limit)
    {
        if (limit is < 1 or > MaxPageSize) throw new WorkerException("INVALID_ARGUMENT", "全局搜索每页最多显示 100 条。");
    }

    private static bool Text(JsonElement element, string name, out string value)
    {
        value = "";
        return element.ValueKind == JsonValueKind.Object && element.TryGetProperty(name, out var property) &&
            property.ValueKind == JsonValueKind.String && !string.IsNullOrWhiteSpace(value = property.GetString() ?? "");
    }

    private static bool TextAllowEmpty(JsonElement element, string name, out string value)
    {
        value = "";
        if (element.ValueKind != JsonValueKind.Object || !element.TryGetProperty(name, out var property) ||
            property.ValueKind != JsonValueKind.String) return false;
        value = property.GetString() ?? "";
        return true;
    }

    private static bool Int(JsonElement element, string name, out int value)
    {
        value = 0;
        return element.ValueKind == JsonValueKind.Object && element.TryGetProperty(name, out var property) &&
            property.ValueKind == JsonValueKind.Number && property.TryGetInt32(out value);
    }

    private static bool Bool(JsonElement element, string name, out bool value)
    {
        value = false;
        if (element.ValueKind != JsonValueKind.Object || !element.TryGetProperty(name, out var property)) return false;
        if (property.ValueKind is not (JsonValueKind.True or JsonValueKind.False)) return false;
        value = property.GetBoolean();
        return true;
    }

    private static WorkerException ProtocolError(string message) => new("PROTOCOL_ERROR", message);
}

/// <summary>In-memory result lifecycle; changing source invalidates every locator and page.</summary>
public sealed class GlobalSearchSessionState
{
    public string? SourceSessionId { get; private set; }
    public GlobalSearchCriteria? AppliedCriteria { get; private set; }
    public GlobalSearchSnapshot? Snapshot { get; private set; }
    public bool IsDirty { get; private set; }
    public string InvalidationNotice { get; private set; } = "";
    public bool IsInvalidated => InvalidationNotice.Length > 0;
    public bool CanContinue => CanNavigate && Snapshot?.NextOffset is not null;
    public bool CanNavigate => Snapshot is not null && !IsDirty && !IsInvalidated &&
        string.Equals(SourceSessionId, Snapshot.SourceSessionId, StringComparison.Ordinal);

    public void ActivateSource(string? sourceSessionId)
    {
        SourceSessionId = string.IsNullOrWhiteSpace(sourceSessionId) ? null : sourceSessionId;
        AppliedCriteria = null;
        Snapshot = null;
        IsDirty = false;
        InvalidationNotice = "";
    }

    public void UpdateCriteria(GlobalSearchCriteria criteria) => IsDirty =
        AppliedCriteria is not null && criteria != AppliedCriteria;

    public void CommitSearch(GlobalSearchCriteria criteria, GlobalSearchSnapshot snapshot)
    {
        if (!string.Equals(SourceSessionId, snapshot.SourceSessionId, StringComparison.Ordinal))
            throw new WorkerException("SOURCE_CHANGED", "数据源已变化，请重新搜索。");
        AppliedCriteria = criteria;
        Snapshot = snapshot;
        IsDirty = false;
        InvalidationNotice = "";
    }

    public void CommitNextPage(GlobalSearchSnapshot snapshot)
    {
        if (!CanNavigate || Snapshot is null || !string.Equals(SourceSessionId, snapshot.SourceSessionId, StringComparison.Ordinal) ||
            !string.Equals(Snapshot.SearchId, snapshot.SearchId, StringComparison.Ordinal))
            throw new WorkerException("SOURCE_CHANGED", "搜索结果已失效，请重新搜索。");
        Snapshot = snapshot;
    }

    public void InvalidateResults(string notice)
    {
        // Preserve the last successfully applied result for reference/return,
        // but never keep retrying an expired page or locator.
        InvalidationNotice = string.IsNullOrWhiteSpace(notice) ? "搜索结果已失效，请重新执行搜索。" : notice;
    }

    public void ClearResults()
    {
        AppliedCriteria = null;
        Snapshot = null;
        IsDirty = false;
        InvalidationNotice = "";
    }
}
