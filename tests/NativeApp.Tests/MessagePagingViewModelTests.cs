using System.Diagnostics;
using System.Text.Json;
using WechatMemory.NativeApp.Models;
using WechatMemory.NativeApp.Services;
using WechatMemory.NativeApp.ViewModels;
using Xunit;

namespace NativeApp.Tests;

public sealed class MessagePagingViewModelTests
{
    [Theory]
    [InlineData("demo")]
    [InlineData("offline")]
    [InlineData("wechat4")]
    public async Task ProductionCommandsUseCursorPagesAndABoundedBidirectionalWindow(string source)
    {
        await using var app = new TestApp(demo: source != "wechat4");
        await app.InitializeAsync();
        var vm = app.Vm;
        if (source == "offline") await vm.ImportJsonAsync(app.PathFor("synthetic.json"));
        if (source == "wechat4") await vm.ConnectWechatCommand.ExecuteAsync(null);
        Assert.True(vm.HasMessageCursorPaginationCapability);
        Assert.True(vm.CanExport, vm.Error);
        Assert.True(vm.ShowCursorMessageNavigation);
        Assert.False(vm.ShowLegacyMessageNavigation);
        Assert.False(vm.CanLoadMore);
        Assert.False(vm.CanLoadPreviousMessageBatch);
        Assert.True(vm.CanLoadNextMessageBatch);
        Assert.Equal(50, vm.Messages.Count);
        Assert.Empty(app.Requests("get_messages"));
        var first = app.Requests("get_messages_page").Last();
        Assert.Equal(source, first.GetProperty("source").GetString());
        Assert.Equal(vm.SourceSessionId, first.GetProperty("sourceSessionId").GetString());
        Assert.Equal(50, first.GetProperty("limit").GetInt32());

        for (int i = 0; i < 20; ++i) await vm.NextMessageBatchCommand.ExecuteAsync(null);
        Assert.Equal(1000, vm.Messages.Count);
        Assert.Equal(20, vm.VisibleMessagePageCount);
        Assert.InRange(vm.VisibleMessageDisplayBytes, 1, MessagePagingWorkflow.MaxDisplayBytes);
        Assert.Equal("m-50", vm.Messages.First().Id);
        Assert.Equal("m-1049", vm.Messages.Last().Id);
        Assert.Contains("51–1,050 / 2,351", vm.MessageSummary);
        Assert.True(vm.CanLoadPreviousMessageBatch);
        await vm.PreviousMessageBatchCommand.ExecuteAsync(null);
        Assert.Equal("m-0", vm.Messages.First().Id);
        Assert.Equal("m-999", vm.Messages.Last().Id);
        Assert.False(vm.CanLoadPreviousMessageBatch);
        var previous = app.Requests("get_messages_page").Last();
        Assert.StartsWith("p:", previous.GetProperty("cursor").GetString());
        Assert.Equal(new[] { "cursor", "limit", "pageSessionId", "source", "sourceSessionId" },
            previous.EnumerateObject().Select(p => p.Name).Order(StringComparer.Ordinal));
        int count = app.Requests("get_messages_page").Count;
        await vm.LoadMoreCommand.ExecuteAsync(null);
        Assert.Equal(count, app.Requests("get_messages_page").Count);
        await vm.RefreshMessagesCommand.ExecuteAsync(null);
        Assert.Equal(50, vm.Messages.Count);
        Assert.Equal(1, vm.VisibleMessagePageCount);
        Assert.False(vm.CanLoadPreviousMessageBatch);
        Assert.DoesNotContain("cursor", app.Requests("get_messages_page").Last().EnumerateObject().Select(p => p.Name));
    }

    [Theory]
    [InlineData("expired-next")]
    [InlineData("changed-next")]
    [InlineData("schema-next")]
    public async Task InvalidatedSessionsKeepTheLastWindowDisableBothDirectionsAndRequireExplicitRefresh(string scenario)
    {
        await using var app = new TestApp(scenario);
        await app.InitializeAsync();
        var vm = app.Vm;
        var original = vm.Messages.ToArray();
        await vm.NextMessageBatchCommand.ExecuteAsync(null);
        Assert.Equal(original, vm.Messages);
        Assert.False(vm.CanLoadNextMessageBatch);
        Assert.False(vm.CanLoadPreviousMessageBatch);
        Assert.Contains("刷新首屏", vm.MessagePagingStatus);
        Assert.True(vm.CanRefreshMessages);
        Assert.Empty(app.Requests("get_messages")); // No silent concatenation or offset fallback.
        await vm.NextMessageBatchCommand.ExecuteAsync(null);
        Assert.Equal(2, app.Requests("get_messages_page").Count);
        await vm.RefreshMessagesCommand.ExecuteAsync(null);
        Assert.Equal("", vm.Error);
        Assert.DoesNotContain("预览已失效", vm.MessagePagingStatus);
        if (scenario == "schema-next")
        {
            Assert.False(vm.ShowCursorMessageNavigation);
            Assert.True(vm.CanLoadMore);
            Assert.Single(app.Requests("get_messages"));
        }
        else
        {
            Assert.True(vm.CanLoadNextMessageBatch);
            Assert.NotEqual(app.Requests("get_messages_page")[1].GetProperty("pageSessionId").GetString(),
                app.Requests("get_messages_page").Last().TryGetProperty("pageSessionId", out var id) ? id.GetString() : null);
        }
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public async Task LivePageBudgetFailurePreservesTheWindowDisablesBothDirectionsAndRequiresRefresh(bool previous)
    {
        await using var app = new TestApp("budget-controlled", demo: false);
        await app.InitializeAsync();
        var vm = app.Vm;
        await vm.ConnectWechatCommand.ExecuteAsync(null);
        for (int i = 0; i < 20; ++i) await vm.NextMessageBatchCommand.ExecuteAsync(null);
        Assert.True(vm.CanLoadNextMessageBatch);
        Assert.True(vm.CanLoadPreviousMessageBatch);
        var original = vm.Messages.ToArray();
        long version = vm.MessageWindowVersion;
        int requests = app.Requests("get_messages_page").Count;
        File.WriteAllText(app.PathFor("reject-page"), "");

        await (previous ? vm.PreviousMessageBatchCommand : vm.NextMessageBatchCommand).ExecuteAsync(null);

        Assert.Contains("PAGE_BUDGET_EXCEEDED", vm.Error);
        Assert.Equal(requests + 1, app.Requests("get_messages_page").Count); // Not a size retry.
        Assert.Equal(original, vm.Messages);
        Assert.Equal(version, vm.MessageWindowVersion);
        Assert.False(vm.CanLoadNextMessageBatch);
        Assert.False(vm.CanLoadPreviousMessageBatch);
        Assert.Contains("最后一页仍保留", vm.MessagePagingStatus);
        Assert.Contains("刷新首屏", vm.MessagePagingStatus);
        Assert.True(vm.CanRefreshMessages);
        Assert.True(vm.CanExport); // Full-range exports do not consume the invalid preview cursor.
        Assert.Empty(app.Requests("get_messages"));
        await vm.NextMessageBatchCommand.ExecuteAsync(null);
        await vm.PreviousMessageBatchCommand.ExecuteAsync(null);
        Assert.Equal(requests + 1, app.Requests("get_messages_page").Count);

        await vm.RefreshMessagesCommand.ExecuteAsync(null);
        Assert.Equal("", vm.Error);
        Assert.True(vm.CanLoadNextMessageBatch);
        Assert.False(vm.CanLoadPreviousMessageBatch);
        Assert.Equal(50, vm.Messages.Count);
        Assert.DoesNotContain("最后一页仍保留", vm.MessagePagingStatus);
    }

    [Theory]
    [InlineData("legacy", 0)]
    [InlineData("no-token", 0)]
    [InlineData("schema", 1)]
    [InlineData("unverifiable", 1)]
    [InlineData("unsupported-worker", 1)]
    public async Task UnsupportedWorkerOrSchemaKeepsLegacyBrowsingWithAnExplicitDegradationNotice(string scenario, int attempts)
    {
        await using var app = new TestApp(scenario);
        await app.InitializeAsync();
        Assert.True(app.Vm.CanExport, app.Vm.Error);
        Assert.True(app.Vm.CanLoadMore);
        Assert.False(app.Vm.ShowCursorMessageNavigation);
        Assert.Contains("原有加载更多", app.Vm.MessagePagingStatus);
        Assert.Contains("未启用有限页窗口", app.Vm.MessagePagingStatus);
        Assert.Equal(attempts, app.Requests("get_messages_page").Count);
        await app.Vm.LoadMoreCommand.ExecuteAsync(null);
        Assert.Equal(100, app.Vm.Messages.Count);
        Assert.Equal(50, app.Requests("get_messages").Last().GetProperty("offset").GetInt32());
        await app.Vm.RefreshMessagesCommand.ExecuteAsync(null);
        Assert.Equal(attempts, app.Requests("get_messages_page").Count);
        Assert.Equal(50, app.Vm.Messages.Count);
    }

    [Fact]
    public async Task PerSourceCapabilitiesOverrideTheGlobalListAcrossSourceSwitches()
    {
        await using var app = new TestApp("source-scoped");
        await app.InitializeAsync();
        Assert.False(app.Vm.HasMessageCursorPaginationCapability);
        Assert.True(app.Vm.CanLoadMore);
        Assert.Empty(app.Requests("get_messages_page"));
        app.Vm.UseWechat = true;
        await app.Vm.ConnectWechatCommand.ExecuteAsync(null);
        Assert.True(app.Vm.HasMessageCursorPaginationCapability);
        Assert.True(app.Vm.ShowCursorMessageNavigation, app.Vm.Error);
        Assert.Single(app.Requests("get_messages_page"));
        await app.Vm.ImportJsonAsync(app.PathFor("offline.json"));
        Assert.False(app.Vm.HasMessageCursorPaginationCapability);
        Assert.True(app.Vm.CanLoadMore);
        Assert.Single(app.Requests("get_messages_page"));
    }

    [Fact]
    public async Task KeywordFallbackAndFullRangeExportTaskAndVoiceNeverConsumeLoadedPreviewOrCursor()
    {
        await using var app = new TestApp();
        await app.InitializeAsync();
        var vm = app.Vm;
        vm.StartDate = new DateTimeOffset(2026, 11, 1, 0, 0, 0, DateRange.Offset);
        vm.EndDate = new DateTimeOffset(2026, 11, 28, 0, 0, 0, DateRange.Offset);
        Assert.True(vm.FiltersDirty);
        Assert.False(vm.CanLoadNextMessageBatch);
        Assert.False(vm.CanExport);
        await vm.ApplyFiltersCommand.ExecuteAsync(null);
        await vm.NextMessageBatchCommand.ExecuteAsync(null);
        Assert.Equal(100, vm.Messages.Count);
        await vm.ExportAsync(app.PathFor("all.md"));
        Assert.Contains("2351", vm.ResultSummary);
        await vm.ExportJobCommand.ExecuteAsync(null);
        vm.EnableVoice = true;
        await vm.TranscribeCommand.ExecuteAsync(null);
        Assert.Equal(50, vm.Messages.Count); // Refresh preview only after full-range transcription.
        foreach (string command in new[] { "export_markdown", "export_job", "transcribe_messages" })
        {
            var request = Assert.Single(app.Requests(command));
            Assert.Equal("conv-1", request.GetProperty("conversationId").GetString());
            Assert.Equal("2026-11-01", request.GetProperty("startDate").GetString());
            Assert.Equal("2026-11-28", request.GetProperty("endDate").GetString());
            Assert.False(request.TryGetProperty("limit", out _));
            Assert.False(request.TryGetProperty("cursor", out _));
            Assert.False(request.TryGetProperty("pageSessionId", out _));
            Assert.False(request.TryGetProperty("offset", out _));
        }
        int cursorCalls = app.Requests("get_messages_page").Count;
        vm.SearchQuery = "needle";
        await vm.ApplyFiltersCommand.ExecuteAsync(null);
        Assert.Equal(cursorCalls, app.Requests("get_messages_page").Count);
        Assert.True(vm.CanLoadMore);
        Assert.False(vm.ShowCursorMessageNavigation);
        Assert.Contains("关键词", vm.MessagePagingStatus);
        Assert.Equal("needle", app.Requests("get_messages").Last().GetProperty("query").GetString());
        vm.SearchQuery = "";
        await vm.ApplyFiltersCommand.ExecuteAsync(null);
        Assert.True(vm.ShowCursorMessageNavigation);
        Assert.Equal(cursorCalls + 1, app.Requests("get_messages_page").Count);
    }

    [Fact]
    public async Task KeywordCapabilityUsesTheB3SnapshotAndTheSameBoundedWindow()
    {
        await using var app = new TestApp("keyword");
        await app.InitializeAsync();
        var vm = app.Vm;
        Assert.True(vm.HasMessageKeywordPaginationCapability);
        Assert.True(vm.ShowCursorMessageNavigation);

        vm.SearchQuery = "needle";
        await vm.ApplyFiltersCommand.ExecuteAsync(null);

        var first = app.Requests("get_messages_page").Last();
        Assert.Equal(new[] { "conversationId", "endDate", "limit", "order", "query", "source", "sourceSessionId", "startDate" },
            first.EnumerateObject().Select(p => p.Name).Order(StringComparer.Ordinal));
        Assert.Equal("needle", first.GetProperty("query").GetString());
        Assert.Equal(50, vm.Messages.Count);
        Assert.Contains("候选 400", vm.MessagePagingStatus);
        Assert.Contains("关键词快照", vm.MessagePagingStatus);

        await vm.NextMessageBatchCommand.ExecuteAsync(null);
        Assert.Equal(100, vm.Messages.Count);
        var continuation = app.Requests("get_messages_page").Last();
        Assert.Equal(new[] { "cursor", "limit", "pageSessionId", "source", "sourceSessionId" },
            continuation.EnumerateObject().Select(p => p.Name).Order(StringComparer.Ordinal));
        Assert.Contains("123", vm.MessageSummary);
        Assert.True(vm.CanLoadNextMessageBatch);
        Assert.False(vm.CanLoadMore);
    }

    [Fact]
    public async Task KeywordSnapshotFailureKeepsLastGoodPageAndRequiresRefresh()
    {
        await using var app = new TestApp("keyword-budget");
        await app.InitializeAsync();
        var vm = app.Vm;
        vm.SearchQuery = "needle";
        await vm.ApplyFiltersCommand.ExecuteAsync(null);
        var original = vm.Messages.ToArray();
        await vm.NextMessageBatchCommand.ExecuteAsync(null);
        Assert.Equal(original, vm.Messages);
        Assert.Contains("最后一页仍保留", vm.MessagePagingStatus);
        Assert.False(vm.CanLoadNextMessageBatch);
        Assert.True(vm.CanRefreshMessages);
    }

    [Fact]
    public async Task KeywordWorkerWithoutCapabilityFallsBackToLegacyRangePaging()
    {
        await using var app = new TestApp("normal");
        await app.InitializeAsync();
        var vm = app.Vm;
        vm.SearchQuery = "needle";
        await vm.ApplyFiltersCommand.ExecuteAsync(null);
        Assert.False(vm.HasMessageKeywordPaginationCapability);
        Assert.True(vm.ShowLegacyMessageNavigation);
        Assert.True(vm.CanLoadMore);
        Assert.Contains("关键词浏览暂用兼容分页", vm.MessagePagingStatus);
        Assert.Single(app.Requests("get_messages"));
        Assert.Equal("needle", app.Requests("get_messages").Last().GetProperty("query").GetString());
    }

    [Fact]
    public async Task GlobalAndContextModesSuspendCursorNavigationAndRestoreOrReplaceTheCorrectWindow()
    {
        await using var app = new TestApp();
        await app.InitializeAsync();
        var vm = app.Vm;
        await vm.NextMessageBatchCommand.ExecuteAsync(null);
        var original = vm.Messages.ToArray();
        vm.SwitchToGlobalSearchCommand.Execute(null);
        Assert.False(vm.ShowCursorMessageNavigation);
        Assert.False(vm.CanLoadNextMessageBatch);
        Assert.False(vm.CanRefreshMessages);
        int requests = app.Requests("get_messages_page").Count;
        await vm.NextMessageBatchCommand.ExecuteAsync(null);
        await vm.RefreshMessagesCommand.ExecuteAsync(null);
        Assert.Equal(requests, app.Requests("get_messages_page").Count);
        vm.GlobalSearchQuery = "needle";
        await vm.ApplyFiltersCommand.ExecuteAsync(null);
        Assert.True(await vm.OpenGlobalHitAsync(vm.GlobalSearchHits[0]));
        Assert.False(vm.CanExport);
        Assert.False(vm.CanLoadNextMessageBatch);
        Assert.Equal(original, vm.Messages);
        await vm.ReturnToSearchCommand.ExecuteAsync(null);
        await vm.SwitchToConversationCommand.ExecuteAsync(null);
        Assert.Equal(original, vm.Messages);
        Assert.True(vm.CanLoadNextMessageBatch);
        Assert.Equal(requests, app.Requests("get_messages_page").Count);
        vm.SwitchToGlobalSearchCommand.Execute(null);
        Assert.True(await vm.OpenGlobalHitAsync(vm.GlobalSearchHits[0]));
        await vm.BrowseContextConversationCommand.ExecuteAsync(null);
        Assert.Equal("conv-2", vm.SelectedConversation?.Id);
        Assert.False(vm.IsContextMode);
        Assert.False(vm.IsGlobalSearchMode);
        Assert.Equal(50, vm.Messages.Count);
        Assert.True(vm.CanExport);
        Assert.Equal("conv-2", app.Requests("get_messages_page").Last().GetProperty("conversationId").GetString());
    }

    [Theory]
    [InlineData("pause-next")]
    [InlineData("pause-first")]
    public async Task GateSpansTheRequestAndCommitAndDirtyLateResponsesNeverReplaceTheWindow(string scenario)
    {
        await using var app = new TestApp(scenario);
        await app.InitializeAsync();
        var vm = app.Vm;
        var original = vm.Messages.ToArray();
        Task operation = scenario == "pause-next" ? vm.NextMessageBatchCommand.ExecuteAsync(null) : vm.RefreshMessagesCommand.ExecuteAsync(null);
        await app.Paused.Task.WaitAsync(TimeSpan.FromSeconds(5));
        Assert.True(vm.IsBusy);
        Assert.False(vm.CanImportJson);
        Assert.False(vm.CanConnect);
        Assert.False(vm.CanExport);
        Assert.False(vm.CanLoadNextMessageBatch);
        Assert.False(vm.CanRefreshMessages);
        await vm.ImportJsonAsync(app.PathFor("must-not-import.json"));
        await vm.RefreshMessagesCommand.ExecuteAsync(null);
        vm.SwitchToGlobalSearchCommand.Execute(null);
        Assert.Empty(app.Requests("import_json"));
        Assert.False(vm.IsGlobalSearchMode);
        vm.SearchQuery = "edited-in-flight";
        app.Release();
        await operation;
        Assert.Contains("筛选条件已变化", vm.Error);
        Assert.Equal(original, vm.Messages);
        Assert.True(vm.FiltersDirty);
        Assert.False(vm.CanLoadNextMessageBatch);
        Assert.False(vm.CanExport);
        Assert.False(vm.IsBusy);
    }

    [Theory]
    [InlineData("source-session-changed")]
    [InlineData("exit-next")]
    public async Task LostWorkerOrSourceInvalidatesTheWholePreviewAndExportScope(string scenario)
    {
        await using var app = new TestApp(scenario);
        await app.InitializeAsync();
        await app.Vm.NextMessageBatchCommand.ExecuteAsync(null);
        Assert.False(app.Vm.IsConnected);
        Assert.Null(app.Vm.SourceSessionId);
        Assert.Empty(app.Vm.Messages);
        Assert.Equal(0, app.Vm.VisibleMessagePageCount);
        Assert.False(app.Vm.CanExport);
        Assert.False(app.Vm.CanLoadNextMessageBatch);
    }

    [Fact]
    public async Task SourceInvalidationDuringRequestRejectsItsLateResponse()
    {
        await using var app = new TestApp("pause-next");
        await app.InitializeAsync();
        Task operation = app.Vm.NextMessageBatchCommand.ExecuteAsync(null);
        await app.Paused.Task.WaitAsync(TimeSpan.FromSeconds(5));
        app.Vm.IsConnected = false;
        app.Release();
        await operation;
        Assert.Empty(app.Vm.Messages);
        Assert.Equal(0, app.Vm.VisibleMessagePageCount);
        Assert.False(app.Vm.CanExport);
    }

    [Fact]
    public async Task FailedImportPreservesCursorButSuccessfulImportAndFailedReconnectDiscardIt()
    {
        await using var app = new TestApp("connect-fail");
        await app.InitializeAsync();
        await app.Vm.NextMessageBatchCommand.ExecuteAsync(null);
        var original = app.Vm.Messages.ToArray();
        string? session = app.Vm.SourceSessionId;
        await app.Vm.ImportJsonAsync(app.PathFor("reject.json"));
        Assert.Equal(original, app.Vm.Messages);
        Assert.Equal(session, app.Vm.SourceSessionId);
        Assert.True(app.Vm.CanLoadNextMessageBatch);
        await app.Vm.ImportJsonAsync(app.PathFor("replacement.json"));
        Assert.NotEqual(session, app.Vm.SourceSessionId);
        Assert.Equal(50, app.Vm.Messages.Count);
        Assert.Equal(1, app.Vm.VisibleMessagePageCount);
        Assert.Equal(app.Vm.SourceSessionId, app.Requests("get_messages_page").Last().GetProperty("sourceSessionId").GetString());
        await app.Vm.ConnectWechatCommand.ExecuteAsync(null);
        Assert.False(app.Vm.IsConnected);
        Assert.Empty(app.Vm.Messages);
        Assert.Equal(0, app.Vm.VisibleMessagePageCount);
        Assert.False(app.Vm.CanExport);
    }

    [Theory]
    [InlineData("wrong-page", false)]
    [InlineData("bad-first", true)]
    public async Task InvalidResponsesCommitNeitherMessagesNorAppliedCriteria(string scenario, bool refresh)
    {
        await using var app = new TestApp(scenario);
        await app.InitializeAsync();
        var original = app.Vm.Messages.ToArray();
        string applied = app.Vm.RangeLabel;
        if (refresh)
        {
            app.Vm.StartDate = new DateTimeOffset(2026, 11, 1, 0, 0, 0, DateRange.Offset);
            await app.Vm.RefreshMessagesCommand.ExecuteAsync(null);
        }
        else await app.Vm.NextMessageBatchCommand.ExecuteAsync(null);
        Assert.NotEmpty(app.Vm.Error);
        Assert.Equal(original, app.Vm.Messages);
        Assert.Equal(applied, app.Vm.RangeLabel);
        Assert.Equal(refresh, app.Vm.FiltersDirty);
        Assert.Empty(app.Requests("get_messages"));
    }

    private sealed class TestApp : IAsyncDisposable
    {
        private readonly string _root = Path.Combine(Path.GetTempPath(), "wechat-native-paging-" + Guid.NewGuid().ToString("N"));
        public MainViewModel Vm { get; }
        public TaskCompletionSource Paused { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);
        public TestApp(string scenario = "normal", bool demo = true)
        {
            Directory.CreateDirectory(_root);
            Vm = new MainViewModel(action => action(), () =>
            {
                string? project = AppContext.BaseDirectory;
                while (project is not null && !File.Exists(Path.Combine(project, "global.json"))) project = Directory.GetParent(project)?.FullName;
                Assert.NotNull(project);
                var info = new ProcessStartInfo(Path.Combine(project!, ".venv", "Scripts", "python.exe")) { WorkingDirectory = _root };
                foreach (string arg in new[] { "-I", "-X", "utf8", "-u", Path.Combine(AppContext.BaseDirectory, "fixtures", "message_paging_vm_worker.py"), scenario, _root })
                    info.ArgumentList.Add(arg);
                var worker = WorkerClient.Start(info);
                worker.ProgressReceived += progress => { if (progress.Label == "fixture_paused") Paused.TrySetResult(); };
                return worker;
            }, demo, (_, _) => { });
            Vm.Conversations.CollectionChanged += (_, change) =>
            {
                if (change.Action == System.Collections.Specialized.NotifyCollectionChangedAction.Reset) Vm.SelectedConversation = null;
            };
        }
        public async Task InitializeAsync()
        {
            await Vm.InitializeAsync();
            Assert.Equal("", Vm.Error);
        }
        public string PathFor(string name) => Path.Combine(_root, name);
        public void Release() => File.WriteAllText(PathFor("release"), "");
        public List<JsonElement> Requests(string command)
        {
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
