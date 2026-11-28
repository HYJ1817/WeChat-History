using System.Collections.ObjectModel;
using System.Text.Json;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using WechatMemory.NativeApp.Models;
using WechatMemory.NativeApp.Services;

namespace WechatMemory.NativeApp.ViewModels;

public partial class MainViewModel : ObservableObject, IAsyncDisposable
{
    private readonly Action<Action> _enqueue;
    private readonly Func<WorkerClient> _startWorker;
    private readonly Action<string, string> _log;
    private readonly bool _demoMode;
    private bool _offlineMode;
    private string? _offlinePath;
    private WorkerClient? _worker;
    private int? _nextOffset;
    private string? _messageConversationId;
    private DateRange _applied = new(null, null);
    private readonly Dictionary<string, string> _accountNames = new(StringComparer.Ordinal);
    private readonly OperationGate _operationGate = new();
    private readonly DisconnectStatusGuard _disconnectStatusGuard = new();
    private bool _pruneWorkflowActive;
    private bool _closing;
    private bool _suppressSceneDates;
    private bool _suppressConversationAutoFetch;
    private bool _suppressConversationReset;
    private ConnectionMedia? _connectedMedia;
    private readonly HashSet<string> _workerCapabilities = new(StringComparer.Ordinal);
    private readonly GlobalSearchWorkflow _globalSearchWorkflow = new();
    private readonly GlobalSearchSessionState _globalSearchState = new();
    private long _sourceEpoch;
    private double _globalReturnScrollOffset;
    private Conversation? _preContextConversation;
    public ObservableCollection<Conversation> Conversations { get; } = [];
    public ObservableCollection<ChatMessage> Messages { get; } = [];
    public ObservableCollection<GlobalSearchHit> GlobalSearchHits { get; } = [];
    public ObservableCollection<ChatMessage> ContextMessages { get; } = [];
    public ObservableCollection<AccountItem> Accounts { get; } = [];
    public ObservableCollection<SceneItem> Scenes { get; } = [];
    public ObservableCollection<JobSummary> Jobs { get; } = [];

    [ObservableProperty] public partial Conversation? SelectedConversation { get; set; }
    [ObservableProperty] public partial AccountItem? SelectedAccount { get; set; }
    [ObservableProperty] public partial SceneItem? SelectedScene { get; set; }
    [ObservableProperty] public partial DateTimeOffset? StartDate { get; set; }
    [ObservableProperty] public partial DateTimeOffset? EndDate { get; set; }
    [ObservableProperty] public partial string SearchQuery { get; set; } = "";
    [ObservableProperty] public partial string GlobalSearchQuery { get; set; } = "";
    [ObservableProperty] public partial DateTimeOffset? GlobalStartDate { get; set; }
    [ObservableProperty] public partial DateTimeOffset? GlobalEndDate { get; set; }
    [ObservableProperty] public partial string GlobalSearchOrder { get; set; } = "desc";
    [ObservableProperty] public partial bool IsGlobalSearchMode { get; private set; }
    [ObservableProperty] public partial bool IsContextMode { get; private set; }
    [ObservableProperty] public partial string ContextSummary { get; set; } = "";
    [ObservableProperty] public partial string ContextCoverageNotice { get; set; } = "";
    [ObservableProperty] public partial string ConversationQuery { get; set; } = "";
    [ObservableProperty] public partial bool IsBusy { get; set; }
    [ObservableProperty] public partial bool IsConnected { get; set; }
    [ObservableProperty] public partial bool FiltersDirty { get; set; }
    [ObservableProperty] public partial bool UseWechat { get; set; }
    [ObservableProperty] public partial bool EnableImages { get; set; }
    [ObservableProperty] public partial bool EnableVoice { get; set; }
    [ObservableProperty] public partial string Status { get; set; } = "正在检测本机微信…";
    [ObservableProperty] public partial string Error { get; set; } = "";
    [ObservableProperty] public partial string ResultPath { get; set; } = "";
    [ObservableProperty] public partial string ResultSummary { get; set; } = "";
    [ObservableProperty] public partial string RangeLabel { get; set; } = "全部日期 · UTC+08:00";
    [ObservableProperty] public partial string MessageSummary { get; set; } = "请选择一个会话。";
    [ObservableProperty] public partial string ConnectionLabel { get; set; } = "尚未连接";
    [ObservableProperty] public partial string ProgressLabel { get; set; } = "";
    [ObservableProperty] public partial string VoiceStatusText { get; set; } = "语音未启用";
    [ObservableProperty] public partial string TargetKind { get; set; } = "none";
    [ObservableProperty] public partial string TargetFolder { get; set; } = "";
    [ObservableProperty] public partial string ObsidianVault { get; set; } = "";
    [ObservableProperty] public partial bool ExportMarkdown { get; set; } = true;
    [ObservableProperty] public partial bool ExportJson { get; set; }
    [ObservableProperty] public partial bool ExportPdf { get; set; }
    [ObservableProperty] public partial bool IncludePngPages { get; set; }
    [ObservableProperty] public partial string CustomSceneName { get; set; } = "";
    [ObservableProperty] public partial string CustomScenePrompt { get; set; } = "";
    [ObservableProperty] public partial string AccountHint { get; set; } = "正在检测微信账户…";
    [ObservableProperty] public partial int PruneRetentionDays { get; set; } = 30;
    [ObservableProperty] public partial bool IsPruneExecuting { get; set; }

    public IReadOnlyList<int> PruneRetentionDayOptions { get; } = [30, 90, 180, 365];
    public bool CanStartOperation => _operationGate.CanStart(IsBusy, _closing);
    public bool CanWork => _operationGate.CanStart(IsBusy, _closing) && IsConnected && _worker?.IsAvailable == true;
    public bool CanManageJobs => _operationGate.CanManageJobs(IsBusy, _closing, _worker?.IsAvailable == true);
    public bool HasGlobalSearchCapability => _workerCapabilities.Contains("global_message_search") && _workerCapabilities.Contains("message_context");
    public string? SourceSessionId => _globalSearchState.SourceSessionId;
    public bool CanUseGlobalSearch => CanWork && HasGlobalSearchCapability && !string.IsNullOrWhiteSpace(SourceSessionId);
    public bool IsGlobalSearchUnavailable => IsConnected && !IsBusy && !CanUseGlobalSearch;
    public bool CanSwitchToGlobalSearch => CanUseGlobalSearch && !IsGlobalSearchMode;
    public bool CanSwitchToConversation => CanWork && IsGlobalSearchMode && !IsContextMode;
    public bool CanEditGlobalFilters => CanUseGlobalSearch && IsGlobalSearchMode && !IsContextMode;
    public bool CanApplyFilters => CanWork && (IsGlobalSearchMode
        ? CanUseGlobalSearch && !IsContextMode && !string.IsNullOrWhiteSpace(GlobalSearchQuery)
        : SelectedConversation is not null && (SearchQuery.Length == 0 || !string.IsNullOrWhiteSpace(SearchQuery)));
    public bool CanClearFilters => CanWork && (IsGlobalSearchMode
        ? CanUseGlobalSearch && !IsContextMode
        : SelectedConversation is not null);
    public bool CanExport => _operationGate.CanExport(IsBusy, _closing, IsConnected,
        SelectedConversation is not null, !FiltersDirty) && _worker?.IsAvailable == true && !IsGlobalSearchMode && !IsContextMode;
    public bool CanLoadMore => CanExport && !IsGlobalSearchMode && _messagePageWindow is null && _nextOffset is not null;
    public bool CanLoadMoreGlobal => CanUseGlobalSearch && IsGlobalSearchMode && !IsContextMode && _globalSearchState.CanContinue;
    public bool CanNavigateGlobalHits => CanUseGlobalSearch && IsGlobalSearchMode && !IsContextMode && _globalSearchState.CanNavigate;
    public bool HasError => Error.Length > 0;
    public bool HasResult => ResultPath.Length > 0;
    public bool HasResultSummary => ResultSummary.Length > 0;
    public bool IsEmpty => !IsBusy && (IsContextMode ? ContextMessages.Count == 0 :
        IsGlobalSearchMode ? GlobalSearchHits.Count == 0 : Messages.Count == 0);
    public bool ShowConversationMessages => !IsGlobalSearchMode && !IsContextMode;
    public bool ShowGlobalSearchResults => IsGlobalSearchMode && !IsContextMode;
    public bool ShowMessageContext => IsContextMode;
    public string EmptyTitle => IsContextMode ? "没有可显示的上下文" :
        IsGlobalSearchMode ? _globalSearchState.Snapshot is null ? "尚无搜索结果" : "没有符合条件的消息" : "没有可显示的消息";
    public string EmptyDescription => IsContextMode ? "返回搜索结果，或在目标会话中浏览。" :
        IsGlobalSearchMode ? GlobalSearchDirty ? "搜索条件已修改；请重新执行后再翻页或定位。" :
            _globalSearchState.Snapshot is null ? "输入关键词并执行全局搜索。" : "尝试其他关键词或日期范围。" :
        "选择会话，或调整日期与关键词。";
    public int ContextFocusIndex => ContextMessages.Select((message, index) => (message, index))
        .Where(entry => entry.message.IsContextFocus).Select(entry => entry.index).DefaultIfEmpty(-1).First();
    public string SelectedTitle => IsContextMode ? ContextConversation?.Title ?? "消息上下文" :
        IsGlobalSearchMode ? "全局搜索结果" : SelectedConversation?.Title ?? "消息预览";
    public string BrowseModeLabel => IsContextMode ? "消息上下文" : IsGlobalSearchMode ? "全局搜索" : "当前会话";
    public string GlobalSearchSummary => _globalSearchState.Snapshot is { } snapshot
        ? $"已载入 {snapshot.Items.Count:N0} 条。{snapshot.Summary}" : "输入关键词后执行全局搜索。";
    public string GlobalAppliedFiltersLabel => _globalSearchState.AppliedCriteria is { } criteria
        ? $"已应用：{criteria.Query} · {criteria.StartDate ?? "不限"} 至 {criteria.EndDate ?? "不限"}（包含，UTC+08:00）· {(criteria.Order == "asc" ? "最早" : "最新")}" : "";
    public string GlobalSearchStatus => _globalSearchState.IsInvalidated ? _globalSearchState.InvalidationNotice :
        GlobalSearchDirty ? "搜索条件已修改；现有结果仅供参考。重新执行后才能继续翻页或定位。" : "";
    public string GlobalCoverageNotice => _globalSearchState.Snapshot?.CoverageNotice ?? "";
    public bool CanSelectConversation => CanWork && !IsContextMode;
    public Conversation? ContextConversation { get; private set; }
    public double GlobalReturnScrollOffset => _globalReturnScrollOffset;
    public bool CanReturnToSearch => IsGlobalSearchMode && IsContextMode && CanWork;
    public bool CanBrowseContextConversation => IsGlobalSearchMode && IsContextMode && CanWork;
    public bool CanConnect => _operationGate.CanReconnect(IsBusy, _closing);
    public bool CanConnectWechat => _operationGate.CanConnectWechat(IsBusy, _closing, IsConnected, Accounts.Count > 0);
    public bool CanImportJson => _operationGate.CanImportJson(IsBusy, _closing, _worker?.IsAvailable == true);
    public bool CanTranscribe => CanWork && !_offlineMode && SelectedConversation is not null && EnableVoice && !FiltersDirty
        && !IsGlobalSearchMode && !IsContextMode && (_demoMode || _connectedMedia?.VoiceReady == true);
    public bool IsWechatMode => !_demoMode && !_offlineMode;
    public bool IsOfflineMode => _offlineMode;
    public bool IsDemoMode => _demoMode;
    public string SourceLabel => _offlineMode
        ? $"离线 JSON · 只读 · {OfflineFileName}"
        : _demoMode ? "自动化测试数据 · 不读取微信" : "本机微信 4.x · 只读 · 本地处理";
    public string SourceModeLabel => _offlineMode
        ? "离线 JSON · 只读"
        : _demoMode ? "自动化演示数据 · 只读" : "本机微信 4.x · 只读";
    private string OfflineFileName => string.IsNullOrWhiteSpace(_offlinePath)
        ? "已选择文件" : Path.GetFileName(_offlinePath);
    public string SelectedScenePrompt => SelectedScene?.Prompt ?? "";
    public bool HasScenePrompt => !string.IsNullOrWhiteSpace(SelectedScenePrompt);
    public bool CanSaveScene => CanWork && CustomSceneName.Trim().Length > 0 && CustomScenePrompt.Trim().Length > 0;
    public bool CanDeleteScene => _operationGate.CanDeleteScene(IsBusy, _closing, SelectedScene is { Builtin: false });
    public bool HasJobs => Jobs.Count > 0;
    public bool CanExportJob => CanExport && (ExportMarkdown || ExportJson || ExportPdf);

    public IAsyncRelayCommand ReconnectCommand { get; }
    public IAsyncRelayCommand ConnectWechatCommand { get; }
    public IAsyncRelayCommand RefreshAccountsCommand { get; }
    public IAsyncRelayCommand DisconnectCommand { get; }
    public IAsyncRelayCommand SearchConversationsCommand { get; }
    public IAsyncRelayCommand ApplyFiltersCommand { get; }
    public IAsyncRelayCommand ClearFiltersCommand { get; }
    public IAsyncRelayCommand LoadMoreCommand { get; }
    public IAsyncRelayCommand LoadMoreGlobalCommand { get; }
    public IRelayCommand SwitchToGlobalSearchCommand { get; }
    public IAsyncRelayCommand SwitchToConversationCommand { get; }
    public IAsyncRelayCommand ReturnToSearchCommand { get; }
    public IAsyncRelayCommand BrowseContextConversationCommand { get; }
    public IAsyncRelayCommand ExportJobCommand { get; }
    public IAsyncRelayCommand SaveSceneCommand { get; }
    public IAsyncRelayCommand DeleteSceneCommand { get; }
    public IAsyncRelayCommand RefreshJobsCommand { get; }
    public IAsyncRelayCommand TranscribeCommand { get; }
    public IAsyncRelayCommand RefreshVoiceCommand { get; }
    public IAsyncRelayCommand PrepareVoiceCommand { get; }

    // The platform adapter supplies dispatch/startup. Tests exercise these exact
    // production commands and state transitions through an isolated worker pipe.
    internal MainViewModel(Action<Action> enqueue, Func<WorkerClient> startWorker, bool demoMode,
        Action<string, string>? log = null)
    {
        _enqueue = enqueue;
        _startWorker = startWorker;
        _log = log ?? ((name, code) => AppLog.Write(name, code));
        _demoMode = demoMode;
        UseWechat = !_demoMode;
        ReconnectCommand = new AsyncRelayCommand(InitializeAsync, () => CanConnect);
        ConnectWechatCommand = new AsyncRelayCommand(ConnectWechatAsync, () => CanConnectWechat);
        RefreshAccountsCommand = new AsyncRelayCommand(RefreshAccountsAsync, () => CanConnect);
        DisconnectCommand = new AsyncRelayCommand(DisconnectAsync, () => CanWork);
        SearchConversationsCommand = new AsyncRelayCommand(SearchConversationsAsync, () => CanSelectConversation);
        ApplyFiltersCommand = new AsyncRelayCommand(ApplyFiltersAsync, () => CanApplyFilters);
        ClearFiltersCommand = new AsyncRelayCommand(ClearFiltersAsync, () => CanClearFilters);
        LoadMoreCommand = new AsyncRelayCommand(() => CanLoadMore
            ? RunAsync(() => FetchAsync(true), "正在加载下一页…") : Task.CompletedTask, () => CanLoadMore);
        PreviousMessageBatchCommand = new AsyncRelayCommand(() => NavigateMessageBatchAsync(true), () => CanLoadPreviousMessageBatch);
        NextMessageBatchCommand = new AsyncRelayCommand(() => NavigateMessageBatchAsync(false), () => CanLoadNextMessageBatch);
        RefreshMessagesCommand = new AsyncRelayCommand(() => CanRefreshMessages
            ? RunAsync(() => FetchAsync(false), "正在刷新消息首屏…") : Task.CompletedTask, () => CanRefreshMessages);
        LoadMoreGlobalCommand = new AsyncRelayCommand(LoadMoreGlobalAsync, () => CanLoadMoreGlobal);
        SwitchToGlobalSearchCommand = new RelayCommand(SwitchToGlobalSearch, () => CanSwitchToGlobalSearch);
        SwitchToConversationCommand = new AsyncRelayCommand(SwitchToConversationAsync, () => CanSwitchToConversation);
        ReturnToSearchCommand = new AsyncRelayCommand(ReturnToSearchAsync, () => CanReturnToSearch);
        BrowseContextConversationCommand = new AsyncRelayCommand(BrowseContextConversationAsync, () => CanBrowseContextConversation);
        ExportJobCommand = new AsyncRelayCommand(ExportJobAsync, () => CanExportJob);
        SaveSceneCommand = new AsyncRelayCommand(SaveSceneAsync, () => CanSaveScene);
        DeleteSceneCommand = new AsyncRelayCommand(DeleteSceneAsync, () => CanDeleteScene);
        RefreshJobsCommand = new AsyncRelayCommand(LoadJobsAsync, () => CanManageJobs);
        TranscribeCommand = new AsyncRelayCommand(TranscribeAsync, () => CanTranscribe);
        RefreshVoiceCommand = new AsyncRelayCommand(RefreshVoiceStatusAsync, () => CanWork);
        PrepareVoiceCommand = new AsyncRelayCommand(PrepareVoiceModelAsync, () => CanWork);
        Status = _demoMode ? "正在准备测试数据…" : "正在检测本机微信…";
        AccountHint = _demoMode ? "自动化测试数据，不读取微信。" : "正在检测微信账户…";
    }

    partial void OnSelectedConversationChanged(Conversation? value)
    {
        if (!_suppressConversationReset)
        {
            ResetMessagePaging();
            Messages.Clear(); _nextOffset = null; _messageConversationId = null; FiltersDirty = true;
        }
        ResultPath = ""; ResultSummary = "";
        UpdateFlags();
        if (!_suppressConversationAutoFetch && !IsGlobalSearchMode && !IsContextMode && value is not null && CanWork)
            _ = ApplyFiltersCommand.ExecuteAsync(null);
    }
    partial void OnStartDateChanged(DateTimeOffset? value) { if (!_suppressSceneDates) MarkDirty(); }
    partial void OnEndDateChanged(DateTimeOffset? value) { if (!_suppressSceneDates) MarkDirty(); }
    partial void OnSearchQueryChanged(string value) => MarkDirty();
    partial void OnGlobalSearchQueryChanged(string value) => UpdateGlobalCriteriaState();
    partial void OnGlobalStartDateChanged(DateTimeOffset? value) => UpdateGlobalCriteriaState();
    partial void OnGlobalEndDateChanged(DateTimeOffset? value) => UpdateGlobalCriteriaState();
    partial void OnGlobalSearchOrderChanged(string value) => UpdateGlobalCriteriaState();
    partial void OnIsGlobalSearchModeChanged(bool value) => UpdateFlags();
    partial void OnIsContextModeChanged(bool value) => UpdateFlags();
    partial void OnUseWechatChanged(bool value)
    {
        _connectedMedia = null;
        if (IsConnected) { IsConnected = false; ConnectionLabel = "请重新连接"; }
        if (!value && _worker?.IsAvailable == true) _ = DisconnectSourceInBackgroundAsync();
        Conversations.Clear(); Messages.Clear(); Accounts.Clear(); SelectedAccount = null;
        AccountHint = value ? "点击刷新账户，然后连接；连接会重启微信一次。" : "演示数据不读取任何微信数据。";
        Status = value ? "已切换到真实微信（只读），请刷新账户并连接。" : "已切换到演示数据，请重新连接。";
        UpdateFlags();
    }

    private async Task DisconnectSourceInBackgroundAsync()
    {
        var worker = _worker;
        if (worker?.IsAvailable != true) return;
        try { await worker.RequestAsync("disconnect", new { }, TimeSpan.FromSeconds(10)); }
        catch (WorkerException) { }
    }
    partial void OnSelectedSceneChanged(SceneItem? value)
    {
        if (value is null) return;
        if (!SceneWindowResolver.TryResolve(value, DateTimeOffset.UtcNow, out var window, out var dateError))
        {
            Error = dateError;
            Status = "场景的日期范围无效，未应用该场景。";
            OnPropertyChanged(nameof(SelectedScenePrompt));
            OnPropertyChanged(nameof(HasScenePrompt));
            return;
        }
        _suppressSceneDates = true;
        try
        {
            StartDate = window.StartDate;
            EndDate = window.EndDate;
        }
        finally { _suppressSceneDates = false; }
        // A scene replaces the whole filter: a scene without a query must not
        // inherit the previous keyword.
        SearchQuery = value.Query ?? "";
        OnPropertyChanged(nameof(SelectedScenePrompt));
        OnPropertyChanged(nameof(HasScenePrompt));
        Status = $"已应用场景：{value.Name}。日期与关键词已更新，请应用筛选。";
        MarkDirty();
    }
    partial void OnTargetKindChanged(string value) => UpdateFlags();
    partial void OnExportMarkdownChanged(bool value) => UpdateFlags();
    partial void OnExportJsonChanged(bool value) => UpdateFlags();
    partial void OnExportPdfChanged(bool value) => UpdateFlags();
    partial void OnEnableVoiceChanged(bool value)
    {
        UpdateFlags();
        if (_demoMode || !IsConnected || _connectedMedia is null) return;
        if (!_connectedMedia.NeedsVoiceReconnect(value)) return;
        // The worker builds its voice cache only during connect, exactly like the
        // image cache. Require the same explicit reconnect; never restart WeChat
        // from a checkbox change.
        _connectedMedia = null;
        IsConnected = false;
        ConnectionLabel = "语音选项已更改 · 请重新连接";
        Status = "开启语音转写需要重新连接微信后生效。请点击“连接微信”并按提示确认重启微信。";
    }
    partial void OnEnableImagesChanged(bool value)
    {
        UpdateFlags();
        if (_demoMode || !IsConnected || _connectedMedia is null) return;
        if (!_connectedMedia.NeedsImageReconnect(value)) return;
        // The worker opened its image cache (or not) during connect. Flipping the
        // checkbox cannot update that source, so require an explicit reconnect;
        // the Connect button's confirmation dialog prevents a silent WeChat restart.
        _connectedMedia = null;
        IsConnected = false;
        ConnectionLabel = "图片选项已更改 · 请重新连接";
        Status = "开启图片恢复需要重新连接微信后生效。请点击“连接微信”并按提示确认重启微信。";
    }
    partial void OnIsBusyChanged(bool value) => UpdateFlags();
    partial void OnIsConnectedChanged(bool value)
    {
        if (!value) InvalidateGlobalSource();
        else UpdateFlags();
    }
    partial void OnFiltersDirtyChanged(bool value) => UpdateFlags();
    partial void OnErrorChanged(string value) => OnPropertyChanged(nameof(HasError));
    partial void OnResultPathChanged(string value) => OnPropertyChanged(nameof(HasResult));
    partial void OnResultSummaryChanged(string value) => OnPropertyChanged(nameof(HasResultSummary));
    partial void OnSelectedAccountChanged(AccountItem? value) => UpdateFlags();
    partial void OnCustomSceneNameChanged(string value) => UpdateFlags();
    partial void OnCustomScenePromptChanged(string value) => UpdateFlags();

    private void MarkDirty()
    {
        if (!IsConnected) return;
        FiltersDirty = true;
        Status = "筛选条件已修改，请应用筛选后再导出。";
        UpdateFlags();
    }

    private GlobalSearchCriteria CurrentGlobalCriteria() => new(
        GlobalSearchQuery.Trim(),
        GlobalStartDate?.ToString("yyyy-MM-dd", System.Globalization.CultureInfo.InvariantCulture),
        GlobalEndDate?.ToString("yyyy-MM-dd", System.Globalization.CultureInfo.InvariantCulture),
        GlobalSearchOrder);

    private void UpdateGlobalCriteriaState()
    {
        _globalSearchState.UpdateCriteria(CurrentGlobalCriteria());
        OnPropertyChanged(nameof(GlobalSearchDirty));
        UpdateFlags();
    }

    public bool GlobalSearchDirty => _globalSearchState.IsDirty;

    private void SetGlobalSource(string? sourceSessionId)
    {
        ++_sourceEpoch;
        _globalSearchState.ActivateSource(sourceSessionId);
        ResetMessagePaging(sourceChanged: true);
        // No ordinary preview or export scope may survive a different source.
        _suppressConversationAutoFetch = true;
        try { SelectedConversation = null; }
        finally { _suppressConversationAutoFetch = false; }
        Conversations.Clear();
        Messages.Clear();
        _nextOffset = null;
        _messageConversationId = null;
        _applied = new DateRange(null, null);
        MessageSummary = "请选择一个会话。";
        RangeLabel = "全部日期 · UTC+08:00";
        FiltersDirty = true;
        ResultPath = "";
        ResultSummary = "";
        GlobalSearchHits.Clear();
        ContextMessages.Clear();
        ContextConversation = null;
        ContextSummary = "";
        ContextCoverageNotice = "";
        IsContextMode = false;
        IsGlobalSearchMode = false;
        _preContextConversation = null;
        _globalReturnScrollOffset = 0;
        OnPropertyChanged(nameof(ContextConversation));
        OnPropertyChanged(nameof(SourceSessionId));
        OnPropertyChanged(nameof(GlobalSearchDirty));
        OnPropertyChanged(nameof(GlobalSearchSummary));
        OnPropertyChanged(nameof(GlobalCoverageNotice));
        UpdateFlags();
    }

    private void InvalidateGlobalSource() => SetGlobalSource(null);

    private void ClearGlobalResults()
    {
        _globalSearchState.ClearResults();
        GlobalSearchHits.Clear();
        ContextMessages.Clear();
        ContextConversation = null;
        ContextSummary = "";
        ContextCoverageNotice = "";
        IsContextMode = false;
        _preContextConversation = null;
        OnPropertyChanged(nameof(ContextConversation));
        OnPropertyChanged(nameof(GlobalSearchDirty));
        OnPropertyChanged(nameof(GlobalSearchSummary));
        OnPropertyChanged(nameof(GlobalCoverageNotice));
        UpdateFlags();
    }

    private void SwitchToGlobalSearch()
    {
        if (!CanSwitchToGlobalSearch) return;
        IsGlobalSearchMode = true;
        UpdateFlags();
    }

    private Task SwitchToConversationAsync() => !CanSwitchToConversation ? Task.CompletedTask : RunAsync(async () =>
    {
        // Restore an existing ordinary preview without reapplying its edited
        // filters. In particular, invalid ordinary dates must not trap the user
        // in global mode with the date controls hidden.
        IsGlobalSearchMode = false;
        if (SelectedConversation?.Id != _messageConversationId)
        {
            ResetMessagePaging();
            Messages.Clear(); _nextOffset = null; _messageConversationId = null; FiltersDirty = true;
            if (SelectedConversation is not null) await FetchAsync(false);
        }
        UpdateFlags();
    }, "正在恢复当前会话浏览…");

    private async Task ApplyFiltersAsync()
    {
        if (!CanApplyFilters) return;
        if (IsGlobalSearchMode) await ExecuteGlobalSearchAsync();
        else await RunAsync(() => FetchAsync(false), "正在筛选消息…");
    }

    private async Task ClearFiltersAsync()
    {
        if (!CanClearFilters) return;
        if (IsGlobalSearchMode)
        {
            GlobalStartDate = null;
            GlobalEndDate = null;
            GlobalSearchQuery = "";
            GlobalSearchOrder = "desc";
            ClearGlobalResults();
            Status = "全局搜索条件已清除。";
            return;
        }
        StartDate = null; EndDate = null; SearchQuery = "";
        await RunAsync(() => FetchAsync(false), "正在加载全部日期…");
    }

    private void UpdateFlags()
    {
        foreach (var name in new[]
        {
            nameof(CanStartOperation), nameof(CanWork), nameof(CanExport), nameof(CanLoadMore), nameof(IsEmpty), nameof(CanManageJobs),
            nameof(SelectedTitle), nameof(CanConnect), nameof(CanConnectWechat), nameof(CanImportJson),
            nameof(CanTranscribe), nameof(IsWechatMode), nameof(IsOfflineMode), nameof(IsDemoMode),
            nameof(SourceLabel), nameof(SourceModeLabel), nameof(HasGlobalSearchCapability), nameof(SourceSessionId),
            nameof(CanUseGlobalSearch), nameof(IsGlobalSearchUnavailable), nameof(CanSwitchToGlobalSearch), nameof(CanSwitchToConversation),
            nameof(CanEditGlobalFilters), nameof(CanApplyFilters), nameof(CanClearFilters), nameof(CanLoadMoreGlobal),
            nameof(CanNavigateGlobalHits), nameof(GlobalSearchDirty), nameof(GlobalSearchSummary), nameof(GlobalCoverageNotice), nameof(BrowseModeLabel),
            nameof(GlobalSearchStatus), nameof(GlobalAppliedFiltersLabel), nameof(CanSelectConversation),
            nameof(CanReturnToSearch), nameof(CanBrowseContextConversation), nameof(ShowConversationMessages),
            nameof(ShowGlobalSearchResults), nameof(ShowMessageContext), nameof(EmptyTitle), nameof(EmptyDescription),
            nameof(ContextFocusIndex), nameof(HasMessageCursorPaginationCapability), nameof(HasMessageKeywordPaginationCapability),
            nameof(ShowCursorMessageNavigation), nameof(ShowLegacyMessageNavigation), nameof(CanRefreshMessages),
            nameof(CanLoadPreviousMessageBatch), nameof(CanLoadNextMessageBatch), nameof(MessagePagingStatus),
            nameof(VisibleMessagePageCount), nameof(VisibleMessageDisplayBytes),
            nameof(CanSaveScene), nameof(CanDeleteScene), nameof(HasJobs), nameof(CanExportJob),
            nameof(SelectedScenePrompt), nameof(HasScenePrompt),
        }) OnPropertyChanged(name);
        ReconnectCommand?.NotifyCanExecuteChanged(); ConnectWechatCommand?.NotifyCanExecuteChanged();
        RefreshAccountsCommand?.NotifyCanExecuteChanged(); DisconnectCommand?.NotifyCanExecuteChanged();
        SearchConversationsCommand?.NotifyCanExecuteChanged();
        ApplyFiltersCommand?.NotifyCanExecuteChanged(); ClearFiltersCommand?.NotifyCanExecuteChanged();
        LoadMoreCommand?.NotifyCanExecuteChanged(); LoadMoreGlobalCommand?.NotifyCanExecuteChanged();
        PreviousMessageBatchCommand?.NotifyCanExecuteChanged(); NextMessageBatchCommand?.NotifyCanExecuteChanged();
        RefreshMessagesCommand?.NotifyCanExecuteChanged();
        SwitchToGlobalSearchCommand?.NotifyCanExecuteChanged(); SwitchToConversationCommand?.NotifyCanExecuteChanged();
        ReturnToSearchCommand?.NotifyCanExecuteChanged(); BrowseContextConversationCommand?.NotifyCanExecuteChanged();
        ExportJobCommand?.NotifyCanExecuteChanged();
        SaveSceneCommand?.NotifyCanExecuteChanged(); DeleteSceneCommand?.NotifyCanExecuteChanged();
        RefreshJobsCommand?.NotifyCanExecuteChanged(); TranscribeCommand?.NotifyCanExecuteChanged();
        RefreshVoiceCommand?.NotifyCanExecuteChanged(); PrepareVoiceCommand?.NotifyCanExecuteChanged();
    }

    private string CurrentSource => _offlineMode ? "offline" : UseWechat ? "wechat4" : "demo";

    // ------------------------------------------------------------------ //
    // Connection
    // ------------------------------------------------------------------ //

    /// <summary>
    /// Starts the worker and discovers accounts. Real mode deliberately does
    /// not connect (or restart WeChat) until the user asks to connect.
    /// </summary>
    public Task InitializeAsync() => RunAsync(async () =>
    {
        _offlineMode = false;
        _offlinePath = null;
        _connectedMedia = null;
        IsConnected = false;
        Conversations.Clear(); Messages.Clear(); Jobs.Clear();
        ResultPath = ""; ResultSummary = "";
        var current = await StartWorkerAsync();

        if (_demoMode)
        {
            IsConnected = true;
            ConnectionLabel = "自动化测试数据 · 本地工作进程";
            Status = "测试数据已连接。";
            Log("demo_connected");
            await LoadConversationsAsync();
            await RefreshAuxiliaryAsync(current);
            OnPropertyChanged(nameof(ConnectionLabel));
            return;
        }

        ConnectionLabel = "尚未连接";
        await RefreshAccountsCoreAsync(current);
        await LoadJobsCoreAsync(current);
        Status = Accounts.Count == 0
            ? "没有找到微信账户。请先登录微信，然后点击“刷新”。"
            : $"找到 {Accounts.Count} 个微信账户。请选择账户后点击“连接微信”。";
        OnPropertyChanged(nameof(ConnectionLabel));
    }, _demoMode ? "正在准备测试数据…" : "正在检测微信账户…");

    private async Task<WorkerClient> StartWorkerAsync()
    {
        SetGlobalSource(null);
        _workerCapabilities.Clear();
        _messageCursorSources = null;
        _messageKeywordSources = null;
        if (_worker is not null) await _worker.DisposeAsync();
        _worker = _startWorker();
        var current = _worker;
        current.DiagnosticReceived += () => Log("worker_diagnostic_received");
        current.ProgressReceived += progress => _enqueue(() =>
        {
            if (!ReferenceEquals(_worker, current)) return;
            ProgressLabel = progress.Total > 0 ? $"{progress.Label}（{progress.Current}/{progress.Total}）" : progress.Label;
            if (!string.IsNullOrWhiteSpace(progress.Label)) Status = progress.Label;
        });
        current.Disconnected += failure => _enqueue(() =>
        {
            if (_closing || !ReferenceEquals(_worker, current)) return;
            _connectedMedia = null;
            InvalidateGlobalSource();
            IsConnected = false; ConnectionLabel = "连接已断开";
            // A late disconnect notification must not erase the conservative
            // PRUNE_RESULT_UNKNOWN warning for this worker instance.
            if (!_disconnectStatusGuard.ShouldPreserve(current))
            {
                Error = failure.Message;
                Status = "任务未完成，可重新检测。";
            }
            Log("worker_disconnected", failure.Code);
        });
        var hello = await current.RequestAsync("hello", new { });
        if (hello.GetProperty("protocolVersion").GetInt32() != Protocol.Version || hello.GetProperty("timezone").GetString() != "UTC+08:00")
            throw new WorkerException("PROTOCOL_ERROR", "工作进程的协议或时区不兼容。");
        _workerCapabilities.Clear();
        if (hello.TryGetProperty("capabilities", out var capabilityElement) && capabilityElement.ValueKind == JsonValueKind.Array)
            foreach (var capability in capabilityElement.EnumerateArray())
                if (capability.ValueKind == JsonValueKind.String && !string.IsNullOrWhiteSpace(capability.GetString()))
                    _workerCapabilities.Add(capability.GetString()!);
        ReadMessagePagingCapabilities(hello);
        if (hello.TryGetProperty("sourceSessionId", out var sessionElement) && sessionElement.ValueKind == JsonValueKind.String)
            SetGlobalSource(sessionElement.GetString());
        else
            UpdateFlags();
        return current;
    }

    private async Task ConnectWechatAsync() => await RunAsync(async () =>
    {
        _connectedMedia = null;
        IsConnected = false;
        var current = _worker is { IsAvailable: true } ? _worker : await StartWorkerAsync();
        await RefreshAccountsCoreAsync(current);
        if (SelectedAccount is null)
            throw new WorkerException("ACCOUNT_NOT_FOUND", "没有找到可用的微信账户。请先登录微信，然后刷新账户。");
        Status = "正在连接微信。连接会重启微信一次，并可能要求在手机上确认登录…";
        var connected = (await current.RequestAsync("connect", new
        {
            source = "wechat4", accountToken = SelectedAccount.Token,
            enableImages = EnableImages, enableVoice = EnableVoice
        }, TimeSpan.FromMinutes(6))).Deserialize<ConnectResult>(Protocol.Json)
            ?? throw new WorkerException("PROTOCOL_ERROR", "微信连接结果格式不正确。");
        if (!connected.Connected || connected.Source != "wechat4" || connected.ConversationCount < 0 || connected.MessageTableCount < 0)
            throw new WorkerException("PROTOCOL_ERROR", "微信连接结果未确认有效的数据源。");
        _connectedMedia = new ConnectionMedia(EnableImages, connected.ImageKeyAvailable,
            EnableVoice, connected.VoiceAvailable);
        SetGlobalSource(connected.SourceSessionId);
        IsConnected = true;
        RememberAccountName(connected.AccountLabel);
        ConnectionLabel = $"{connected.AccountLabel ?? SelectedAccount.Label} · 只读";
        Status = $"微信已连接：{connected.ConversationCount} 个会话，{connected.MessageTableCount} 张消息表。";
        Log("wechat_connected");
        await LoadConversationsAsync();
        await RefreshAuxiliaryAsync(current);
        if (EnableImages && !connected.ImageKeyAvailable)
            Status = "微信已连接，但没有取得图片密钥，图片恢复不可用。";
        OnPropertyChanged(nameof(ConnectionLabel));
    }, "正在连接微信…");

    private async Task RefreshAccountsAsync() => await RunAsync(async () =>
    {
        var current = _worker is { IsAvailable: true } ? _worker : await StartWorkerAsync();
        await RefreshAccountsCoreAsync(current);
        if (SelectedAccount is null) throw new WorkerException("ACCOUNT_NOT_FOUND", "没有找到可用的微信账户。");
        Status = $"已找到 {Accounts.Count} 个微信账户，请点击“连接微信”。";
    }, "正在检测本机微信账户…");

    private async Task RefreshAccountsCoreAsync(WorkerClient worker)
    {
        var previousToken = SelectedAccount?.Token;
        var list = (await worker.RequestAsync("list_accounts", new { }, TimeSpan.FromSeconds(60)))
            .Deserialize<AccountList>(Protocol.Json) ?? new AccountList([]);
        Accounts.Clear();
        foreach (var item in list.Items)
        {
            Accounts.Add(_accountNames.TryGetValue(item.Token, out var name) && !string.IsNullOrWhiteSpace(name)
                ? item with { Label = name }
                : item);
        }
        SelectedAccount = Accounts.FirstOrDefault(item => item.Token == previousToken) ?? Accounts.FirstOrDefault();
        AccountHint = Accounts.Count == 0
            ? "没有找到微信 4.x 数据目录。请确认微信已登录过。"
            : $"找到 {Accounts.Count} 个账户。连接会重启微信一次，连接后显示微信昵称。";
        UpdateFlags();
    }

    public Task ImportJsonAsync(string path) => RunAsync(async () =>
    {
        if (string.IsNullOrWhiteSpace(path) || !Path.IsPathFullyQualified(path) ||
            !string.Equals(Path.GetExtension(path), ".json", StringComparison.OrdinalIgnoreCase))
            throw new WorkerException("INVALID_ARGUMENT", "请选择绝对路径的 JSON 文件。");
        var imported = await OfflineImportPreflight.ExecuteAsync(StartDate, EndDate, async () =>
        {
            var worker = _worker is { IsAvailable: true } ? _worker : await StartWorkerAsync();
            var response = await worker.RequestAsync("import_json", new { path }, TimeSpan.FromSeconds(120));
            return (Worker: worker, Response: response);
        });
        var current = imported.Worker;
        var result = imported.Response.Deserialize<OfflineImportResult>(Protocol.Json)
            ?? throw new WorkerException("PROTOCOL_ERROR", "离线导入结果格式不正确。");
        if (result.ConversationCount < 0 || result.MessageCount < 0 ||
            !string.Equals(result.Source, "offline", StringComparison.Ordinal) ||
            !string.Equals(result.Schema, "wechat-memory-json-v1", StringComparison.Ordinal) &&
            !string.Equals(result.Schema, "demo-json-v1", StringComparison.Ordinal))
            throw new WorkerException("PROTOCOL_ERROR", "工作进程返回了不兼容的离线数据源结果。");

        // The worker commits its complete validated snapshot before this point;
        // only now does the UI replace the active source and its visible state.
        _offlineMode = true;
        _offlinePath = path;
        _connectedMedia = null;
        SetGlobalSource(result.SourceSessionId);
        IsConnected = true;
        Accounts.Clear();
        SelectedAccount = null;
        AccountHint = "离线 JSON 已加载；不会读取微信、网络或 JSON 中指向的媒体文件。";
        ConnectionLabel = "离线 JSON · 只读";
        ResultPath = "";
        ResultSummary = "";
        OnPropertyChanged(nameof(SourceLabel));
        OnPropertyChanged(nameof(SourceModeLabel));
        await LoadConversationsAsync();
        await RefreshAuxiliaryAsync(current);
        Status = $"离线 JSON 已加载：{result.ConversationCount} 个会话，{result.MessageCount} 条消息。";
        Log("offline_json_imported");
    }, "正在验证并导入离线 JSON…");

    private void RememberAccountName(string? name)
    {
        if (SelectedAccount is null || string.IsNullOrWhiteSpace(name) ||
            name.StartsWith("微信账户", StringComparison.Ordinal))
            return;
        _accountNames[SelectedAccount.Token] = name;
        int index = Accounts.IndexOf(SelectedAccount);
        if (index < 0) return;
        var updated = SelectedAccount with { Label = name };
        Accounts[index] = updated;
        SelectedAccount = updated;
    }

    private async Task DisconnectAsync() => await RunAsync(async () =>
    {
        if (_worker?.IsAvailable == true)
        {
            try { await _worker.RequestAsync("disconnect", new { }, TimeSpan.FromSeconds(10)); }
            catch (WorkerException) { }
        }
        _connectedMedia = null;
        IsConnected = false; ConnectionLabel = "已断开";
        Conversations.Clear(); Messages.Clear(); Jobs.Clear();
        Status = "已断开微信。工作进程仍在运行。";
    }, "正在断开…");

    private async Task LoadConversationsAsync(string? query = null, bool loadMessages = true,
        bool preserveMessageState = false, string? preferredConversationId = null)
    {
        var current = _worker ?? throw new WorkerException("WORKER_EXITED", "工作进程不可用。");
        var previousId = preferredConversationId ?? SelectedConversation?.Id;
        var list = await RequestConversationsAsync(current, query);
        if (!ReferenceEquals(_worker, current) || !current.IsAvailable || !IsConnected || _closing)
            throw new WorkerException("SOURCE_CHANGED", "数据源已变化，未应用过期的会话列表。");
        if (!string.IsNullOrWhiteSpace(list.SourceSessionId) &&
            !string.Equals(list.SourceSessionId, _globalSearchState.SourceSessionId, StringComparison.Ordinal))
            SetGlobalSource(list.SourceSessionId);
        _suppressConversationReset = preserveMessageState;
        _suppressConversationAutoFetch = true;
        try
        {
            // ListView can synchronously clear its TwoWay SelectedItem when the
            // collection is reset; suppression must include the collection edit.
            Conversations.Clear();
            if (!preserveMessageState) Messages.Clear();
            ResultPath = ""; ResultSummary = "";
            foreach (var item in list.Items) Conversations.Add(item);
            SelectedConversation = Conversations.FirstOrDefault(item => item.Id == previousId) ?? Conversations.FirstOrDefault();
        }
        finally
        {
            _suppressConversationReset = false;
            _suppressConversationAutoFetch = false;
        }
        OnPropertyChanged(nameof(SelectedTitle));
        if (SelectedConversation is null) { MessageSummary = "没有可用会话。"; Status = "没有会话可显示。"; return; }
        if (loadMessages && !IsGlobalSearchMode && !IsContextMode) await FetchAsync(false);
    }

    private async Task<ConversationResult> RequestConversationsAsync(WorkerClient worker, string? query)
    {
        var list = (await worker.RequestAsync("list_conversations", new { source = CurrentSource, query }))
            .Deserialize<ConversationResult>(Protocol.Json)
            ?? throw new WorkerException("PROTOCOL_ERROR", "会话列表格式不正确。");
        if (list.Items is null || list.Items.Any(item => item is null || string.IsNullOrWhiteSpace(item.Id) ||
            item.Title is null || item.Kind is null || item.MessageCount < 0) ||
            list.Items.Select(item => item.Id).Distinct(StringComparer.Ordinal).Count() != list.Items.Count)
            throw new WorkerException("PROTOCOL_ERROR", "会话列表包含无效或重复的会话身份。");
        return list;
    }

    private Task SearchConversationsAsync() => !CanSelectConversation ? Task.CompletedTask : RunAsync(async () =>
    {
        var query = string.IsNullOrWhiteSpace(ConversationQuery) ? null : ConversationQuery.Trim();
        await LoadConversationsAsync(query, preserveMessageState: IsGlobalSearchMode);
        Status = query is null ? "已显示全部会话。" : $"已按会话名称筛选：{query}";
    }, "正在筛选会话…");

    private async Task RefreshAuxiliaryAsync(WorkerClient current)
    {
        try
        {
            await current.RequestAsync("recover_jobs", new { }, TimeSpan.FromSeconds(60));
        }
        catch (WorkerException) { }
        await LoadScenesAsync(current);
        await LoadJobsCoreAsync(current);
        // faster-whisper can take seconds to import; never hold the connected UI on it.
        _ = RefreshVoiceStatusCoreAsync(current);
    }

    // ------------------------------------------------------------------ //
    // Messages and export
    // ------------------------------------------------------------------ //

    private async Task FetchAsync(bool more, Conversation? targetConversation = null)
    {
        var conversation = targetConversation ?? SelectedConversation;
        if (conversation is null) { MessageSummary = "没有可用会话。"; Status = "没有会话。"; return; }
        var worker = _worker is { IsAvailable: true } current ? current
            : throw new WorkerException("WORKER_EXITED", "工作进程不可用，请重新检测。");
        long epoch = _sourceEpoch;
        var range = more ? _applied : DateRange.Create(StartDate, EndDate);
        var query = string.IsNullOrWhiteSpace(SearchQuery) ? null : SearchQuery.Trim();
        if (!more && query is not null && await TryFetchKeywordFirstAsync(
                worker, conversation, range, query, epoch, targetConversation is not null)) return;
        if (!more && await TryFetchCursorFirstAsync(worker, conversation, range, query, epoch, targetConversation is not null)) return;
        int offset = more ? _nextOffset ?? 0 : 0;
        var result = (await worker.RequestAsync("get_messages", new
        {
            source = CurrentSource, conversationId = conversation.Id,
            startDate = range.StartDate, endDate = range.EndDate,
            query, order = "desc", offset, limit = 50
        })).Deserialize<MessageResult>(Protocol.Json) ?? throw new WorkerException("PROTOCOL_ERROR", "消息列表格式不正确。");
        EnsureMessageRequestCurrent(worker, conversation, range, query, epoch, targetConversation is not null);
        if (result.Items is null || result.Total < offset || result.Items.Count > 50 ||
            result.Items.Count > result.Total - offset ||
            result.NextOffset != (offset + result.Items.Count < result.Total ? offset + result.Items.Count : (int?)null) ||
            result.Items.Count == 0 && offset < result.Total ||
            result.Items.Any(item => item is null || string.IsNullOrWhiteSpace(item.Id) || item.Sender is null ||
                item.Content is null || item.Kind is null || !DateTimeOffset.TryParse(item.Timestamp,
                    System.Globalization.CultureInfo.InvariantCulture, System.Globalization.DateTimeStyles.AssumeUniversal, out _)))
            throw new WorkerException("PROTOCOL_ERROR", "消息页内容、总数或分页位置不一致。");
        foreach (var item in result.Items) MessageProjectionReader.Validate(item);

        // A context -> ordinary transition does not change selection until its
        // first page has succeeded and passed all validation above.
        CommitMessageSelection(conversation, targetConversation is not null);
        ResetMessagePaging();
        _messagePagingNotice = LegacyPagingNotice(query, conversation.Id);
        if (!more) Messages.Clear();
        foreach (var item in result.Items) Messages.Add(item);
        _nextOffset = result.NextOffset; _applied = range; _messageConversationId = conversation.Id;
        FiltersDirty = false;
        RangeLabel = query is null ? range.Label : $"{range.Label} · 搜索“{query}”";
        MessageSummary = result.Total == 0 ? "此会话在所选条件下没有消息。" : $"已显示 {Messages.Count} / {result.Total} 条消息";
        Status = result.Total == 0 ? "筛选完成，没有符合条件的消息。" : "消息已加载，可按当前范围导出。";
        UpdateFlags();
    }

    private Task ExecuteGlobalSearchAsync() => RunAsync(async () =>
    {
        var worker = _worker is { IsAvailable: true } current
            ? current : throw new WorkerException("WORKER_EXITED", "工作进程不可用，请重新检测。");
        string sourceSessionId = _globalSearchState.SourceSessionId
            ?? throw new WorkerException("CAPABILITY_UNAVAILABLE", "当前数据源未提供全局搜索能力。");
        if (!HasGlobalSearchCapability)
            throw new WorkerException("CAPABILITY_UNAVAILABLE", "当前工作进程不支持全局搜索。");
        var criteria = CurrentGlobalCriteria();
        _ = DateRange.Create(GlobalStartDate, GlobalEndDate);
        long epoch = _sourceEpoch;
        var candidate = await _globalSearchWorkflow.SearchFirstPageAsync(worker, CurrentSource, sourceSessionId, criteria);
        EnsureGlobalSourceCurrent(worker, sourceSessionId, epoch);
        if (CurrentGlobalCriteria() != criteria)
        {
            _globalSearchState.UpdateCriteria(CurrentGlobalCriteria());
            OnPropertyChanged(nameof(GlobalSearchDirty));
            UpdateFlags();
            throw new WorkerException("SEARCH_STALE", "搜索条件已变化，未应用迟到的结果。请重新执行搜索。");
        }
        _globalSearchState.CommitSearch(criteria, candidate);
        GlobalSearchHits.Clear();
        foreach (var hit in candidate.Items) GlobalSearchHits.Add(hit);
        ContextMessages.Clear();
        ContextConversation = null;
        IsContextMode = false;
        OnPropertyChanged(nameof(ContextConversation));
        OnPropertyChanged(nameof(GlobalSearchDirty));
        OnPropertyChanged(nameof(GlobalSearchSummary));
        OnPropertyChanged(nameof(GlobalCoverageNotice));
        Status = candidate.Total == 0
            ? "全局搜索完成，没有符合条件的消息。"
            : $"全局搜索完成：{candidate.Items.Count:N0} / {candidate.Total:N0} 条命中已载入。";
        UpdateFlags();
    }, "正在搜索当前数据源中的全部会话…");

    private Task LoadMoreGlobalAsync() => !CanLoadMoreGlobal ? Task.CompletedTask : RunAsync(async () =>
    {
        var worker = _worker is { IsAvailable: true } current
            ? current : throw new WorkerException("WORKER_EXITED", "工作进程不可用，请重新检测。");
        var sourceSessionId = _globalSearchState.SourceSessionId
            ?? throw new WorkerException("SOURCE_CHANGED", "数据源已变化，请重新搜索。");
        var snapshot = _globalSearchState.Snapshot
            ?? throw new WorkerException("PAGE_EXPIRED", "全局搜索结果已过期，请重新搜索。");
        int previousCount = snapshot.Items.Count;
        long epoch = _sourceEpoch;
        var candidate = await _globalSearchWorkflow.SearchNextPageAsync(worker, CurrentSource, snapshot);
        EnsureGlobalSourceCurrent(worker, sourceSessionId, epoch);
        _globalSearchState.CommitNextPage(candidate);
        foreach (var hit in candidate.Items.Skip(previousCount)) GlobalSearchHits.Add(hit);
        OnPropertyChanged(nameof(GlobalSearchSummary));
        OnPropertyChanged(nameof(GlobalCoverageNotice));
        Status = $"已载入 {GlobalSearchHits.Count:N0} / {candidate.Total:N0} 条全局搜索命中。";
        UpdateFlags();
    }, "正在加载下一页全局搜索结果…");

    private void EnsureGlobalSourceCurrent(WorkerClient worker, string sourceSessionId, long epoch)
    {
        if (epoch != _sourceEpoch || !ReferenceEquals(_worker, worker) || !worker.IsAvailable || !IsConnected || _closing ||
            !string.Equals(_globalSearchState.SourceSessionId, sourceSessionId, StringComparison.Ordinal))
            throw new WorkerException("SOURCE_CHANGED", "数据源已变化，未应用过期的搜索结果。");
    }

    private void EnsureGlobalSnapshotCurrent(GlobalSearchSnapshot snapshot)
    {
        if (!_globalSearchState.CanNavigate ||
            !string.Equals(_globalSearchState.Snapshot?.SearchId, snapshot.SearchId, StringComparison.Ordinal))
            throw new WorkerException("SEARCH_STALE", "搜索条件或结果集已变化，请重新执行搜索。");
    }

    public async Task<bool> OpenGlobalHitAsync(GlobalSearchHit hit, double returnScrollOffset = 0)
    {
        if (!CanNavigateGlobalHits || _globalSearchState.Snapshot is not { } snapshot ||
            !GlobalSearchHits.Any(item => item.HitId == hit.HitId)) return false;
        bool opened = false;
        await RunAsync(async () =>
        {
            var worker = _worker is { IsAvailable: true } current
                ? current : throw new WorkerException("WORKER_EXITED", "工作进程不可用，请重新检测。");
            string sourceSessionId = _globalSearchState.SourceSessionId
                ?? throw new WorkerException("SOURCE_CHANGED", "数据源已变化，请重新搜索。");
            long epoch = _sourceEpoch;
            var context = await _globalSearchWorkflow.GetContextAsync(worker, CurrentSource, snapshot, hit);
            EnsureGlobalSourceCurrent(worker, sourceSessionId, epoch);
            EnsureGlobalSnapshotCurrent(snapshot);

            Conversation? previousConversation = SelectedConversation;
            bool conversationVisible = Conversations.Any(item => item.Id == context.Conversation.Id);
            bool conversationFilterHidTarget = !conversationVisible && !string.IsNullOrWhiteSpace(ConversationQuery);
            ConversationResult? expandedList = null;
            if (!conversationVisible)
            {
                // Stage the unfiltered list too. A failed list request or a changed
                // source must not partially clear the filter/selection behind the results.
                var list = await RequestConversationsAsync(worker, query: null);
                EnsureGlobalSourceCurrent(worker, sourceSessionId, epoch);
                EnsureGlobalSnapshotCurrent(snapshot);
                if (!string.Equals(list.SourceSessionId, sourceSessionId, StringComparison.Ordinal) ||
                    !list.Items.Any(item => item.Id == context.Conversation.Id && item.Kind == context.Conversation.Kind))
                    throw new WorkerException("SEARCH_SOURCE_CHANGED", "目标会话或数据源已变化，请重新搜索。");
                expandedList = list;
            }

            _preContextConversation = previousConversation;
            _globalReturnScrollOffset = double.IsFinite(returnScrollOffset) ? Math.Max(0, returnScrollOffset) : 0;
            _suppressConversationReset = true;
            _suppressConversationAutoFetch = true;
            try
            {
                if (expandedList is not null)
                {
                    Conversations.Clear();
                    foreach (var item in expandedList.Items) Conversations.Add(item);
                    ConversationQuery = "";
                }
                SelectedConversation = Conversations.FirstOrDefault(item => item.Id == context.Conversation.Id)
                    ?? context.Conversation;
            }
            finally { _suppressConversationReset = false; _suppressConversationAutoFetch = false; }

            ContextMessages.Clear();
            foreach (var item in context.Items) ContextMessages.Add(item);
            ContextConversation = context.Conversation;
            ContextSummary = $"上下文 {context.FocusIndex + 1} / {context.Items.Count}" +
                (context.HasEarlier ? " · 前面还有消息" : "") + (context.HasLater ? " · 后面还有消息" : "");
            ContextCoverageNotice = snapshot.CoverageNotice;
            IsGlobalSearchMode = true;
            IsContextMode = true;
            OnPropertyChanged(nameof(ContextConversation));
            OnPropertyChanged(nameof(SelectedTitle));
            Status = conversationFilterHidTarget
                ? "已清除会话名称筛选，并打开经过验证的消息上下文。"
                : "已打开经过验证的消息上下文；此范围不包含完整会话。";
            UpdateFlags();
            opened = true;
        }, "正在验证原消息并读取邻近上下文…");
        return opened;
    }

    private Task ReturnToSearchAsync()
    {
        if (!CanReturnToSearch) return Task.CompletedTask;
        var restore = _preContextConversation;
        _suppressConversationReset = true;
        _suppressConversationAutoFetch = true;
        try
        {
            SelectedConversation = restore is null ? null :
                Conversations.FirstOrDefault(item => item.Id == restore.Id) ?? restore;
        }
        finally { _suppressConversationReset = false; _suppressConversationAutoFetch = false; }
        ContextMessages.Clear();
        ContextConversation = null;
        ContextSummary = "";
        ContextCoverageNotice = "";
        IsContextMode = false;
        IsGlobalSearchMode = true;
        _preContextConversation = null;
        OnPropertyChanged(nameof(ContextConversation));
        OnPropertyChanged(nameof(SelectedTitle));
        Status = "已返回原全局搜索结果。";
        UpdateFlags();
        return Task.CompletedTask;
    }

    private Task BrowseContextConversationAsync() => !CanBrowseContextConversation ? Task.CompletedTask : RunAsync(async () =>
    {
        if (ContextConversation is null) throw new WorkerException("INVALID_ARGUMENT", "没有可浏览的目标会话。");
        var target = Conversations.FirstOrDefault(item => item.Id == ContextConversation.Id) ?? ContextConversation;
        await FetchAsync(false, target);
        ContextMessages.Clear();
        ContextConversation = null;
        ContextSummary = "";
        ContextCoverageNotice = "";
        IsContextMode = false;
        IsGlobalSearchMode = false;
        _preContextConversation = null;
        OnPropertyChanged(nameof(ContextConversation));
        OnPropertyChanged(nameof(SelectedTitle));
        Status = "已恢复为单会话浏览。";
        UpdateFlags();
    }, "正在恢复目标会话浏览…");

    public Task ExportAsync(string path) => ExportAsync(path, "markdown", false);

    public Task ExportAsync(string path, string format, bool includePages) => RunAsync(async () =>
    {
        if (IsGlobalSearchMode || IsContextMode)
            throw new WorkerException("INVALID_ARGUMENT", "全局搜索结果和有限上下文不能直接导出；请返回单会话浏览后再导出。");
        if (SelectedConversation is null || FiltersDirty) throw new WorkerException("INVALID_ARGUMENT", "请先选择会话并应用筛选。");
        var range = _applied;
        var query = string.IsNullOrWhiteSpace(SearchQuery) ? null : SearchQuery.Trim();
        var command = format switch
        {
            "json" => "export_json",
            "pdf" => "export_pdf",
            _ => "export_markdown",
        };
        string? pagesDir = null;
        if (format == "pdf" && includePages)
            pagesDir = Path.Combine(Path.GetDirectoryName(path)!, Path.GetFileNameWithoutExtension(path) + "_pages");
        var result = (await _worker!.RequestAsync(command, new
        {
            source = CurrentSource, conversationId = SelectedConversation.Id,
            startDate = range.StartDate, endDate = range.EndDate, query,
            outputPath = path, overwrite = true,
            pagesDir, materializeMedia = EnableImages,
        }, format == "pdf" ? TimeSpan.FromMinutes(10) : TimeSpan.FromSeconds(120)))
            .Deserialize<ExportDetail>(Protocol.Json) ?? throw new WorkerException("PROTOCOL_ERROR", "导出结果格式不正确。");
        if (!File.Exists(result.Path)) throw new WorkerException("EXPORT_FAILED", "工作进程未生成导出文件。");
        ResultPath = result.Path;
        ResultSummary = format switch
        {
            "pdf" => $"{result.MessageCount} 条消息 · {result.PageCount} 页 · {result.Bytes:N0} 字节",
            _ => $"{result.MessageCount} 条消息 · {result.Bytes:N0} 字节",
        };
        Status = $"导出完成：{ResultSummary}";
        Log("export_completed", format.ToUpperInvariant());
    }, format == "pdf" ? "正在生成 PDF…" : "正在导出…");

    // ------------------------------------------------------------------ //
    // Scenes, jobs and delivery
    // ------------------------------------------------------------------ //

    private async Task LoadScenesAsync(WorkerClient? worker = null)
    {
        worker ??= _worker;
        if (worker?.IsAvailable != true) return;
        try
        {
            var list = (await worker.RequestAsync("list_scenes", new { }, TimeSpan.FromSeconds(60)))
                .Deserialize<SceneList>(Protocol.Json);
            if (list is null) return;
            Scenes.Clear();
            foreach (var item in list.Items) Scenes.Add(item);
            OnPropertyChanged(nameof(Scenes));
        }
        catch (WorkerException e) when (e.Code is "CAPABILITY_UNAVAILABLE" or "JOB_STORE_UNAVAILABLE" or "SCENE_INVALID")
        {
            Scenes.Clear();
        }
        catch (WorkerException) { }
    }

    private async Task SaveSceneAsync() => await RunAsync(async () =>
    {
        var scene = SceneDraft.Create(
            SceneDraft.NewId(DateTimeOffset.UtcNow),
            CustomSceneName.Trim(),
            StartDate,
            EndDate,
            string.IsNullOrWhiteSpace(SearchQuery) ? null : SearchQuery.Trim(),
            includeImagePages: true,
            markdown: ExportMarkdown,
            jsonOutput: ExportJson,
            target: TargetKind == "none" ? "folder" : TargetKind,
            prompt: CustomScenePrompt.Trim());
        await _worker!.RequestAsync("save_scene", new { scene }, TimeSpan.FromSeconds(60));
        CustomSceneName = ""; CustomScenePrompt = "";
        await LoadScenesAsync();
        Status = "场景已保存。";
    }, "正在保存场景…");

    private async Task DeleteSceneAsync() => await RunAsync(async () =>
    {
        if (SelectedScene is null || SelectedScene.Builtin) return;
        await _worker!.RequestAsync("delete_scene", new { sceneId = SelectedScene.Id }, TimeSpan.FromSeconds(60));
        await LoadScenesAsync();
        Status = "场景已删除。";
    }, "正在删除场景…");

    private async Task ExportJobAsync() => await RunAsync(async () =>
    {
        if (IsGlobalSearchMode || IsContextMode)
            throw new WorkerException("INVALID_ARGUMENT", "全局搜索结果和有限上下文不能生成普通会话任务。");
        if (SelectedConversation is null || FiltersDirty) throw new WorkerException("INVALID_ARGUMENT", "请先选择会话并应用筛选。");
        var formats = new List<string>();
        if (ExportMarkdown) formats.Add("markdown");
        if (ExportJson) formats.Add("json");
        if (ExportPdf) formats.Add("pdf");
        if (formats.Count == 0) throw new WorkerException("INVALID_ARGUMENT", "请至少选择一种导出格式。");
        object target = TargetKind switch
        {
            "folder" when !string.IsNullOrWhiteSpace(TargetFolder) => new { kind = "folder", root = TargetFolder, organizeByDate = true },
            "obsidian" when !string.IsNullOrWhiteSpace(ObsidianVault) => new { kind = "obsidian", vault = ObsidianVault, folder = "微信记忆", attachments = "_附件", dedupe = true },
            _ => new { kind = "none" },
        };
        var query = string.IsNullOrWhiteSpace(SearchQuery) ? null : SearchQuery.Trim();
        var result = await _worker!.RequestAsync("export_job", new
        {
            source = CurrentSource, conversationId = SelectedConversation.Id,
            startDate = _applied.StartDate, endDate = _applied.EndDate, query,
            formats, target, sceneId = SelectedScene?.Id,
        }, TimeSpan.FromMinutes(10));
        var job = result.GetProperty("job").Deserialize<JobSummary>(Protocol.Json);
        if (job is null) throw new WorkerException("PROTOCOL_ERROR", "任务结果格式不正确。");
        ResultPath = job.Directory ?? "";
        ResultSummary = job.Summary;
        Status = $"任务 {job.JobId} 已创建：{job.StatusLabel}。";
        await LoadJobsCoreAsync(_worker);
        Log("job_created");
    }, "正在生成任务与导出文件…");

    private Task LoadJobsAsync() => RunAsync(async () =>
    {
        await LoadJobsCoreAsync(_worker);
        Status = Jobs.Count == 0 ? "暂无任务记录。" : $"已加载 {Jobs.Count} 个任务。";
    }, "正在读取任务历史…");

    private async Task LoadJobsCoreAsync(WorkerClient? worker)
    {
        if (worker?.IsAvailable != true) return;
        try
        {
            var list = (await worker.RequestAsync("list_jobs", new { limit = 80 }, TimeSpan.FromSeconds(60)))
                .Deserialize<JobList>(Protocol.Json);
            if (list is null) return;
            Jobs.Clear();
            foreach (var item in list.Items) Jobs.Add(item);
            OnPropertyChanged(nameof(Jobs)); OnPropertyChanged(nameof(HasJobs));
        }
        catch (WorkerException e) when (e.Code is "CAPABILITY_UNAVAILABLE" or "JOB_STORE_UNAVAILABLE")
        {
            Jobs.Clear();
        }
        catch (WorkerException) { }
    }

    public async Task<JobDetail?> LoadJobDetailAsync(JobSummary job)
    {
        JobDetail? detail = null;
        await RunAsync(async () =>
        {
            if (string.IsNullOrWhiteSpace(job.JobId))
                throw new WorkerException("INVALID_ARGUMENT", "任务标识无效。");
            var worker = _worker is { IsAvailable: true } current
                ? current : throw new WorkerException("WORKER_EXITED", "工作进程不可用，请重新检测。");
            var response = await worker.RequestAsync("get_job", new
            {
                jobId = job.JobId,
                includePrompt = false,
                includePreviews = false,
            }, TimeSpan.FromSeconds(60));
            var candidate = TaskHistoryResponseValidator.ParseJobDetail(job.JobId, response);
            detail = candidate;
            Status = "任务详情已加载。";
        }, "正在读取任务详情…");
        return detail;
    }

    public async Task<string?> ResolveJobArtifactPathAsync(JobSummary job, JobArtifact artifact)
    {
        string? resolvedPath = null;
        await RunAsync(async () =>
        {
            if (string.IsNullOrWhiteSpace(job.JobId) || string.IsNullOrWhiteSpace(artifact.Name))
                throw new WorkerException("INVALID_ARGUMENT", "任务文件信息无效。");
            var worker = _worker is { IsAvailable: true } current
                ? current : throw new WorkerException("WORKER_EXITED", "工作进程不可用，请重新检测。");
            var response = await worker.RequestAsync("get_job_artifact_path", new
            {
                jobId = job.JobId,
                name = artifact.Name,
            }, TimeSpan.FromSeconds(60));
            if (response.ValueKind != JsonValueKind.Object ||
                !response.TryGetProperty("path", out var pathElement) ||
                pathElement.ValueKind != JsonValueKind.String)
                throw new WorkerException("PROTOCOL_ERROR", "任务文件路径结果格式不正确。");
            string path = pathElement.GetString() ?? "";
            if (!Path.IsPathFullyQualified(path) || !File.Exists(path))
                throw new WorkerException("JOB_NOT_FOUND", "任务文件不存在或已被移动。");
            resolvedPath = path;
        }, "正在验证任务文件…");
        return resolvedPath;
    }

    public bool CanRetryJob(JobSummary job) =>
        _operationGate.CanRetry(IsBusy, _closing, _worker?.IsAvailable == true, job.CanRedeliver);

    public IDisposable? TryBeginPruneWorkflow()
    {
        if (!CanManageJobs) return null;
        var gateLease = _operationGate.TryEnter();
        if (gateLease is null) return null;

        _disconnectStatusGuard.Clear(_worker!);
        IsBusy = true;
        Error = "";
        Status = "正在准备任务历史清理…";
        ProgressLabel = "";
        _pruneWorkflowActive = true;
        return new CallbackLease(() =>
        {
            _pruneWorkflowActive = false;
            gateLease.Dispose();
            IsBusy = false;
            ProgressLabel = "";
        });
    }

    public async Task<PrunePreview?> PreviewPruneJobsAsync(int days)
    {
        if (!_pruneWorkflowActive || !IsBusy || _closing) return null;
        try
        {
            if (!PruneRetentionDayOptions.Contains(days))
                throw new WorkerException("INVALID_ARGUMENT", "请选择 30、90、180 或 365 天的保留期限。");
            var worker = _worker is { IsAvailable: true } current
                ? current : throw new WorkerException("WORKER_EXITED", "工作进程不可用，请重新检测。");
            var response = await worker.RequestAsync("preview_prune_jobs", new { days }, TimeSpan.FromMinutes(10));
            var candidate = TaskHistoryResponseValidator.ParsePrunePreview(days, response);
            Status = candidate.CandidateCount == 0
                ? "只读预览完成：没有符合条件的已完成任务。"
                : $"只读预览完成：发现 {candidate.CandidateCount:N0} 个旧的已完成任务；尚未删除。";
            return candidate;
        }
        catch (WorkerException error)
        {
            HandleWorkerFailure(error);
            return null;
        }
        catch (Exception error) when (IsHandledResultFailure(error))
        {
            HandleResultFailure(error);
            return null;
        }
    }

    public async Task<PruneResult?> ConfirmPruneJobsAsync(PrunePreview preview)
    {
        if (!_pruneWorkflowActive || !IsBusy || _closing) return null;
        WorkerClient? cleanupWorker = null;
        try
        {
            if (preview.CandidateCount <= 0 || string.IsNullOrWhiteSpace(preview.PreviewToken) ||
                !PruneRetentionDayOptions.Contains(preview.Days))
                throw new WorkerException("PRUNE_PREVIEW_REQUIRED", "没有有效的清理预览；请重新预览。");
            cleanupWorker = _worker is { IsAvailable: true } current
                ? current : throw new WorkerException("WORKER_EXITED", "工作进程不可用，请重新检测。");

            JsonElement response;
            IsPruneExecuting = true;
            try
            {
                // Never send the potentially multi-megabyte snapshot back to the
                // worker. The token is bound to the previewed days server-side.
                response = await cleanupWorker.RequestAsync("confirm_prune_jobs", new
                {
                    days = preview.Days,
                    previewToken = preview.PreviewToken,
                }, TimeSpan.FromMinutes(10));
            }
            catch (WorkerException error) when (IsPrunePreExecutionRejection(error.Code))
            {
                throw;
            }
            catch (WorkerException error)
            {
                throw UnknownPruneOutcome(error);
            }
            finally
            {
                IsPruneExecuting = false;
            }

            PruneResult parsedOutcome;
            try
            {
                parsedOutcome = TaskHistoryResponseValidator.ParsePruneResult(preview.Days, response);
            }
            catch (WorkerException error)
            {
                throw UnknownPruneOutcome(error);
            }

            await LoadJobsCoreAsync(cleanupWorker);
            Status = PruneResultPresentation.Create(parsedOutcome).Status;
            return parsedOutcome;
        }
        catch (WorkerException error)
        {
            if (error.Code == "PRUNE_RESULT_UNKNOWN")
            {
                if (cleanupWorker is not null) _disconnectStatusGuard.Preserve(cleanupWorker);
                Error = error.Message;
                Status = "清理结果未知，请刷新任务历史核实。";
                Log("request_failed", error.Code);
            }
            else
            {
                HandleWorkerFailure(error);
            }
            return null;
        }
        catch (Exception error) when (IsHandledResultFailure(error))
        {
            HandleResultFailure(error);
            return null;
        }
    }

    private static bool IsPrunePreExecutionRejection(string code) => code is
        "INVALID_ARGUMENT" or "UNKNOWN_COMMAND" or "PROTOCOL_MISMATCH" or
        "PRUNE_PREVIEW_REQUIRED" or "PRUNE_PREVIEW_UNKNOWN" or
        "PRUNE_PREVIEW_EXPIRED" or "PRUNE_PREVIEW_MISMATCH" or
        "JOB_STORE_UNAVAILABLE" or "JOB_PATH_UNSAFE";

    private static WorkerException UnknownPruneOutcome(WorkerException error) => new(
        "PRUNE_RESULT_UNKNOWN",
        "清理执行期间未收到完整结果，可能已有部分任务被永久删除。请刷新任务历史核实；不要根据此消息推断未删除。",
        error);

    public static string FormatPruneReasonCounts(IReadOnlyDictionary<string, int> counts) =>
        PrunePreview.FormatReasonCounts(counts);

    public Task DeliverJobAsync(JobSummary job) => RunAsync(async () =>
    {
        object target = TargetKind switch
        {
            "folder" when !string.IsNullOrWhiteSpace(TargetFolder) => new { kind = "folder", root = TargetFolder, organizeByDate = true },
            "obsidian" when !string.IsNullOrWhiteSpace(ObsidianVault) => new { kind = "obsidian", vault = ObsidianVault, folder = "微信记忆", attachments = "_附件", dedupe = true },
            _ => throw new WorkerException("INVALID_ARGUMENT", "请先选择文件夹或 Obsidian 目标。"),
        };
        var response = await _worker!.RequestAsync("deliver_job", new { jobId = job.JobId, target }, TimeSpan.FromMinutes(5));
        var delivery = DeliveryOutcome.Parse(response);
        await LoadJobsCoreAsync(_worker);
        // deliver_job returns ok:true even when the copy failed; the delivery
        // object is the only authoritative success signal.
        delivery.ThrowIfFailed();
        Status = "任务已重新投递。";
    }, "正在投递任务…");

    // ------------------------------------------------------------------ //
    // Voice
    // ------------------------------------------------------------------ //

    private Task RefreshVoiceStatusAsync() => RunAsync(async () =>
    {
        await RefreshVoiceStatusCoreAsync(_worker);
        Status = "已刷新语音状态。";
    }, "正在检查语音组件…");

    private async Task RefreshVoiceStatusCoreAsync(WorkerClient? worker)
    {
        if (worker?.IsAvailable != true) return;
        try
        {
            var status = (await worker.RequestAsync("voice_status", new { }, TimeSpan.FromSeconds(60)))
                .Deserialize<VoiceStatus>(Protocol.Json);
            if (status is not null) VoiceStatusText = status.Summary;
        }
        catch (WorkerException e) when (e.Code is "CAPABILITY_UNAVAILABLE") { VoiceStatusText = "语音组件不可用"; }
        catch (WorkerException) { VoiceStatusText = "语音状态不可用"; }
    }

    public async Task PrepareVoiceModelAsync()
    {
        if (_worker?.IsAvailable != true) { Error = "工作进程未连接。"; return; }
        await RunAsync(async () =>
        {
            await _worker.RequestAsync("prepare_voice_model", new { consent = true }, TimeSpan.FromMinutes(30));
            await RefreshVoiceStatusCoreAsync(_worker);
            Status = "语音模型已准备好，可以转写当前会话的语音。";
            Log("voice_model_ready");
        }, "正在下载或加载语音模型（首次可能需要较长时间）…");
    }

    private Task TranscribeAsync() => RunAsync(async () =>
    {
        if (IsGlobalSearchMode || IsContextMode)
            throw new WorkerException("INVALID_ARGUMENT", "全局搜索和上下文模式不能执行单会话语音转写。");
        if (SelectedConversation is null) throw new WorkerException("INVALID_ARGUMENT", "请先选择会话。");
        var query = string.IsNullOrWhiteSpace(SearchQuery) ? null : SearchQuery.Trim();
        var result = (await _worker!.RequestAsync("transcribe_messages", new
        {
            source = CurrentSource, conversationId = SelectedConversation.Id,
            startDate = _applied.StartDate, endDate = _applied.EndDate, query,
        }, TimeSpan.FromMinutes(30))).Deserialize<TranscribeResult>(Protocol.Json);
        await FetchAsync(false);
        Status = result is null ? "语音转写完成。" : $"语音转写完成：{result.Transcribed} / {result.Total} 条消息有转写。";
        Log("voice_transcribed");
    }, "正在本地转写语音…");

    // ------------------------------------------------------------------ //
    // Error / lifecycle
    // ------------------------------------------------------------------ //

    public void ShowError(string message, string code)
    {
        Error = message; Status = "操作未完成。"; Log("ui_operation_failed", code);
    }

    private async Task RunAsync(Func<Task> operation, string status)
    {
        var gateLease = _operationGate.TryEnter();
        if (gateLease is null || IsBusy || _closing)
        {
            gateLease?.Dispose();
            return;
        }
        if (_worker is { } current) _disconnectStatusGuard.Clear(current);
        IsBusy = true; Error = ""; Status = status; ProgressLabel = "";
        try { await operation(); }
        catch (WorkerException error) { HandleWorkerFailure(error); }
        catch (Exception error) when (IsHandledResultFailure(error)) { HandleResultFailure(error); }
        finally
        {
            gateLease.Dispose();
            IsBusy = false;
            ProgressLabel = "";
        }
    }

    private void Log(string name, string code = "OK") => _log(name, code);

    private void HandleWorkerFailure(WorkerException error)
    {
        Error = error.Message; Status = "任务未完成。"; Log("request_failed", error.Code);
        if (error.Code == "SOURCE_SESSION_CHANGED")
        {
            // The worker no longer owns this source instance. Do not leave an
            // ordinary export scope enabled behind a disconnected search screen.
            IsConnected = false;
            InvalidateGlobalSource();
            ConnectionLabel = "数据源已失效 · 请重新连接或导入";
        }
        else if (IsGlobalSearchMode && error.Code is "SEARCH_EXPIRED" or "SEARCH_SOURCE_CHANGED" or
            "CONTEXT_TARGET_CHANGED" or "CONTEXT_TARGET_UNKNOWN" or "PAGE_EXPIRED" or "PAGE_SOURCE_CHANGED")
        {
            _globalSearchState.InvalidateResults(error.Message);
            UpdateFlags();
        }
        else if (!IsGlobalSearchMode && _messagePageWindow is not null && IsMessagePagingFailure(error))
        {
            // Keep the last successfully committed window visible. A failed
            // keyword scan, expired snapshot, changed source, budget rejection,
            // or malformed response must never turn a partial response into a
            // replacement page or silently fall back during navigation.
            _messagePagingInvalidation = error.Code is "PAGE_EXPIRED" or "PAGE_SOURCE_CHANGED" or "PAGE_CRITERIA_CHANGED"
                ? _messagePageWindow.First.IsKeyword
                    ? "消息关键词快照已失效，请点击“刷新首屏”；不会把新数据接到旧游标后。"
                    : "消息预览已失效，请点击“刷新首屏”；不会把新数据接到旧游标后。"
                : "消息页未能完成读取；最后一页仍保留，请点击“刷新首屏”重试。";
            if (IsCursorCompatibilityFailure(error) && _messageConversationId is not null)
            {
                if (_messagePageWindow.First.IsKeyword)
                    _legacyKeywordPagingConversations[_messageConversationId] = "此会话结构不再支持关键词快照分页，已降级";
                else
                    _legacyPagingConversations[_messageConversationId] = "此会话结构不再支持稳定游标分页，已降级";
            }
            UpdateFlags();
        }
        if (error.Code == "WECHAT_LOGIN_TIMEOUT")
            AccountHint = "微信登录超时。请手动打开并登录微信，然后点击“刷新”再试。";
        else if (error.Code == "WECHAT_KEY_UNAVAILABLE")
            AccountHint = "无法读取微信进程。请确认微信能正常启动，必要时重启电脑后再试。";
        else if (error.Code is "WECHAT_NOT_FOUND" or "WECHAT_EXE_NOT_FOUND")
            AccountHint = "没有找到微信 4.x。请先安装并登录微信，然后点击“刷新”。";
        if (_worker?.IsAvailable != true) { IsConnected = false; ConnectionLabel = "工作进程不可用"; }
    }

    private void HandleResultFailure(Exception error)
    {
        Error = "处理结果失败。请重新连接或检查输出文件夹权限。"; Status = "任务未完成。";
        Log("result_failed", error.GetType().Name);
    }

    private static bool IsHandledResultFailure(Exception error) =>
        error is IOException or UnauthorizedAccessException or JsonException or KeyNotFoundException or InvalidOperationException or FormatException;

    private sealed class CallbackLease(Action release) : IDisposable
    {
        private Action? _release = release;
        public void Dispose() => Interlocked.Exchange(ref _release, null)?.Invoke();
    }

    public async ValueTask DisposeAsync()
    {
        _closing = true; IsConnected = false;
        InvalidateGlobalSource();
        if (_worker is not null) await _worker.DisposeAsync();
    }
}
