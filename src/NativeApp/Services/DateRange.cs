using System.Globalization;

namespace WechatMemory.NativeApp.Services;

// Demo records always use UTC+08:00; never infer a date from the PC's time zone.
public sealed record DateRange(string? StartDate, string? EndDate)
{
    public static readonly TimeSpan Offset = TimeSpan.FromHours(8);
    public static DateRange Create(DateTimeOffset? start, DateTimeOffset? end)
    {
        if (start.HasValue && end.HasValue && start.Value.Date > end.Value.Date)
            throw new WorkerException("INVALID_ARGUMENT", "开始日期不能晚于结束日期。");
        // Restrict the end so adding one day is valid on both runtimes.
        if (end?.Year >= 9999)
            throw new WorkerException("INVALID_ARGUMENT", "结束日期必须早于 9999 年。");
        return new(start?.ToString("yyyy-MM-dd", CultureInfo.InvariantCulture),
                   end?.ToString("yyyy-MM-dd", CultureInfo.InvariantCulture));
    }

    public (DateTimeOffset? Start, DateTimeOffset? EndExclusive) Bounds() => (
        StartDate is null ? null : new DateTimeOffset(DateTime.ParseExact(StartDate, "yyyy-MM-dd", CultureInfo.InvariantCulture), Offset),
        EndDate is null ? null : new DateTimeOffset(DateTime.ParseExact(EndDate, "yyyy-MM-dd", CultureInfo.InvariantCulture).AddDays(1), Offset));

    public string Label => StartDate is null && EndDate is null ? "全部日期 · UTC+08:00" :
        $"{StartDate ?? "不限"} 至 {EndDate ?? "不限"}（含首尾日期）· UTC+08:00";
}

/// <summary>
/// Validates the filters a newly imported source will immediately use before
/// running an operation with source-switching side effects.
/// </summary>
public static class OfflineImportPreflight
{
    public static async Task<T> ExecuteAsync<T>(
        DateTimeOffset? start,
        DateTimeOffset? end,
        Func<Task<T>> import)
    {
        ArgumentNullException.ThrowIfNull(import);
        _ = DateRange.Create(start, end);
        return await import();
    }
}
