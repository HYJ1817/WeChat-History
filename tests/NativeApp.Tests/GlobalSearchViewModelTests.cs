using System.Diagnostics;
using System.Text.Json;
using WechatMemory.NativeApp.Models;
using WechatMemory.NativeApp.Services;
using WechatMemory.NativeApp.ViewModels;
using Xunit;

namespace NativeApp.Tests;

public sealed class GlobalSearchViewModelTests
{
    [Theory]
    [InlineData("demo")]
    [InlineData("offline")]
    [InlineData("wechat4")]
    public async Task ProductionCommandsSearchPageLocateReturnAndRestoreOrdinaryExportScope(string source)
    {
        await using var app = new TestApp(demo: source != "wechat4");
        await app.InitializeAsync();
        var vm = app.Vm;
        if (source == "offline") await vm.ImportJsonAsync(app.PathFor("synthetic.json"));
        if (source == "wechat4") await vm.ConnectWechatCommand.ExecuteAsync(null);
        Assert.True(vm.CanUseGlobalSearch, vm.Error);
        Assert.Equal("conv-2", vm.SelectedConversation?.Id);

        vm.StartDate = new DateTimeOffset(2026, 10, 1, 0, 0, 0, DateRange.Offset);
        vm.EndDate = new DateTimeOffset(2026, 10, 31, 0, 0, 0, DateRange.Offset);
        vm.SearchQuery = "ordinary-filter";
        await vm.ApplyFiltersCommand.ExecuteAsync(null);
        var ordinary = vm.Messages.ToArray();
        vm.SwitchToGlobalSearchCommand.Execute(null);
        vm.ConversationQuery = "仅原会话";
        await vm.SearchConversationsCommand.ExecuteAsync(null);
        Assert.Single(vm.Conversations);
        Assert.Equal(ordinary, vm.Messages);

        vm.GlobalSearchQuery = "  needle  ";
        vm.GlobalStartDate = new DateTimeOffset(2026, 11, 1, 0, 0, 0, DateRange.Offset);
        vm.GlobalEndDate = new DateTimeOffset(2026, 11, 28, 0, 0, 0, DateRange.Offset);
        vm.GlobalSearchOrder = "asc";
        Assert.Empty(app.Requests("search_messages")); // Editing never scans.
        await vm.ApplyFiltersCommand.ExecuteAsync(null);
        Assert.Equal("hit-1", Assert.Single(vm.GlobalSearchHits).HitId);
        Assert.False(vm.GlobalSearchDirty);
        Assert.Contains("needle", vm.GlobalAppliedFiltersLabel);
        Assert.Contains("3", vm.GlobalCoverageNotice);
        Assert.True(vm.CanLoadMoreGlobal);
        Assert.False(vm.CanExport);
        Assert.False(vm.CanExportJob);
        Assert.False(vm.CanTranscribe);
        Assert.False(vm.CanLoadMore);
        var first = Assert.Single(app.Requests("search_messages"));
        Assert.Equal(source, first.GetProperty("source").GetString());
        Assert.Equal(vm.SourceSessionId, first.GetProperty("sourceSessionId").GetString());
        Assert.Equal("needle", first.GetProperty("query").GetString());
        Assert.Equal("2026-11-01", first.GetProperty("startDate").GetString());
        Assert.Equal("2026-11-28", first.GetProperty("endDate").GetString());
        Assert.Equal("asc", first.GetProperty("order").GetString());
        Assert.Equal(50, first.GetProperty("limit").GetInt32());
        Assert.False(first.TryGetProperty("conversationId", out _));

        vm.GlobalSearchOrder = "desc";
        Assert.True(vm.GlobalSearchDirty);
        Assert.False(vm.CanNavigateGlobalHits);
        Assert.False(vm.CanLoadMoreGlobal);
        await vm.LoadMoreGlobalCommand.ExecuteAsync(null);
        Assert.False(await vm.OpenGlobalHitAsync(vm.GlobalSearchHits[0]));
        Assert.Single(app.Requests("search_messages"));
        vm.GlobalSearchOrder = "asc";
        Assert.False(vm.GlobalSearchDirty);
        await vm.LoadMoreGlobalCommand.ExecuteAsync(null);
        Assert.Equal(new[] { "hit-1", "hit-2" }, vm.GlobalSearchHits.Select(hit => hit.HitId));
        Assert.False(vm.CanLoadMoreGlobal);
        var next = app.Requests("search_messages")[1];
        Assert.Equal(new[] { "limit", "offset", "searchId", "source", "sourceSessionId" },
            next.EnumerateObject().Select(p => p.Name).OrderBy(name => name, StringComparer.Ordinal));

        await vm.ExportAsync(app.PathFor("blocked.md"));
        await vm.ExportJobCommand.ExecuteAsync(null);
        await vm.LoadMoreCommand.ExecuteAsync(null);
        await vm.TranscribeCommand.ExecuteAsync(null);
        Assert.Empty(app.Requests("export_markdown"));
        Assert.Empty(app.Requests("export_job"));
        Assert.Empty(app.Requests("transcribe_messages"));
        int ordinaryRequests = app.Requests("get_messages").Count;
        var hits = vm.GlobalSearchHits.ToArray();
        Assert.True(await vm.OpenGlobalHitAsync(hits[1], 328.5), vm.Error);
        Assert.Equal(ordinaryRequests, app.Requests("get_messages").Count);
        Assert.Equal("", vm.ConversationQuery);
        Assert.Contains("已清除", vm.Status);
        Assert.Equal("conv-1", vm.SelectedConversation?.Id);
        Assert.True(vm.IsContextMode);
        Assert.Equal(1, vm.ContextFocusIndex);
        Assert.Equal("msg-2", vm.ContextMessages[vm.ContextFocusIndex].Id);
        Assert.Single(vm.ContextMessages, message => message.IsContextFocus);
        Assert.All(vm.ContextMessages, message => Assert.Null(message.AttachmentPath));
        Assert.False(vm.CanLoadMore);
        Assert.False(vm.CanExport);
        Assert.False(vm.CanExportJob);
        await vm.ExportAsync(app.PathFor("blocked-context.md"));
        await vm.ExportJobCommand.ExecuteAsync(null);
        await vm.LoadMoreCommand.ExecuteAsync(null);
        Assert.Empty(app.Requests("export_markdown"));
        Assert.Empty(app.Requests("export_job"));
        Assert.Equal(ordinaryRequests, app.Requests("get_messages").Count);

        await vm.ReturnToSearchCommand.ExecuteAsync(null);
        Assert.False(vm.IsContextMode);
        Assert.True(vm.IsGlobalSearchMode);
        Assert.Equal(hits, vm.GlobalSearchHits);
        Assert.Equal(328.5, vm.GlobalReturnScrollOffset);
        Assert.Equal("conv-2", vm.SelectedConversation?.Id);
        Assert.Equal(ordinary, vm.Messages);
        Assert.Equal(ordinaryRequests, app.Requests("get_messages").Count);

        Assert.True(await vm.OpenGlobalHitAsync(hits[0]));
        await vm.BrowseContextConversationCommand.ExecuteAsync(null);
        Assert.False(vm.IsGlobalSearchMode);
        Assert.False(vm.IsContextMode);
        Assert.Empty(vm.ContextMessages);
        Assert.Equal("conv-1", vm.SelectedConversation?.Id);
        Assert.Equal(50, vm.Messages.Count);
        Assert.True(vm.CanExport, vm.Error);
        Assert.True(vm.CanExportJob);
        var browse = app.Requests("get_messages").Last();
        Assert.Equal("ordinary-filter", browse.GetProperty("query").GetString());
        Assert.Equal("2026-10-01", browse.GetProperty("startDate").GetString());
        Assert.Equal("2026-10-31", browse.GetProperty("endDate").GetString());
        await vm.ExportAsync(app.PathFor("ordinary.md"));
        Assert.Contains("60", vm.ResultSummary); // Export is not the 50 loaded messages.
        var export = Assert.Single(app.Requests("export_markdown"));
        Assert.Equal("conv-1", export.GetProperty("conversationId").GetString());
        Assert.Equal("ordinary-filter", export.GetProperty("query").GetString());
        Assert.False(export.TryGetProperty("limit", out _));
        Assert.False(export.TryGetProperty("searchId", out _));
        Assert.False(export.TryGetProperty("offset", out _));
    }

    [Theory]
    [InlineData("legacy")]
    [InlineData("search-only")]
    [InlineData("no-token")]
    public async Task UnsupportedWorkersKeepOrdinaryBrowsingAndDisableGlobalEntry(string scenario)
    {
        await using var app = new TestApp(scenario);
        await app.InitializeAsync();
        Assert.True(app.Vm.CanExport, app.Vm.Error);
        Assert.False(app.Vm.CanUseGlobalSearch);
        Assert.False(app.Vm.SwitchToGlobalSearchCommand.CanExecute(null));
        app.Vm.SwitchToGlobalSearchCommand.Execute(null);
        Assert.False(app.Vm.IsGlobalSearchMode);
        Assert.Empty(app.Requests("search_messages"));
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public async Task FailedImportPreservesSearchAndContextButSuccessfulImportInvalidatesThem(bool context)
    {
        await using var app = new TestApp();
        await app.InitializeAsync();
        await app.SearchAsync();
        var vm = app.Vm;
        if (context) Assert.True(await vm.OpenGlobalHitAsync(vm.GlobalSearchHits[0], 100));
        var session = vm.SourceSessionId;
        var hits = vm.GlobalSearchHits.ToArray();
        var messages = vm.ContextMessages.ToArray();
        var selection = vm.SelectedConversation;
        await vm.ImportJsonAsync(app.PathFor("reject.json"));
        Assert.Contains("IMPORT_INVALID_JSON", vm.Error);
        Assert.Equal(session, vm.SourceSessionId);
        Assert.Equal(hits, vm.GlobalSearchHits);
        Assert.Equal(messages, vm.ContextMessages);
        Assert.Equal(selection, vm.SelectedConversation);
        Assert.True(vm.IsGlobalSearchMode);
        Assert.Equal(context, vm.IsContextMode);
        Assert.True(vm.IsConnected);
        Assert.False(vm.GlobalSearchDirty);

        await vm.ImportJsonAsync(app.PathFor("replacement.json"));
        Assert.NotEqual(session, vm.SourceSessionId);
        Assert.Empty(vm.GlobalSearchHits);
        Assert.Empty(vm.ContextMessages);
        Assert.Null(vm.ContextConversation);
        Assert.False(vm.IsGlobalSearchMode);
        Assert.False(vm.IsContextMode);
        Assert.Equal("", vm.GlobalAppliedFiltersLabel);
        Assert.True(vm.CanExport, vm.Error);
    }

    [Fact]
    public async Task EditingDuringAnInFlightSearchDoesNotApplyLateResultsOrBypassTheGate()
    {
        await using var app = new TestApp("pause-search");
        await app.InitializeAsync();
        await app.SearchAsync();
        var vm = app.Vm;
        var hits = vm.GlobalSearchHits.ToArray();
        vm.GlobalSearchQuery = "paused";
        Task request = vm.ApplyFiltersCommand.ExecuteAsync(null);
        await app.Paused.Task.WaitAsync(TimeSpan.FromSeconds(5));
        Assert.True(vm.IsBusy);
        Assert.False(vm.CanConnect);
        Assert.False(vm.CanImportJson);
        Assert.False(vm.CanEditGlobalFilters);
        Assert.False(vm.CanLoadMoreGlobal);
        Assert.False(vm.CanNavigateGlobalHits);
        await vm.ClearFiltersCommand.ExecuteAsync(null);
        await vm.SwitchToConversationCommand.ExecuteAsync(null);
        await vm.ImportJsonAsync(app.PathFor("must-not-import.json"));
        Assert.True(vm.IsGlobalSearchMode);
        Assert.Equal("paused", vm.GlobalSearchQuery);
        Assert.Empty(app.Requests("import_json"));
        vm.GlobalSearchQuery = "newer-criteria";
        app.Release();
        await request;
        Assert.Contains("迟到", vm.Error);
        Assert.Equal(hits, vm.GlobalSearchHits);
        Assert.True(vm.GlobalSearchDirty);
        Assert.Contains("needle", vm.GlobalAppliedFiltersLabel);
        vm.GlobalSearchQuery = "needle";
        Assert.True(vm.CanNavigateGlobalHits);
    }

    [Fact]
    public async Task SourceInvalidationRejectsAnInFlightResponse()
    {
        await using var app = new TestApp("pause-search");
        await app.InitializeAsync();
        await app.SearchAsync();
        app.Vm.GlobalSearchQuery = "paused";
        Task request = app.Vm.ApplyFiltersCommand.ExecuteAsync(null);
        await app.Paused.Task.WaitAsync(TimeSpan.FromSeconds(5));
        app.Vm.IsConnected = false;
        Assert.Empty(app.Vm.GlobalSearchHits);
        Assert.Null(app.Vm.SourceSessionId);
        app.Release();
        await request;
        Assert.Empty(app.Vm.GlobalSearchHits);
        Assert.False(app.Vm.CanNavigateGlobalHits);
        Assert.False(app.Vm.CanExport);
        Assert.False(app.Vm.IsConnected);
    }

    [Theory]
    [InlineData("expired-page")]
    [InlineData("changed-page")]
    public async Task ExpiredOrChangedSearchDisablesOldPagesAndLocatorsUntilACompleteNewSearch(string scenario)
    {
        await using var app = new TestApp(scenario);
        await app.InitializeAsync();
        await app.SearchAsync();
        var hits = app.Vm.GlobalSearchHits.ToArray();
        await app.Vm.LoadMoreGlobalCommand.ExecuteAsync(null);
        Assert.Equal(hits, app.Vm.GlobalSearchHits);
        Assert.False(app.Vm.CanLoadMoreGlobal);
        Assert.False(app.Vm.CanNavigateGlobalHits);
        Assert.NotEmpty(app.Vm.GlobalSearchStatus);
        app.Vm.GlobalSearchQuery = "changed";
        app.Vm.GlobalSearchQuery = "needle";
        Assert.False(app.Vm.CanNavigateGlobalHits);
        await app.Vm.ApplyFiltersCommand.ExecuteAsync(null);
        Assert.True(app.Vm.CanNavigateGlobalHits, app.Vm.Error);
        Assert.True(app.Vm.CanLoadMoreGlobal);
        Assert.Equal("", app.Vm.GlobalSearchStatus);
    }

    [Theory]
    [InlineData("bad-focus", true)]
    [InlineData("hidden-list-fail", true)]
    [InlineData("expired-context", false)]
    public async Task FailedContextNavigationCommitsNoModeSelectionFilterOrMessageChanges(string scenario, bool canRetry)
    {
        await using var app = new TestApp(scenario);
        await app.InitializeAsync();
        await app.SearchAsync();
        var vm = app.Vm;
        vm.ConversationQuery = "仅原会话";
        await vm.SearchConversationsCommand.ExecuteAsync(null);
        var selected = vm.SelectedConversation;
        var ordinary = vm.Messages.ToArray();
        var visible = vm.Conversations.ToArray();
        int before = app.Requests("get_messages").Count;
        Assert.False(await vm.OpenGlobalHitAsync(vm.GlobalSearchHits[0]));
        Assert.False(vm.IsContextMode);
        Assert.True(vm.IsGlobalSearchMode);
        Assert.Equal(selected, vm.SelectedConversation);
        Assert.Equal(ordinary, vm.Messages);
        Assert.Equal(visible, vm.Conversations);
        Assert.Equal("仅原会话", vm.ConversationQuery);
        Assert.Empty(vm.ContextMessages);
        Assert.Equal(before, app.Requests("get_messages").Count);
        Assert.Equal(canRetry, vm.CanNavigateGlobalHits);
        Assert.False(vm.CanExport);
    }

    [Fact]
    public async Task InvalidOrdinaryPageDoesNotReplaceTheVerifiedContext()
    {
        await using var app = new TestApp("bad-browse");
        await app.InitializeAsync();
        await app.SearchAsync();
        Assert.True(await app.Vm.OpenGlobalHitAsync(app.Vm.GlobalSearchHits[0]));
        var context = app.Vm.ContextMessages.ToArray();
        await app.Vm.BrowseContextConversationCommand.ExecuteAsync(null);
        Assert.True(app.Vm.IsContextMode);
        Assert.Equal(context, app.Vm.ContextMessages);
        Assert.False(app.Vm.CanExport);
        Assert.NotEmpty(app.Vm.Error);
    }

    [Fact]
    public async Task FailedOrMismatchedNewSearchPreservesTheLastAppliedSnapshotAndZeroResultsKeepCoverage()
    {
        await using var app = new TestApp();
        await app.InitializeAsync();
        await app.SearchAsync();
        var hits = app.Vm.GlobalSearchHits.ToArray();
        foreach (string query in new[] { "soft-error", "bad-first" })
        {
            app.Vm.GlobalSearchQuery = query;
            await app.Vm.ApplyFiltersCommand.ExecuteAsync(null);
            Assert.Equal(hits, app.Vm.GlobalSearchHits);
            Assert.Contains("needle", app.Vm.GlobalAppliedFiltersLabel);
            Assert.True(app.Vm.GlobalSearchDirty);
            Assert.False(app.Vm.CanNavigateGlobalHits);
        }
        app.Vm.GlobalSearchQuery = "none";
        await app.Vm.ApplyFiltersCommand.ExecuteAsync(null);
        Assert.Empty(app.Vm.GlobalSearchHits);
        Assert.Contains("部分复杂消息", app.Vm.GlobalCoverageNotice);
        Assert.Contains("0", app.Vm.GlobalSearchSummary);
        Assert.False(app.Vm.CanLoadMoreGlobal);
        app.Vm.GlobalSearchQuery = " \t ";
        Assert.False(app.Vm.CanApplyFilters);
        int requests = app.Requests("search_messages").Count;
        await app.Vm.ApplyFiltersCommand.ExecuteAsync(null);
        Assert.Equal(requests, app.Requests("search_messages").Count);
    }

    [Theory]
    [InlineData("exit-search")]
    [InlineData("source-session-changed")]
    public async Task WorkerExitOrDestroyedSourceDropsEverySearchLocatorAndExportScope(string scenario)
    {
        await using var app = new TestApp(scenario);
        await app.InitializeAsync();
        await app.SearchAsync();
        if (scenario == "source-session-changed") await app.Vm.LoadMoreGlobalCommand.ExecuteAsync(null);
        Assert.False(app.Vm.IsConnected);
        Assert.Null(app.Vm.SourceSessionId);
        Assert.Empty(app.Vm.GlobalSearchHits);
        Assert.Empty(app.Vm.ContextMessages);
        Assert.False(app.Vm.CanExport);
        Assert.False(app.Vm.CanExportJob);
        Assert.False(app.Vm.CanNavigateGlobalHits);
    }

    [Fact]
    public async Task ReconnectFailureNeverResurrectsTheDestroyedSourceOrItsLocators()
    {
        await using var app = new TestApp("reconnect-fail", demo: false);
        await app.InitializeAsync();
        await app.Vm.ConnectWechatCommand.ExecuteAsync(null);
        await app.SearchAsync();
        Assert.True(app.Vm.CanNavigateGlobalHits, app.Vm.Error);
        // This is the real reconnect-required option path, not a test-only source setter.
        app.Vm.EnableVoice = true;
        Assert.False(app.Vm.IsConnected);
        Assert.Null(app.Vm.SourceSessionId);
        Assert.True(app.Vm.CanConnectWechat);
        await app.Vm.ConnectWechatCommand.ExecuteAsync(null);
        Assert.Contains("无法读取", app.Vm.AccountHint);
        Assert.False(app.Vm.IsConnected);
        Assert.Null(app.Vm.SourceSessionId);
        Assert.Empty(app.Vm.GlobalSearchHits);
        Assert.False(app.Vm.CanExport);
        Assert.False(app.Vm.CanUseGlobalSearch);
    }

    [Fact]
    public async Task GlobalSearchDoesNotRequireSelectionAndInvalidDatesNeverScan()
    {
        await using var app = new TestApp();
        await app.InitializeAsync();
        app.Vm.SelectedConversation = null;
        app.Vm.SwitchToGlobalSearchCommand.Execute(null);
        app.Vm.GlobalSearchQuery = "needle";
        app.Vm.GlobalStartDate = new DateTimeOffset(2026, 11, 28, 0, 0, 0, DateRange.Offset);
        app.Vm.GlobalEndDate = new DateTimeOffset(2026, 11, 1, 0, 0, 0, DateRange.Offset);
        await app.Vm.ApplyFiltersCommand.ExecuteAsync(null);
        Assert.Contains("开始日期", app.Vm.Error);
        Assert.Empty(app.Requests("search_messages"));
        app.Vm.GlobalEndDate = null;
        await app.Vm.ApplyFiltersCommand.ExecuteAsync(null);
        Assert.NotEmpty(app.Vm.GlobalSearchHits);
        Assert.Null(app.Vm.SelectedConversation);
        Assert.True(await app.Vm.OpenGlobalHitAsync(app.Vm.GlobalSearchHits[0]));
        await app.Vm.ReturnToSearchCommand.ExecuteAsync(null);
        Assert.Null(app.Vm.SelectedConversation);
        await app.Vm.ClearFiltersCommand.ExecuteAsync(null);
        Assert.Empty(app.Vm.GlobalSearchHits);
        Assert.Equal("", app.Vm.GlobalSearchQuery);
        Assert.False(app.Vm.CanLoadMoreGlobal);
        Assert.False(app.Vm.CanNavigateGlobalHits);
        await app.Vm.SwitchToConversationCommand.ExecuteAsync(null);
        Assert.False(app.Vm.IsGlobalSearchMode);
        Assert.Empty(app.Vm.Messages);
        Assert.False(app.Vm.CanExport);
    }

    private sealed class TestApp : IAsyncDisposable
    {
        private readonly string _root = Path.Combine(Path.GetTempPath(), "wechat-native-search-" + Guid.NewGuid().ToString("N"));
        public MainViewModel Vm { get; }
        public TaskCompletionSource Paused { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);

        public TestApp(string scenario = "normal", bool demo = true)
        {
            Directory.CreateDirectory(_root);
            Vm = new MainViewModel(action => action(), () =>
            {
                string? project = AppContext.BaseDirectory;
                while (project is not null && !File.Exists(Path.Combine(project, "global.json")))
                    project = Directory.GetParent(project)?.FullName;
                Assert.NotNull(project);
                var info = new ProcessStartInfo(Path.Combine(project!, ".venv", "Scripts", "python.exe")) { WorkingDirectory = _root };
                foreach (string arg in new[] { "-I", "-X", "utf8", "-u", Path.Combine(AppContext.BaseDirectory,
                    "fixtures", "global_search_vm_worker.py"), scenario, _root }) info.ArgumentList.Add(arg);
                var worker = WorkerClient.Start(info);
                worker.ProgressReceived += progress => { if (progress.Label == "fixture_paused") Paused.TrySetResult(); };
                return worker;
            }, demo, (_, _) => { });
            // Model the synchronous TwoWay SelectedItem reset WinUI performs
            // when its ItemsSource collection is cleared.
            Vm.Conversations.CollectionChanged += (_, change) =>
            {
                if (change.Action == System.Collections.Specialized.NotifyCollectionChangedAction.Reset)
                    Vm.SelectedConversation = null;
            };
        }

        public async Task InitializeAsync()
        {
            await Vm.InitializeAsync();
            Assert.Equal("", Vm.Error);
        }

        public async Task SearchAsync()
        {
            Vm.SwitchToGlobalSearchCommand.Execute(null);
            Vm.GlobalSearchQuery = "needle";
            await Vm.ApplyFiltersCommand.ExecuteAsync(null);
        }

        public string PathFor(string name) => Path.Combine(_root, name);
        public void Release() => File.WriteAllText(PathFor("release"), "");
        public List<JsonElement> Requests(string command)
        {
            // voice_status intentionally runs independently after connection.
            using var file = new FileStream(PathFor("requests.jsonl"), FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
            using var reader = new StreamReader(file);
            var requests = new List<JsonElement>();
            while (reader.ReadLine() is { } line)
            {
                using var document = JsonDocument.Parse(line);
                if (document.RootElement.GetProperty("command").GetString() == command)
                    requests.Add(document.RootElement.GetProperty("payload").Clone());
            }
            return requests;
        }

        public async ValueTask DisposeAsync()
        {
            Release();
            await Vm.DisposeAsync();
            Directory.Delete(_root, recursive: true);
        }
    }
}
