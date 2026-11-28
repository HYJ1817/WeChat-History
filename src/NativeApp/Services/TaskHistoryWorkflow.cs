using System.Text.Json;
using WechatMemory.NativeApp.Models;

namespace WechatMemory.NativeApp.Services;

public static class TaskHistoryResponseValidator
{
    public static JobDetail ParseJobDetail(string requestedJobId, JsonElement response)
    {
        if (string.IsNullOrWhiteSpace(requestedJobId))
            throw new WorkerException("INVALID_ARGUMENT", "任务标识无效。");
        var candidate = JobDetail.Parse(response);
        if (!string.Equals(candidate.Job.JobId, requestedJobId, StringComparison.Ordinal))
            throw new WorkerException("PROTOCOL_ERROR", "工作进程返回了其他任务的详情。");
        return candidate;
    }

    public static PrunePreview ParsePrunePreview(int requestedDays, JsonElement response)
    {
        var candidate = PrunePreview.Parse(response);
        if (candidate.Days != requestedDays)
            throw new WorkerException("PROTOCOL_ERROR", "清理预览的保留天数不匹配。");
        return candidate;
    }

    public static PruneResult ParsePruneResult(int requestedDays, JsonElement response)
    {
        var candidate = PruneResult.Parse(response);
        if (candidate.Days != requestedDays)
            throw new WorkerException("PROTOCOL_ERROR", "清理执行结果的保留天数不匹配。");
        return candidate;
    }
}

public sealed record PruneResultPresentation(
    string Title,
    string Summary,
    string Status,
    bool MayHavePartiallyDeletedFiles)
{
    public static PruneResultPresentation Create(PruneResult result)
    {
        ArgumentNullException.ThrowIfNull(result);
        bool deleteFailed = result.SkippedReasonCounts.TryGetValue("delete_failed", out int failedCount) && failedCount > 0;
        string reasonSummary = PrunePreview.FormatReasonCounts(result.SkippedReasonCounts);

        if (deleteFailed)
        {
            string summary = $"完整清理 {result.RemovedCount:N0} 项；未完整清理或跳过 {result.SkippedCount:N0} 项。删除失败条目可能已有部分文件被永久删除，请刷新任务历史核实。";
            if (reasonSummary.Length > 0) summary += "\n原因摘要：" + reasonSummary;
            return new(
                "清理未完成，请核实",
                summary,
                $"清理未完成：已完整清理 {result.RemovedCount:N0} 项；另有 {result.SkippedCount:N0} 项未完整清理或跳过，部分文件可能已永久删除。请刷新任务历史核实。",
                MayHavePartiallyDeletedFiles: true);
        }

        if (result.SkippedCount > 0)
        {
            string summary = $"完整清理 {result.RemovedCount:N0} 项；未完整清理或跳过 {result.SkippedCount:N0} 项。";
            if (reasonSummary.Length > 0) summary += "\n跳过原因：" + reasonSummary;
            return new(
                result.RemovedCount > 0 ? "清理部分完成" : "没有任务完整清理",
                summary,
                $"清理结束：完整清理 {result.RemovedCount:N0} 项；另有 {result.SkippedCount:N0} 项未完整清理或跳过。",
                MayHavePartiallyDeletedFiles: false);
        }

        if (result.RemovedCount == 0)
        {
            return new(
                "本次没有任务完整清理",
                "完整清理 0 项。",
                "清理结束：本次没有任务完整清理。",
                MayHavePartiallyDeletedFiles: false);
        }

        return new(
            "清理完成",
            $"完整清理 {result.RemovedCount:N0} 项。",
            $"清理结束：已完整清理 {result.RemovedCount:N0} 项。",
            MayHavePartiallyDeletedFiles: false);
    }
}
