using System.Diagnostics;
using System.Text.Json;
using WechatMemory.NativeApp.Services;
using WechatMemory.NativeApp.ViewModels;
using Xunit;

namespace NativeApp.Tests;

public sealed class MessagePagingWorkerIntegrationTests
{
    [Fact]
    public async Task ActualWorkerAndSourceAcceptThePlannedPayloadAndPageSyntheticShardsBothWays()
    {
        await using var peer = new RealPeer(syntheticWechat: true);
        var worker = peer.Start();
        var hello = await worker.RequestAsync("hello", new { });
        Assert.Contains("message_cursor_pagination", hello.GetProperty("capabilities").EnumerateArray().Select(v => v.GetString()));
        Assert.Contains("message_cursor_pagination", hello.GetProperty("sourceCapabilities").GetProperty("wechat4").EnumerateArray().Select(v => v.GetString()));
        string session = hello.GetProperty("sourceSessionId").GetString()!;
        var workflow = new MessagePagingWorkflow();
        var window = await workflow.FirstPageAsync(worker, "wechat4", session, "c", new(null, null));
        Assert.Equal(1203, window.Total);
        Assert.Equal("row-1202", window.Items.First().Content);
        Assert.Equal("row-1153", window.Items.Last().Content);
        Assert.Single(window.Items.Select(item => item.Id).Distinct()); // All physical rows deliberately share public IDs.
        for (int page = 0; page < 20; ++page) window = await workflow.NavigateAsync(worker, "wechat4", window, false);
        Assert.Equal(20, window.Pages.Count);
        Assert.Equal("row-1152", window.Items.First().Content);
        Assert.Equal("row-153", window.Items.Last().Content);
        Assert.Equal(50, window.First.StartIndex);
        window = await workflow.NavigateAsync(worker, "wechat4", window, true);
        Assert.Null(window.First.PreviousCursor);
        Assert.Equal("row-1202", window.Items.First().Content);
        Assert.Equal("row-203", window.Items.Last().Content);
        while (window.Last.NextCursor is not null) window = await workflow.NavigateAsync(worker, "wechat4", window, false);
        Assert.Equal("row-0", window.Items.Last().Content);
        Assert.Equal(1203, window.Last.EndIndex);
        Assert.Equal(3, window.Last.Items.Count);
        Assert.InRange(window.Items.Count(), 1, 1000);

        await worker.RequestAsync("fixture_mutate", new { });
        var changed = await Assert.ThrowsAsync<WorkerException>(() => workflow.NavigateAsync(worker, "wechat4", window, true));
        Assert.Equal("PAGE_SOURCE_CHANGED", changed.Code);
        var refreshed = await workflow.FirstPageAsync(worker, "wechat4", session, "c", new(null, null));
        Assert.NotEqual(window.First.PageSessionId, refreshed.First.PageSessionId);
        var unknown = await Assert.ThrowsAsync<WorkerException>(() => worker.RequestAsync("get_messages_page", new
        {
            source = "wechat4", sourceSessionId = session, pageSessionId = "expired-fixture-session", cursor = "expired-fixture-cursor", limit = 50,
        }));
        Assert.Equal("PAGE_EXPIRED", unknown.Code);
        var replacement = await worker.RequestAsync("import_json", new { path = peer.DataPath });
        Assert.NotEqual(session, replacement.GetProperty("sourceSessionId").GetString());
        var staleSource = await Assert.ThrowsAsync<WorkerException>(() => workflow.NavigateAsync(worker, "wechat4", refreshed, false));
        Assert.Equal("SOURCE_SESSION_CHANGED", staleSource.Code);
        Assert.True(worker.IsAvailable);
    }

    [Theory]
    [InlineData("asc")]
    [InlineData("desc")]
    public async Task ActualWorkerAdvertisesOfflineCursorsAndPagesImportedMessagesBothWays(string order)
    {
        await using var peer = new RealPeer(syntheticWechat: false);
        var worker = peer.Start();
        var hello = await worker.RequestAsync("hello", new { });
        Assert.Contains("message_cursor_pagination", hello.GetProperty("capabilities").EnumerateArray().Select(v => v.GetString()));
        Assert.Contains("message_cursor_pagination", hello.GetProperty("sourceCapabilities").GetProperty("offline").EnumerateArray().Select(v => v.GetString()));
        var demoCapabilities = hello.GetProperty("sourceCapabilities").GetProperty("demo").EnumerateArray()
            .Select(value => value.GetString()).ToArray();
        Assert.Contains("message_cursor_pagination", demoCapabilities);
        Assert.Contains("message_keyword_pagination", demoCapabilities);
        var imported = await worker.RequestAsync("import_json", new { path = peer.DataPath });
        Assert.Equal("offline", imported.GetProperty("source").GetString());
        string session = imported.GetProperty("sourceSessionId").GetString()!;
        Assert.NotEmpty(session);
        Assert.NotEqual(hello.GetProperty("sourceSessionId").GetString(), session);

        var first = await worker.RequestAsync("get_messages_page", new
        {
            source = "offline", sourceSessionId = session, conversationId = "c", order, limit = 50,
        });
        Assert.Equal("precise-v1", first.GetProperty("sortVersion").GetString());
        Assert.Equal(61, first.GetProperty("total").GetInt32());
        Assert.Equal(50, first.GetProperty("items").GetArrayLength());
        Assert.Equal(JsonValueKind.Null, first.GetProperty("previousCursor").ValueKind);
        string pageSession = first.GetProperty("pageSessionId").GetString()!;
        string nextCursor = first.GetProperty("nextCursor").GetString()!;
        Assert.NotEmpty(pageSession);
        Assert.NotEmpty(nextCursor);

        var next = await worker.RequestAsync("get_messages_page", new
        {
            source = "offline", sourceSessionId = session, pageSessionId = pageSession, cursor = nextCursor, limit = 50,
        });
        Assert.Equal(pageSession, next.GetProperty("pageSessionId").GetString());
        Assert.Equal("precise-v1", next.GetProperty("sortVersion").GetString());
        Assert.Equal(61, next.GetProperty("total").GetInt32());
        Assert.Equal(11, next.GetProperty("items").GetArrayLength());
        Assert.Equal(JsonValueKind.Null, next.GetProperty("nextCursor").ValueKind);
        string previousCursor = next.GetProperty("previousCursor").GetString()!;
        Assert.NotEmpty(previousCursor);
        var expectedIds = Enumerable.Range(0, 61).Select(i => "m-" + i);
        if (order == "desc") expectedIds = expectedIds.Reverse();
        Assert.Equal(expectedIds, first.GetProperty("items").EnumerateArray()
            .Concat(next.GetProperty("items").EnumerateArray()).Select(item => item.GetProperty("id").GetString()));

        var previous = await worker.RequestAsync("get_messages_page", new
        {
            source = "offline", sourceSessionId = session, pageSessionId = pageSession, cursor = previousCursor, limit = 50,
        });
        Assert.Equal(pageSession, previous.GetProperty("pageSessionId").GetString());
        Assert.Equal("precise-v1", previous.GetProperty("sortVersion").GetString());
        Assert.Equal(61, previous.GetProperty("total").GetInt32());
        Assert.Equal(JsonValueKind.Null, previous.GetProperty("previousCursor").ValueKind);
        Assert.Equal(expectedIds.Take(50), previous.GetProperty("items").EnumerateArray().Select(item => item.GetProperty("id").GetString()));
    }

    [Fact]
    public async Task ProductionViewModelUsesDemoAndOfflineCursorsWithFullRangeExport()
    {
        await using var peer = new RealPeer(syntheticWechat: false);
        await using var vm = new MainViewModel(action => action(), peer.Start, true, (_, _) => { });
        await vm.InitializeAsync();
        Assert.Equal("", vm.Error);
        Assert.True(vm.HasMessageCursorPaginationCapability);
        Assert.True(vm.ShowCursorMessageNavigation);
        Assert.False(vm.ShowLegacyMessageNavigation);
        Assert.False(vm.CanLoadMore);
        Assert.True(vm.CanLoadNextMessageBatch);
        Assert.Equal(50, vm.Messages.Count);
        Assert.Contains("稳定游标", vm.MessagePagingStatus);
        await vm.LoadMoreCommand.ExecuteAsync(null);
        Assert.Equal(50, vm.Messages.Count); // Legacy loading is not an implicit fallback.
        await vm.NextMessageBatchCommand.ExecuteAsync(null);
        Assert.Equal("", vm.Error);
        Assert.Equal(61, vm.Messages.Count);
        Assert.False(vm.CanLoadNextMessageBatch);

        Assert.True(vm.HasMessageKeywordPaginationCapability);
        vm.SearchQuery = "synthetic-message";
        await vm.ApplyFiltersCommand.ExecuteAsync(null);
        Assert.Equal("", vm.Error);
        Assert.True(vm.ShowCursorMessageNavigation);
        Assert.False(vm.ShowLegacyMessageNavigation);
        Assert.Equal(50, vm.Messages.Count); // The keyword snapshot publishes only its first bounded page.
        Assert.Contains("synthetic-message", vm.RangeLabel);

        await vm.ImportJsonAsync(peer.DataPath);
        Assert.Equal("", vm.Error);
        Assert.True(vm.HasMessageCursorPaginationCapability);
        Assert.True(vm.ShowCursorMessageNavigation);
        Assert.False(vm.ShowLegacyMessageNavigation);
        Assert.False(vm.CanLoadMore);
        Assert.True(vm.CanLoadNextMessageBatch);
        Assert.False(vm.CanLoadPreviousMessageBatch);
        Assert.Equal(1, vm.VisibleMessagePageCount);
        Assert.Equal(50, vm.Messages.Count);
        Assert.Equal(Enumerable.Range(11, 50).Reverse().Select(i => "m-" + i), vm.Messages.Select(item => item.Id));
        string exportPath = Path.Combine(peer.Root, "complete.md");
        await vm.ExportAsync(exportPath);
        Assert.Equal("", vm.Error);
        Assert.Contains("61", vm.ResultSummary);
        Assert.Contains(@"synthetic\-message\-0", await File.ReadAllTextAsync(exportPath));
        Assert.Equal(50, vm.Messages.Count);
        await vm.NextMessageBatchCommand.ExecuteAsync(null);
        Assert.Equal("", vm.Error);
        Assert.Equal(61, vm.Messages.Count);
        Assert.Equal(2, vm.VisibleMessagePageCount);
        Assert.Equal(Enumerable.Range(0, 61).Reverse().Select(i => "m-" + i), vm.Messages.Select(item => item.Id));
        Assert.False(vm.CanLoadNextMessageBatch);
        Assert.False(vm.CanLoadMore);
    }

    private sealed class RealPeer : IAsyncDisposable
    {
        public string Root { get; } = Path.Combine(Path.GetTempPath(), "wechat-native-b2-real-" + Guid.NewGuid().ToString("N"));
        public string DataPath => Path.Combine(Root, "synthetic.json");
        private readonly bool _syntheticWechat;
        private WorkerClient? _worker;
        private string? _nativeTempRoot;
        public RealPeer(bool syntheticWechat)
        {
            _syntheticWechat = syntheticWechat;
            Directory.CreateDirectory(Root);
            File.WriteAllText(DataPath, JsonSerializer.Serialize(new
            {
                version = 1,
                conversations = new[] { new
                {
                    id = "c", name = "合成会话", kind = "direct",
                    messages = Enumerable.Range(0, 61).Select(i => new
                    {
                        id = "m-" + i, sender = "合成成员", type = "text", content = "synthetic-message-" + i,
                        timestamp = new DateTimeOffset(2026, 11, 25, 12, 0, 0, DateRange.Offset).AddSeconds(i).ToString("O"),
                        is_outgoing = false,
                    }),
                } },
            }));
        }
        public WorkerClient Start()
        {
            string? project = AppContext.BaseDirectory;
            while (project is not null && !File.Exists(Path.Combine(project, "global.json"))) project = Directory.GetParent(project)?.FullName;
            Assert.NotNull(project);
            var info = new ProcessStartInfo(Path.Combine(project!, ".venv", "Scripts", "python.exe")) { WorkingDirectory = Root };
            foreach (string arg in new[] { "-I", "-X", "utf8", "-u" }) info.ArgumentList.Add(arg);
            if (_syntheticWechat)
            {
                info.ArgumentList.Add(Path.Combine(AppContext.BaseDirectory, "fixtures", "message_paging_real_worker.py"));
                info.ArgumentList.Add(project!);
                info.ArgumentList.Add(Root);
            }
            else
            {
                foreach (string arg in new[] { Path.Combine(project!, "src", "PythonWorker", "worker.py"), "--demo", DataPath,
                    "--jobs-dir", Path.Combine(Root, "jobs"), "--scenes-file", Path.Combine(Root, "scenes.json"),
                    "--model-root", Path.Combine(Root, "models") }) info.ArgumentList.Add(arg);
            }
            info.Environment["LOCALAPPDATA"] = Path.Combine(Root, "local");
            info.Environment["APPDATA"] = Path.Combine(Root, "roaming");
            info.Environment["TEMP"] = info.Environment["TMP"] = Root;
            _worker = WorkerClient.Start(info);
            // KnownFolder is not redirected by LOCALAPPDATA. Only this worker's
            // separately generated, GUID-owned native root is checked/removed.
            _nativeTempRoot = _worker.TempRootPath;
            return _worker;
        }
        public async ValueTask DisposeAsync()
        {
            if (_worker is not null) await _worker.DisposeAsync();
            if (_nativeTempRoot is not null) Assert.False(Directory.Exists(_nativeTempRoot));
            Directory.Delete(Root, recursive: true);
        }
    }
}
