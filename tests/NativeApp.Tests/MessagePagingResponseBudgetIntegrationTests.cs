using System.Diagnostics;
using System.Text.Json;
using WechatMemory.NativeApp.Services;
using WechatMemory.NativeApp.ViewModels;
using Xunit;

namespace NativeApp.Tests;

public sealed class MessagePagingResponseBudgetIntegrationTests
{
    [Theory]
    [InlineData("offline")]
    [InlineData("wechat4")]
    [InlineData("demo")]
    public async Task RealWorkerLongMessagesShrinkWithoutLosingBodiesOrEitherDirectionalBoundary(string source)
    {
        await using var peer = new BudgetPeer(source, 180, i => LongContent(i));
        var worker = peer.Start();
        var hello = await worker.RequestAsync("hello", new { });
        var identity = source == "offline"
            ? await worker.RequestAsync("import_json", new { path = peer.DataPath }) : hello;
        string session = identity.GetProperty("sourceSessionId").GetString()!;
        var recorder = new RecordingClient(worker);
        var workflow = new MessagePagingWorkflow();
        var window = await workflow.FirstPageAsync(recorder, source, session, "c", new(null, null));
        Assert.Equal(new[] { 50, 25 }, recorder.Requests.Select(request => request.GetProperty("limit").GetInt32()));
        Assert.Equal(25, window.First.Items.Count);
        Assert.Equal(180, window.Total);
        var forward = window.Items.ToList();
        while (window.Last.NextCursor is not null)
        {
            int before = recorder.Requests.Count;
            string boundary = window.Last.NextCursor;
            window = await workflow.NavigateAsync(recorder, source, window, false);
            Assert.All(recorder.Requests.Skip(before), request => Assert.Equal(boundary, request.GetProperty("cursor").GetString()));
            forward.AddRange(window.Last.Items);
            Assert.InRange(window.DisplayBytes, 1, MessagePagingWorkflow.MaxDisplayBytes);
        }
        var expected = Enumerable.Range(0, 180).Reverse().Select(LongContent).ToArray();
        Assert.Equal(expected, forward.Select(item => item.Content));
        Assert.Equal(180, window.Last.EndIndex);
        Assert.True(window.First.StartIndex > 0); // Actual display-byte eviction, not a hand-built window.
        var backward = window.Items.ToList();
        while (window.First.PreviousCursor is not null)
        {
            int before = recorder.Requests.Count;
            string boundary = window.First.PreviousCursor;
            long end = window.First.StartIndex;
            window = await workflow.NavigateAsync(recorder, source, window, true);
            Assert.All(recorder.Requests.Skip(before), request => Assert.Equal(boundary, request.GetProperty("cursor").GetString()));
            Assert.Equal(end, window.First.EndIndex);
            backward.InsertRange(0, window.First.Items);
            Assert.InRange(window.DisplayBytes, 1, MessagePagingWorkflow.MaxDisplayBytes);
        }
        Assert.Equal(expected, backward.Select(item => item.Content));
        Assert.Equal(0, window.First.StartIndex);
        Assert.True(worker.IsAvailable);
    }

    [Fact]
    public async Task ImportedFiftyThirtyThousandCharacterMessagesLoadRefreshAndRemainExportableInTheViewModel()
    {
        await using var peer = new BudgetPeer("offline", 50, i => LongContent(i));
        await using var vm = new MainViewModel(action => action(), peer.Start, true, (_, _) => { });
        await vm.InitializeAsync();
        Assert.Equal("", vm.Error);
        await vm.ImportJsonAsync(peer.DataPath);
        Assert.Equal("", vm.Error);
        Assert.True(vm.IsConnected);
        Assert.True(vm.CanExport);
        Assert.True(vm.ShowCursorMessageNavigation);
        Assert.False(vm.ShowLegacyMessageNavigation);
        Assert.Equal(25, vm.Messages.Count);
        Assert.Equal(Enumerable.Range(25, 25).Reverse().Select(LongContent), vm.Messages.Select(item => item.Content));
        await vm.NextMessageBatchCommand.ExecuteAsync(null);
        Assert.Equal("", vm.Error);
        Assert.Equal(Enumerable.Range(0, 50).Reverse().Select(LongContent), vm.Messages.Select(item => item.Content));
        Assert.False(vm.CanLoadNextMessageBatch);
        Assert.True(vm.CanExport);
        await vm.RefreshMessagesCommand.ExecuteAsync(null);
        Assert.Equal("", vm.Error);
        Assert.Equal(25, vm.Messages.Count);
        Assert.True(vm.CanLoadNextMessageBatch);
        Assert.True(vm.CanExport);
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public async Task RealWorkerSingleOversizedMessageFailsExplicitlyAfterSixRequestsAndPreservesAnyExistingWindow(bool continuation)
    {
        await using var peer = new BudgetPeer("offline", continuation ? 61 : 1,
            i => i == (continuation ? 10 : 0) ? new string('x', MessagePagingWorkflow.MaxResponseBytes) : "row-" + i);
        var worker = peer.Start();
        await worker.RequestAsync("hello", new { });
        var imported = await worker.RequestAsync("import_json", new { path = peer.DataPath });
        string session = imported.GetProperty("sourceSessionId").GetString()!;
        var recorder = new RecordingClient(worker);
        var workflow = new MessagePagingWorkflow();
        var original = continuation ? await workflow.FirstPageAsync(recorder, "offline", session, "c", new(null, null)) : null;
        int before = recorder.Requests.Count;
        var error = await Assert.ThrowsAsync<WorkerException>(() => original is null
            ? workflow.FirstPageAsync(recorder, "offline", session, "c", new(null, null))
            : workflow.NavigateAsync(recorder, "offline", original, false));
        Assert.Equal("PAGE_RESPONSE_TOO_LARGE", error.Code);
        Assert.Contains("1 条", error.Message);
        Assert.Contains("正文未截断", error.Message);
        Assert.Equal(new[] { 50, 25, 12, 6, 3, 1 }, recorder.Requests.Skip(before).Select(request => request.GetProperty("limit").GetInt32()));
        if (original is not null)
        {
            Assert.Single(original.Pages);
            Assert.Equal(50, original.Last.EndIndex);
            Assert.Equal(Enumerable.Range(11, 50).Reverse().Select(i => "row-" + i), original.Items.Select(item => item.Content));
            Assert.All(recorder.Requests.Skip(before), request => Assert.Equal(original.Last.NextCursor, request.GetProperty("cursor").GetString()));
        }
        await worker.RequestAsync("list_conversations", new { source = "offline" });
        Assert.True(worker.IsAvailable); // An application-level size failure never kills the worker.
    }

    [Fact]
    public async Task RealWorkerTerminalSizeFailureKeepsTheViewModelsLastWindowAndRequiresRefreshWithoutLegacyFallback()
    {
        await using var peer = new BudgetPeer("offline", 61,
            i => i == 10 ? new string('x', MessagePagingWorkflow.MaxResponseBytes) : "row-" + i);
        await using var vm = new MainViewModel(action => action(), peer.Start, true, (_, _) => { });
        await vm.InitializeAsync();
        await vm.ImportJsonAsync(peer.DataPath);
        Assert.Equal("", vm.Error);
        var original = vm.Messages.ToArray();
        await vm.NextMessageBatchCommand.ExecuteAsync(null);
        Assert.Contains("1 条", vm.Error);
        Assert.Equal(original, vm.Messages);
        Assert.True(vm.ShowCursorMessageNavigation);
        Assert.False(vm.ShowLegacyMessageNavigation);
        Assert.False(vm.CanLoadNextMessageBatch);
        Assert.False(vm.CanLoadPreviousMessageBatch);
        Assert.Contains("最后一页仍保留", vm.MessagePagingStatus);
        Assert.Contains("刷新首屏", vm.MessagePagingStatus);
        Assert.True(vm.CanExport);
        Assert.True(vm.CanRefreshMessages);
        await vm.RefreshMessagesCommand.ExecuteAsync(null);
        Assert.Equal("", vm.Error);
        Assert.Equal(original, vm.Messages);
        Assert.True(vm.CanLoadNextMessageBatch);
    }

    private static string LongContent(int index)
    {
        string prefix = "row-" + index + "|";
        return prefix + new string('x', 30_000 - prefix.Length);
    }

    private sealed class RecordingClient(WorkerClient worker) : IWorkerRequestClient
    {
        public List<JsonElement> Requests { get; } = [];
        public Task<JsonElement> RequestAsync(string command, object payload, TimeSpan? timeout = null)
        {
            Assert.Equal("get_messages_page", command);
            Requests.Add(JsonSerializer.SerializeToElement(payload, Protocol.Json));
            return worker.RequestAsync(command, payload, timeout);
        }
    }

    private sealed class BudgetPeer : IAsyncDisposable
    {
        private readonly string _source;
        private WorkerClient? _worker;
        private string? _nativeTempRoot;
        public string Root { get; } = Path.Combine(Path.GetTempPath(), "wechat-native-page-budget-" + Guid.NewGuid().ToString("N"));
        public string DataPath => Path.Combine(Root, "long-messages.json");
        public BudgetPeer(string source, int count, Func<int, string> content)
        {
            _source = source;
            Directory.CreateDirectory(Root);
            WriteData(DataPath, count, content);
            WriteData(Path.Combine(Root, "demo.json"), 2, i => "demo-" + i);
        }
        private static void WriteData(string path, int count, Func<int, string> content) => File.WriteAllText(path,
            JsonSerializer.Serialize(new
            {
                version = 1,
                conversations = new[] { new
                {
                    id = "c", name = "Synthetic budget fixture", kind = "direct",
                    messages = Enumerable.Range(0, count).Select(i => new
                    {
                        id = "m-" + i, sender = "synthetic", type = "text", content = content(i),
                        timestamp = new DateTimeOffset(2026, 11, 25, 12, 0, 0, DateRange.Offset).AddSeconds(i).ToString("O"),
                        is_outgoing = false,
                    }),
                } },
            }));
        public WorkerClient Start()
        {
            string? project = AppContext.BaseDirectory;
            while (project is not null && !File.Exists(Path.Combine(project, "global.json"))) project = Directory.GetParent(project)?.FullName;
            Assert.NotNull(project);
            var info = new ProcessStartInfo(Path.Combine(project!, ".venv", "Scripts", "python.exe")) { WorkingDirectory = Root };
            foreach (string arg in new[] { "-I", "-X", "utf8", "-u" }) info.ArgumentList.Add(arg);
            if (_source == "wechat4")
            {
                foreach (string arg in new[] { Path.Combine(AppContext.BaseDirectory, "fixtures", "message_paging_real_worker.py"),
                    project!, Root, "long-messages" }) info.ArgumentList.Add(arg);
            }
            else
            {
                foreach (string arg in new[] { Path.Combine(project!, "src", "PythonWorker", "worker.py"),
                    "--demo", _source == "demo" ? DataPath : Path.Combine(Root, "demo.json"),
                    "--jobs-dir", Path.Combine(Root, "jobs"), "--scenes-file", Path.Combine(Root, "scenes.json"),
                    "--model-root", Path.Combine(Root, "models") }) info.ArgumentList.Add(arg);
            }
            info.Environment["LOCALAPPDATA"] = Path.Combine(Root, "local");
            info.Environment["APPDATA"] = Path.Combine(Root, "roaming");
            info.Environment["TEMP"] = info.Environment["TMP"] = Root;
            _worker = WorkerClient.Start(info);
            _nativeTempRoot = _worker.TempRootPath; // KnownFolder ignores LOCALAPPDATA; this separate root is GUID-owned.
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
