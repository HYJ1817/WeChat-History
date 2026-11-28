using System.Text.Json;
using WechatMemory.NativeApp.Models;
using WechatMemory.NativeApp.Services;
using Xunit;

namespace NativeApp.Tests;

public sealed class TaskHistoryTests
{
    [Fact]
    public void JobDetailUsesOnlyFieldsReturnedByGetJobAndArtifacts()
    {
        const string json = """
            {
              "job": {
                "jobId": "job-123",
                "status": "failed",
                "createdAt": "2026-11-20T10:00:00Z",
                "updatedAt": "2026-11-21T11:30:00Z",
                "conversationId": "conv-1",
                "conversationName": "合成会话",
                "messageCount": 7,
                "sceneName": "周报",
                "target": "folder",
                "destination": "C:/synthetic/delivery",
                "detail": "合成说明",
                "error": "合成错误",
                "attempts": 2,
                "artifacts": [{"name":"chat.md","kind":"markdown","size":120,"sha256":"abc"}]
              },
              "manifest": {
                "source": {"label":"合成来源"},
                "window": {"start":"2026-11-20T00:00:00Z","end":null,"query":"合成关键词"}
              },
              "state": {"status":"failed","retryable":true}
            }
            """;
        using var document = JsonDocument.Parse(json);

        var detail = JobDetail.Parse(document.RootElement);

        Assert.Equal("job-123", detail.Job.JobId);
        Assert.Equal("失败", detail.Job.StatusLabel);
        Assert.Equal(7, detail.Job.MessageCount!.Value);
        Assert.Equal("2026-11-21T11:30:00Z", detail.Job.UpdatedAt);
        Assert.Equal("folder", detail.Job.Target);
        Assert.Equal("合成错误", detail.Job.Error);
        Assert.Equal(2, detail.Job.Attempts!.Value);
        Assert.True(detail.Job.Retryable!.Value);
        Assert.Equal("合成来源", detail.SourceLabel);
        Assert.Equal("2026-11-20T00:00:00Z", detail.Start);
        Assert.Equal("合成关键词", detail.Query);
        Assert.Equal("chat.md", Assert.Single(detail.Job.Artifacts!).Name);
        Assert.Equal(120L, detail.Job.Artifacts![0].Size!.Value);
    }

    [Fact]
    public void JobDetailLeavesMissingOrMalformedValuesUnknown()
    {
        using var document = JsonDocument.Parse("""
            {"job":{"jobId":"job-safe","status":17,"messageCount":-1,"artifacts":[{"name":false}]},"state":{}}
            """);

        var detail = JobDetail.Parse(document.RootElement);

        Assert.Equal("job-safe", detail.Job.JobId);
        Assert.Equal("未知状态", detail.Job.StatusLabel);
        Assert.Null(detail.Job.MessageCount);
        Assert.Null(detail.Job.Retryable);
        Assert.Null(detail.Job.Artifacts);
        Assert.Equal("创建时间未提供", detail.Job.CreatedAtDetailLabel);
    }

    [Fact]
    public void PruneResponsesParseBoundedSummaryAndToken()
    {
        using var previewJson = JsonDocument.Parse("""
            {"dryRun":true,"days":30,"cutoff":"2026-10-01T00:00:00Z","candidateCount":12,
             "knownArtifactBytes":4096,"unknownSizeCount":2,"skippedReasonCounts":{"in_use":1,"safe_delete_unavailable":1},
             "previewToken":"short-process-local-token"}
            """);
        using var resultJson = JsonDocument.Parse("""
            {"dryRun":false,"days":30,"cutoff":"2026-10-01T00:00:00Z","removedCount":10,
             "skippedCount":2,"skippedReasonCounts":{"protection_failed":2}}
            """);

        var preview = PrunePreview.Parse(previewJson.RootElement);
        var result = PruneResult.Parse(resultJson.RootElement);

        Assert.Equal(12, preview.CandidateCount);
        Assert.Equal(4096L, preview.KnownArtifactBytes);
        Assert.Equal(2, preview.UnknownSizeCount);
        Assert.False(string.IsNullOrWhiteSpace(preview.PreviewToken));
        Assert.Equal(10, result.RemovedCount);
        Assert.Equal(2, result.SkippedCount);
        Assert.Equal(2, result.SkippedReasonCounts["protection_failed"]);
        string labels = PrunePreview.FormatReasonCounts(new Dictionary<string, int>
        {
            ["in_use"] = 1,
            ["protection_failed"] = 1,
            ["safe_delete_unavailable"] = 1,
        });
        Assert.Contains("文件正在使用中", labels);
        Assert.Contains("无法建立安全删除保护", labels);
        Assert.Contains("当前卷不支持安全删除", labels);
    }

    [Fact]
    public void ConfirmRequestCarriesOnlyDaysAndTokenNotSnapshot()
    {
        var wire = Protocol.Request("confirm-1", "confirm_prune_jobs", new
        {
            days = 90,
            previewToken = "process-local-token",
        });
        using var document = JsonDocument.Parse(wire);
        var request = document.RootElement;

        Assert.Equal("confirm_prune_jobs", request.GetProperty("command").GetString());
        Assert.Equal(90, request.GetProperty("payload").GetProperty("days").GetInt32());
        Assert.Equal("process-local-token", request.GetProperty("payload").GetProperty("previewToken").GetString());
        Assert.False(request.GetProperty("payload").TryGetProperty("snapshot", out _));
        Assert.True(System.Text.Encoding.UTF8.GetByteCount(wire) < 1024);
    }

    [Fact]
    public async Task ReversedImportFiltersFailBeforeImportRequestIsStarted()
    {
        int calls = 0;
        var start = new DateTimeOffset(2026, 11, 25, 0, 0, 0, TimeSpan.FromHours(8));
        var end = new DateTimeOffset(2026, 11, 24, 0, 0, 0, TimeSpan.FromHours(8));

        var error = await Assert.ThrowsAsync<WorkerException>(() => OfflineImportPreflight.ExecuteAsync(
            start,
            end,
            () =>
            {
                calls++;
                return Task.FromResult(true);
            }));

        Assert.Equal("INVALID_ARGUMENT", error.Code);
        Assert.Equal(0, calls);
    }

    [Fact]
    public async Task ValidImportFiltersReachImportRequestOnce()
    {
        int calls = 0;
        var start = new DateTimeOffset(2026, 11, 24, 0, 0, 0, TimeSpan.FromHours(8));
        var end = new DateTimeOffset(2026, 11, 24, 23, 0, 0, TimeSpan.FromHours(8));

        bool result = await OfflineImportPreflight.ExecuteAsync(start, end, () =>
        {
            calls++;
            return Task.FromResult(true);
        });

        Assert.True(result);
        Assert.Equal(1, calls);
    }

    [Fact]
    public void JobDetailForAnotherRequestedIdIsRejectedBeforeReturningCandidate()
    {
        using var response = JsonDocument.Parse("""
            {"job":{"jobId":"job-B","directory":"C:/synthetic/job-B"}}
            """);

        var error = Assert.Throws<WorkerException>(() =>
            TaskHistoryResponseValidator.ParseJobDetail("job-A", response.RootElement));

        Assert.Equal("PROTOCOL_ERROR", error.Code);
        Assert.Contains("其他任务", error.Message);
    }

    [Fact]
    public void PrunePreviewForDifferentDaysIsRejectedBeforeReturningCandidate()
    {
        using var response = JsonDocument.Parse("""
            {"dryRun":true,"days":30,"cutoff":"2026-10-01T00:00:00Z","candidateCount":1,
             "knownArtifactBytes":0,"unknownSizeCount":0,"previewToken":"synthetic-token"}
            """);

        var error = Assert.Throws<WorkerException>(() =>
            TaskHistoryResponseValidator.ParsePrunePreview(90, response.RootElement));

        Assert.Equal("PROTOCOL_ERROR", error.Code);
        Assert.Contains("天数不匹配", error.Message);
    }

    [Fact]
    public void CleanupWorkflowGateBlocksGlobalOperationsUntilLeaseIsReleased()
    {
        var gate = new OperationGate();
        Assert.True(gate.CanReconnect(isBusy: false, closing: false));
        Assert.True(gate.CanImportJson(isBusy: false, closing: false, workerAvailable: true));
        Assert.True(gate.CanExport(isBusy: false, closing: false, connected: true, hasConversation: true, filtersClean: true));
        Assert.True(gate.CanRetry(isBusy: false, closing: false, workerAvailable: true, retryable: true));

        var waitingForConfirmation = gate.TryEnter();
        Assert.NotNull(waitingForConfirmation);
        // The modal wait spans independent dispatcher turns; the gate itself must
        // keep commands disabled even if a transient busy flag were cleared.
        Assert.False(gate.CanReconnect(isBusy: false, closing: false));
        Assert.False(gate.CanImportJson(isBusy: false, closing: false, workerAvailable: true));
        Assert.False(gate.CanExport(isBusy: false, closing: false, connected: true, hasConversation: true, filtersClean: true));
        Assert.False(gate.CanRetry(isBusy: false, closing: false, workerAvailable: true, retryable: true));
        Assert.Null(gate.TryEnter());

        waitingForConfirmation.Dispose(); // Cancel/close path releases the scope.
        Assert.True(gate.CanReconnect(isBusy: false, closing: false));
        Assert.True(gate.CanImportJson(isBusy: false, closing: false, workerAvailable: true));
        Assert.True(gate.CanExport(isBusy: false, closing: false, connected: true, hasConversation: true, filtersClean: true));
        Assert.True(gate.CanRetry(isBusy: false, closing: false, workerAvailable: true, retryable: true));
    }

    [Fact]
    public void DeleteFailureProtocolSummaryWarnsThatFilesMayAlreadyBeDeleted()
    {
        string fixturePath = Path.Combine(AppContext.BaseDirectory, "fixtures", "partial_prune_result.json");
        using var response = JsonDocument.Parse(File.ReadAllText(fixturePath));

        var result = PruneResult.Parse(response.RootElement);
        var presentation = PruneResultPresentation.Create(result);

        Assert.Equal(0, result.RemovedCount);
        Assert.Equal(1, result.SkippedCount);
        Assert.True(presentation.MayHavePartiallyDeletedFiles);
        Assert.Equal("清理未完成，请核实", presentation.Title);
        Assert.Contains("完整清理 0 项", presentation.Summary);
        Assert.Contains("可能已有部分文件被永久删除", presentation.Summary);
        Assert.DoesNotContain("未删除任何任务", presentation.Summary);
        Assert.DoesNotContain("没有任务被清理", presentation.Summary);
    }

    [Fact]
    public void ProtectiveSkipsAndNormalZeroResultAreNotReportedAsSuccessOrPartialDelete()
    {
        using var skippedResponse = JsonDocument.Parse("""
            {"dryRun":false,"days":30,"cutoff":"2026-10-01T00:00:00Z","removedCount":0,
             "skippedCount":2,"skippedReasonCounts":{"protection_failed":1,"safe_delete_unavailable":1}}
            """);
        using var emptyResponse = JsonDocument.Parse("""
            {"dryRun":false,"days":30,"cutoff":"2026-10-01T00:00:00Z","removedCount":0,
             "skippedCount":0,"skippedReasonCounts":{}}
            """);

        var skipped = PruneResultPresentation.Create(PruneResult.Parse(skippedResponse.RootElement));
        var empty = PruneResultPresentation.Create(PruneResult.Parse(emptyResponse.RootElement));

        Assert.Equal("没有任务完整清理", skipped.Title);
        Assert.False(skipped.MayHavePartiallyDeletedFiles);
        Assert.Contains("完整清理 0 项；未完整清理或跳过 2 项", skipped.Summary);
        Assert.DoesNotContain("部分文件可能已永久删除", skipped.Summary);
        Assert.NotEqual("清理完成", skipped.Title);

        Assert.Equal("本次没有任务完整清理", empty.Title);
        Assert.False(empty.MayHavePartiallyDeletedFiles);
        Assert.Equal("完整清理 0 项。", empty.Summary);
    }

    [Fact]
    public void LateDisconnectGuardOnlyProtectsTheWorkerWithUnknownPruneOutcome()
    {
        var guard = new DisconnectStatusGuard();
        var uncertainWorker = new object();
        var differentWorker = new object();

        guard.Preserve(uncertainWorker);

        Assert.True(guard.ShouldPreserve(uncertainWorker));
        Assert.False(guard.ShouldPreserve(differentWorker));
        guard.Clear(differentWorker);
        Assert.True(guard.ShouldPreserve(uncertainWorker));
        guard.Clear(uncertainWorker);
        Assert.False(guard.ShouldPreserve(uncertainWorker));
    }
}
