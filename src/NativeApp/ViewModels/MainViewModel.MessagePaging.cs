using System.Text.Json;
using CommunityToolkit.Mvvm.Input;
using WechatMemory.NativeApp.Models;
using WechatMemory.NativeApp.Services;

namespace WechatMemory.NativeApp.ViewModels;

public partial class MainViewModel
{
    private readonly MessagePagingWorkflow _messagePagingWorkflow = new();
    private readonly Dictionary<string, string> _legacyPagingConversations = new(StringComparer.Ordinal);
    private readonly Dictionary<string, string> _legacyKeywordPagingConversations = new(StringComparer.Ordinal);
    private HashSet<string>? _messageCursorSources;
    private HashSet<string>? _messageKeywordSources;
    private MessagePageWindow? _messagePageWindow;
    private string _messagePagingNotice = "";
    private string _messagePagingInvalidation = "";

    public bool HasMessageCursorPaginationCapability => _workerCapabilities.Contains("message_cursor_pagination") &&
        (_messageCursorSources is null || _messageCursorSources.Contains(CurrentSource));
    public bool HasMessageKeywordPaginationCapability => _workerCapabilities.Contains("message_keyword_pagination") &&
        (_messageKeywordSources is null || _messageKeywordSources.Contains(CurrentSource));
    public bool ShowCursorMessageNavigation => ShowConversationMessages && _messagePageWindow is not null;
    public bool ShowLegacyMessageNavigation => ShowConversationMessages && _messagePageWindow is null;
    private bool CanNavigateMessageWindow => CanWork && ShowCursorMessageNavigation && !FiltersDirty &&
        _messagePagingInvalidation.Length == 0 && SelectedConversation?.Id == _messageConversationId;
    public bool CanLoadPreviousMessageBatch => CanNavigateMessageWindow && _messagePageWindow?.First.PreviousCursor is not null;
    public bool CanLoadNextMessageBatch => CanNavigateMessageWindow && _messagePageWindow?.Last.NextCursor is not null;
    public bool CanRefreshMessages => CanWork && ShowConversationMessages && SelectedConversation is not null;
    public string MessagePagingStatus => _messagePagingInvalidation.Length > 0 ? _messagePagingInvalidation :
        FiltersDirty && _messageConversationId is not null ? "筛选条件已修改；请应用筛选或刷新首屏后再翻页。" : _messagePagingNotice;
    public int VisibleMessagePageCount => _messagePageWindow?.Pages.Count ?? 0;
    public long VisibleMessageDisplayBytes => _messagePageWindow?.DisplayBytes ?? 0;
    internal long MessageWindowVersion { get; private set; }
    internal int MessageBatchFocusIndex { get; private set; }

    public IAsyncRelayCommand PreviousMessageBatchCommand { get; }
    public IAsyncRelayCommand NextMessageBatchCommand { get; }
    public IAsyncRelayCommand RefreshMessagesCommand { get; }

    private void ReadMessagePagingCapabilities(JsonElement hello)
    {
        _messageCursorSources = null;
        _messageKeywordSources = null;
        // Older capability-aware workers use only the flat list. A structured
        // map is authoritative so a B3-capable worker can advertise support per
        // source without accidentally enabling it for another source.
        if (!hello.TryGetProperty("sourceCapabilities", out var sources)) return;
        if (sources.ValueKind != JsonValueKind.Object)
            throw new WorkerException("PROTOCOL_ERROR", "数据源分页能力格式不正确。");
        var cursor = new HashSet<string>(StringComparer.Ordinal);
        var keyword = new HashSet<string>(StringComparer.Ordinal);
        foreach (var source in sources.EnumerateObject())
        {
            if (source.Value.ValueKind != JsonValueKind.Array ||
                source.Value.EnumerateArray().Any(value => value.ValueKind != JsonValueKind.String))
                throw new WorkerException("PROTOCOL_ERROR", "数据源分页能力格式不正确。");
            var capabilities = source.Value.EnumerateArray().Select(value => value.GetString()!).ToHashSet(StringComparer.Ordinal);
            if (capabilities.Contains("message_cursor_pagination")) cursor.Add(source.Name);
            if (capabilities.Contains("message_keyword_pagination")) keyword.Add(source.Name);
        }
        _messageCursorSources = cursor;
        _messageKeywordSources = keyword;
    }

    private void ResetMessagePaging(bool sourceChanged = false)
    {
        _messagePageWindow = null;
        _messagePagingNotice = "";
        _messagePagingInvalidation = "";
        if (sourceChanged)
        {
            _legacyPagingConversations.Clear();
            _legacyKeywordPagingConversations.Clear();
        }
    }

    private static bool IsCursorCompatibilityFailure(WorkerException error) => error.Code is
        "PAGE_UNSUPPORTED_SCHEMA" or "PAGE_SOURCE_UNVERIFIABLE" or "CAPABILITY_UNAVAILABLE" or "UNKNOWN_COMMAND";

    private static bool IsMessagePagingFailure(WorkerException error) =>
        IsCursorCompatibilityFailure(error) || error.Code is
        "PAGE_EXPIRED" or "PAGE_SOURCE_CHANGED" or "PAGE_CRITERIA_CHANGED" or
        "PAGE_BUDGET_EXCEEDED" or "SEARCH_BUDGET_EXCEEDED" or "RESULT_LIMIT_EXCEEDED" or "SEARCH_CACHE_LIMIT" or
        "RESPONSE_LIMIT_EXCEEDED" or "PAGE_RESPONSE_INVALID" or "PAGE_RESPONSE_TOO_LARGE" or
        "PAGE_READ_FAILED" or "PAGE_SOURCE_UNAVAILABLE" or "MESSAGE_DECODE_FAILED" or
        "MESSAGE_TOO_LARGE" or "SEARCH_UNSUPPORTED_SCHEMA" or "INVALID_SEARCH_SNAPSHOT" or
        "PROTOCOL_ERROR";

    private string LegacyPagingNotice(string? query, string conversationId)
    {
        string reason = query is not null ? "关键词浏览暂用兼容分页（首次及续页可能较慢）" :
            _legacyPagingConversations.TryGetValue(conversationId, out var notice) ? notice :
            "当前工作进程未提供稳定游标分页";
        return $"{reason}；使用原有加载更多，尚未启用有限页窗口。导出/任务仍读取完整筛选范围。";
    }

    private async Task<bool> TryFetchKeywordFirstAsync(WorkerClient worker, Conversation conversation,
        DateRange range, string query, long epoch, bool commitSelection)
    {
        if (!HasMessageKeywordPaginationCapability || string.IsNullOrWhiteSpace(SourceSessionId) ||
            _legacyKeywordPagingConversations.ContainsKey(conversation.Id)) return false;
        string session = SourceSessionId;
        MessagePageWindow candidate;
        try
        {
            candidate = await _messagePagingWorkflow.KeywordFirstPageAsync(
                worker, CurrentSource, session, conversation.Id, range, query, "desc");
        }
        catch (WorkerException error) when (IsCursorCompatibilityFailure(error))
        {
            EnsureMessageRequestCurrent(worker, conversation, range, query, epoch, commitSelection);
            _legacyKeywordPagingConversations[conversation.Id] = "此工作进程或会话结构不支持关键词快照分页，已降级";
            return false;
        }
        EnsureMessageRequestCurrent(worker, conversation, range, query, epoch, commitSelection);
        if (SourceSessionId != session) throw new WorkerException("SOURCE_CHANGED", "数据源已变化，未应用过期的消息页。");
        CommitMessageSelection(conversation, commitSelection);
        _applied = range;
        _messageConversationId = conversation.Id;
        _nextOffset = null;
        FiltersDirty = false;
        RangeLabel = $"{range.Label} · 搜索“{query}”";
        CommitMessageWindow(candidate);
        Status = candidate.Total == 0 ? "筛选完成，没有符合条件的消息。" : "关键词快照已加载；导出和任务仍按完整筛选范围读取。";
        UpdateFlags();
        return true;
    }

    private async Task<bool> TryFetchCursorFirstAsync(WorkerClient worker, Conversation conversation,
        DateRange range, string? query, long epoch, bool commitSelection)
    {
        if (query is not null || !HasMessageCursorPaginationCapability || string.IsNullOrWhiteSpace(SourceSessionId) ||
            _legacyPagingConversations.ContainsKey(conversation.Id)) return false;
        string session = SourceSessionId;
        MessagePageWindow candidate;
        try
        {
            candidate = await _messagePagingWorkflow.FirstPageAsync(worker, CurrentSource, session, conversation.Id, range);
        }
        catch (WorkerException error) when (IsCursorCompatibilityFailure(error))
        {
            EnsureMessageRequestCurrent(worker, conversation, range, query, epoch, commitSelection);
            _legacyPagingConversations[conversation.Id] = "此工作进程或会话结构不支持稳定游标分页，已降级";
            return false;
        }
        EnsureMessageRequestCurrent(worker, conversation, range, query, epoch, commitSelection);
        if (SourceSessionId != session) throw new WorkerException("SOURCE_CHANGED", "数据源已变化，未应用过期的消息页。");
        CommitMessageSelection(conversation, commitSelection);
        _applied = range;
        _messageConversationId = conversation.Id;
        _nextOffset = null;
        FiltersDirty = false;
        RangeLabel = range.Label;
        CommitMessageWindow(candidate);
        Status = candidate.Total == 0 ? "筛选完成，没有符合条件的消息。" : "游标消息已加载；导出和任务仍按完整筛选范围读取。";
        UpdateFlags();
        return true;
    }

    private Task NavigateMessageBatchAsync(bool previous)
    {
        if (!(previous ? CanLoadPreviousMessageBatch : CanLoadNextMessageBatch)) return Task.CompletedTask;
        return RunAsync(async () =>
        {
            var worker = _worker is { IsAvailable: true } current ? current
                : throw new WorkerException("WORKER_EXITED", "工作进程不可用，请重新检测。");
            var window = _messagePageWindow ?? throw new WorkerException("PAGE_EXPIRED", "消息分页已失效，请刷新首屏。");
            var conversation = SelectedConversation!;
            long epoch = _sourceEpoch;
            var candidate = await _messagePagingWorkflow.NavigateAsync(worker, CurrentSource, window, previous);
            string? query = window.First.IsKeyword && !string.IsNullOrWhiteSpace(SearchQuery)
                ? SearchQuery.Trim() : null;
            EnsureMessageRequestCurrent(worker, conversation, _applied, query, epoch, false);
            if (!ReferenceEquals(window, _messagePageWindow) || FiltersDirty || SourceSessionId != window.First.SourceSessionId)
                throw new WorkerException("SEARCH_STALE", "会话或筛选条件已变化，请重新应用筛选。");
            CommitMessageWindow(candidate, focusLastPage: !previous);
            Status = "已切换消息批次；超出窗口的消息已释放，可向前或向后重新读取。";
            UpdateFlags();
        }, previous ? "正在加载上一批消息…" : "正在加载下一批消息…");
    }

    private void CommitMessageWindow(MessagePageWindow candidate, bool focusLastPage = false)
    {
        _messagePageWindow = candidate;
        ++MessageWindowVersion;
        MessageBatchFocusIndex = focusLastPage ? candidate.Pages.Sum(page => page.Items.Count) - candidate.Last.Items.Count : 0;
        _messagePagingInvalidation = "";
        long candidateCount = candidate.First.CandidateCount ?? candidate.Total;
        _messagePagingNotice = candidate.First.IsKeyword
            ? $"关键词快照 · 匹配 {candidate.Total:N0} 条 / 候选 {candidateCount:N0} 条 · 最多保留 {MessagePagingWorkflow.MaxVisiblePages} 页 / {MessagePagingWorkflow.MaxDisplayBytes / (1024 * 1024)} MiB 展示内容。导出/任务仍读取完整筛选范围。"
            : $"稳定游标 · 最多保留 {MessagePagingWorkflow.MaxVisiblePages} 页 / {MessagePagingWorkflow.MaxDisplayBytes / (1024 * 1024)} MiB 展示内容，超出时释放另一端。导出/任务不受预览窗口限制。";
        Messages.Clear();
        foreach (var item in candidate.Items) Messages.Add(item);
        MessageSummary = candidate.Total == 0 ? "此会话在所选条件下没有消息。" :
            $"当前显示第 {candidate.First.StartIndex + 1:N0}–{candidate.Last.EndIndex:N0} / {candidate.Total:N0} 条 · {candidate.Pages.Count} 页窗口";
    }

    private void EnsureMessageRequestCurrent(WorkerClient worker, Conversation conversation, DateRange range,
        string? query, long epoch, bool commitSelection)
    {
        if (epoch != _sourceEpoch || !ReferenceEquals(_worker, worker) || !worker.IsAvailable || !IsConnected || _closing)
            throw new WorkerException("SOURCE_CHANGED", "数据源已变化，未应用过期的消息页。");
        if (!commitSelection && SelectedConversation?.Id != conversation.Id ||
            DateRange.Create(StartDate, EndDate) != range ||
            (string.IsNullOrWhiteSpace(SearchQuery) ? null : SearchQuery.Trim()) != query)
            throw new WorkerException("SEARCH_STALE", "会话或筛选条件已变化，请重新应用筛选。");
    }

    private void CommitMessageSelection(Conversation conversation, bool commitSelection)
    {
        if (!commitSelection) return;
        _suppressConversationReset = true;
        _suppressConversationAutoFetch = true;
        try { SelectedConversation = conversation; }
        finally { _suppressConversationReset = false; _suppressConversationAutoFetch = false; }
    }
}
