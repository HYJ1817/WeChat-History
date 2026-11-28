using System.Diagnostics;
using System.Text.Json;
using WechatMemory.NativeApp.Models;
using WechatMemory.NativeApp.Services;
using Xunit;

namespace NativeApp.Tests;

public sealed class GlobalSearchWorkflowTests
{
    [Fact]
    public async Task ProductionWorkflowUsesBoundSearchPagesAndValidatesContextThroughWorkerPipe()
    {
        await using var worker = StartFakeWorker();
        var recorder = new RecordingWorker(worker);
        var workflow = new GlobalSearchWorkflow();
        var criteria = new GlobalSearchCriteria("needle", "2026-11-01", "2026-11-28", "asc");

        var first = await workflow.SearchFirstPageAsync(recorder, "offline", "session-1", criteria, limit: 1);
        Assert.Equal("search-1", first.SearchId);
        Assert.Equal("session-1", first.SourceSessionId);
        Assert.Equal("hit-1", Assert.Single(first.Items).HitId);
        Assert.Equal(1, first.NextOffset);
        Assert.Equal(2, first.Total);

        var initialPayload = recorder.Payloads[0];
        Assert.Equal("needle", initialPayload.GetProperty("query").GetString());
        Assert.Equal("2026-11-01", initialPayload.GetProperty("startDate").GetString());
        Assert.Equal("2026-11-28", initialPayload.GetProperty("endDate").GetString());
        Assert.Equal("asc", initialPayload.GetProperty("order").GetString());
        Assert.Equal(0, initialPayload.GetProperty("offset").GetInt32());
        Assert.Equal(1, initialPayload.GetProperty("limit").GetInt32());
        Assert.False(initialPayload.TryGetProperty("conversationId", out _));

        var second = await workflow.SearchNextPageAsync(recorder, "offline", first, limit: 1);
        Assert.Equal(new[] { "hit-1", "hit-2" }, second.Items.Select(hit => hit.HitId));
        var nextPayload = recorder.Payloads[1];
        Assert.Equal(new[] { "limit", "offset", "searchId", "source", "sourceSessionId" },
            nextPayload.EnumerateObject().Select(property => property.Name).OrderBy(name => name, StringComparer.Ordinal));
        Assert.Equal(1, nextPayload.GetProperty("offset").GetInt32());
        Assert.False(nextPayload.TryGetProperty("query", out _));
        Assert.False(nextPayload.TryGetProperty("startDate", out _));
        Assert.False(nextPayload.TryGetProperty("order", out _));

        var context = await workflow.GetContextAsync(recorder, "offline", second, second.Items[0]);
        Assert.Equal("conv-1", context.Conversation.Id);
        Assert.Equal(1, context.FocusIndex);
        Assert.True(context.Items[context.FocusIndex].IsContextFocus);
        Assert.Equal("msg-1", context.Items[context.FocusIndex].Id);
        Assert.False(context.Items[context.FocusIndex].HasAttachment);
        var contextPayload = recorder.Payloads[2];
        Assert.Equal(new[] { "after", "before", "hitId", "searchId", "source", "sourceSessionId" },
            contextPayload.EnumerateObject().Select(property => property.Name).OrderBy(name => name, StringComparer.Ordinal));
        Assert.Equal(20, contextPayload.GetProperty("before").GetInt32());
        Assert.Equal(20, contextPayload.GetProperty("after").GetInt32());
    }

    [Fact]
    public async Task WorkflowRejectsSearchResultsFromAnotherSourceSession()
    {
        var worker = new FixedResponseWorker("""
            {"searchId":"s1","sourceSessionId":"source-other","items":[],"total":0,
             "resultLimit":1000,"truncated":false,"nextOffset":null,"coverage":{"complete":true}}
            """);
        var workflow = new GlobalSearchWorkflow();

        var error = await Assert.ThrowsAsync<WorkerException>(() => workflow.SearchFirstPageAsync(
            worker, "demo", "source-expected", new GlobalSearchCriteria("needle", null, null, "desc")));

        Assert.Equal("PROTOCOL_ERROR", error.Code);
    }

    [Fact]
    public void SessionStateDisablesPaginationAndLocatorsForDirtyOrChangedSource()
    {
        using var coverage = JsonDocument.Parse("{\"complete\":true}");
        var criteria = new GlobalSearchCriteria("needle", null, null, "desc");
        var snapshot = new GlobalSearchSnapshot("search-1", "source-1", [], 2, 2, false, 1, coverage.RootElement.Clone());
        var state = new GlobalSearchSessionState();
        state.ActivateSource("source-1");
        state.CommitSearch(criteria, snapshot);

        Assert.True(state.CanNavigate);
        Assert.True(state.CanContinue);
        state.UpdateCriteria(criteria with { Query = "changed" });
        Assert.True(state.IsDirty);
        Assert.False(state.CanNavigate);
        Assert.False(state.CanContinue);

        state.UpdateCriteria(criteria);
        Assert.False(state.IsDirty);
        Assert.True(state.CanNavigate);
        state.ActivateSource("source-2");
        Assert.Null(state.Snapshot);
        Assert.Null(state.AppliedCriteria);
        Assert.False(state.CanNavigate);
        Assert.False(state.CanContinue);
    }

    private static WorkerClient StartFakeWorker()
    {
        string? root = AppContext.BaseDirectory;
        while (root is not null && !File.Exists(Path.Combine(root, "global.json")))
            root = Directory.GetParent(root)?.FullName;
        Assert.NotNull(root);
        string python = Path.Combine(root, ".venv", "Scripts", "python.exe");
        Assert.True(File.Exists(python), "Run scripts/Prepare-Python.ps1 before C# pipe integration tests.");
        var info = new ProcessStartInfo(python) { WorkingDirectory = root };
        foreach (var arg in new[] { "-I", "-X", "utf8", "-u", Path.Combine(AppContext.BaseDirectory, "fixtures", "fake_worker.py"), "global-search" })
            info.ArgumentList.Add(arg);
        return WorkerClient.Start(info);
    }

    private sealed class RecordingWorker(WorkerClient inner) : IWorkerRequestClient
    {
        public List<JsonElement> Payloads { get; } = [];

        public Task<JsonElement> RequestAsync(string command, object payload, TimeSpan? timeout = null)
        {
            Payloads.Add(JsonSerializer.SerializeToElement(payload, Protocol.Json));
            return inner.RequestAsync(command, payload, timeout);
        }
    }

    private sealed class FixedResponseWorker(string json) : IWorkerRequestClient
    {
        public Task<JsonElement> RequestAsync(string command, object payload, TimeSpan? timeout = null)
        {
            using var document = JsonDocument.Parse(json);
            return Task.FromResult(document.RootElement.Clone());
        }
    }
}
