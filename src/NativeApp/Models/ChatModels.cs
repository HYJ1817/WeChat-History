using System.Globalization;
using System.Text.Json;
using System.Text.Json.Serialization;
using WechatMemory.NativeApp.Services;

namespace WechatMemory.NativeApp.Models;

public sealed record Conversation(
    string Id,
    string Title,
    int MessageCount,
    string Kind = "direct",
    bool CountKnown = true)
{
    public string CountLabel => CountKnown ? $"{MessageCount} 条消息" : (Kind == "group" ? "微信群聊" : "微信会话");
    public bool IsGroup => Kind == "group";
}

public sealed record ChatMessage(
    string Id,
    string Sender,
    string Timestamp,
    string Content,
    string Kind,
    bool IsOutgoing = false,
    string? AttachmentPath = null,
    int? DurationMs = null,
    string? Transcript = null,
    bool IsContextFocus = false) : MessageProjection
{
    public string TimeLabel => DateTimeOffset.Parse(Timestamp, CultureInfo.InvariantCulture)
        .ToOffset(TimeSpan.FromHours(8)).ToString("yyyy-MM-dd HH:mm:ss", CultureInfo.InvariantCulture);
    public string KindLabel => KindLabelFor(DisplayKind, Kind);
    public bool HasAttachment => !string.IsNullOrWhiteSpace(AttachmentPath) && File.Exists(AttachmentPath);
    public bool HasTranscript => !string.IsNullOrWhiteSpace(Transcript);
    public string AttachmentLabel => HasAttachment ? "打开已恢复图片" : "";
    public string FocusLabel => IsContextFocus ? "搜索命中" : "";
}

public sealed record ConversationResult(List<Conversation> Items, string? SourceSessionId = null);
public sealed record MessageResult(List<ChatMessage> Items, int Total, int? NextOffset);

public sealed record GlobalSearchCriteria(string Query, string? StartDate, string? EndDate, string Order)
{
    public bool IsExecutable => !string.IsNullOrWhiteSpace(Query);
}

public sealed record GlobalSearchHit(
    string HitId,
    string ConversationId,
    string ConversationTitle,
    string ConversationKind,
    string MessageId,
    string Sender,
    string Timestamp,
    string Kind,
    string Snippet,
    IReadOnlyList<string> MatchedFields) : MessageProjection
{
    public string TimeLabel => DateTimeOffset.Parse(Timestamp, CultureInfo.InvariantCulture)
        .ToOffset(TimeSpan.FromHours(8)).ToString("yyyy-MM-dd HH:mm:ss", CultureInfo.InvariantCulture);
    public string KindLabel => KindLabelFor(DisplayKind, Kind);
    public string MatchedFieldsLabel => string.Join("、", MatchedFields.Select(fieldName => fieldName switch
    {
        "sender" => "发送者", "content" => "正文", "kind" => "类型", "transcript" => "转写", _ => fieldName,
    }));
    public Conversation Conversation => new(ConversationId, ConversationTitle, 0, ConversationKind, CountKnown: false);
}

public sealed record GlobalSearchSnapshot(
    string SearchId,
    string SourceSessionId,
    IReadOnlyList<GlobalSearchHit> Items,
    int Total,
    int ResultLimit,
    bool Truncated,
    int? NextOffset,
    JsonElement Coverage)
{
    public string CoverageNotice => GlobalSearchCoverage.Format(Coverage);
    public string Summary => Truncated
        ? $"共 {Total:N0} 条命中；仅开放排序前 {ResultLimit:N0} 条。"
        : $"共 {Total:N0} 条命中。";
}

public sealed record MessageContextResult(
    Conversation Conversation,
    IReadOnlyList<ChatMessage> Items,
    int FocusIndex,
    bool HasEarlier,
    bool HasLater);

public static class GlobalSearchCoverage
{
    public static string Format(JsonElement coverage)
    {
        if (coverage.ValueKind != JsonValueKind.Object) return "";
        int partial = ReadCount(coverage, "partialMessages") ?? ReadCount(coverage, "partialCount") ?? 0;
        bool incomplete = coverage.TryGetProperty("complete", out var complete) && complete.ValueKind == JsonValueKind.False;
        string? note = ReadText(coverage, "warning") ?? ReadText(coverage, "message");
        if (coverage.TryGetProperty("warnings", out var warnings) && warnings.ValueKind == JsonValueKind.Array)
        {
            var first = warnings.EnumerateArray().FirstOrDefault(item => item.ValueKind == JsonValueKind.String);
            if (first.ValueKind == JsonValueKind.String) note ??= first.GetString();
        }
        if (partial > 0 || incomplete)
            return string.IsNullOrWhiteSpace(note)
                ? $"部分复杂消息未完整展开{(partial > 0 ? $"（{partial:N0} 条）" : "")}，搜索覆盖可能不完整。"
                : $"搜索覆盖可能不完整{(partial > 0 ? $"（{partial:N0} 条）" : "")}：{note}";
        return note ?? "";
    }

    private static int? ReadCount(JsonElement element, string name) =>
        element.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.Number &&
        value.TryGetInt32(out int count) && count >= 0 ? count : null;

    private static string? ReadText(JsonElement element, string name) =>
        element.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.String
            ? value.GetString() : null;
}

public sealed record ExportResult(string Path, int MessageCount, long Bytes);
public sealed record ExportDetail(
    string Path,
    int MessageCount,
    long Bytes,
    string Format,
    int PageCount = 0,
    int ChatPageCount = 0,
    int ImagePageCount = 0,
    string? PagesDir = null,
    string? AssetsDir = null);

public sealed record AccountItem(string Token, string Label, string LastUsed, bool Recent)
{
    public string Detail => string.IsNullOrWhiteSpace(LastUsed) ? "" :
        "最近数据 " + DateTimeOffset.Parse(LastUsed, CultureInfo.InvariantCulture)
            .ToOffset(TimeSpan.FromHours(8)).ToString("yyyy-MM-dd HH:mm", CultureInfo.InvariantCulture);
}

public sealed record AccountList(List<AccountItem> Items);
public sealed record ConnectResult(
    bool Connected,
    string? Source,
    string? AccountLabel,
    int ConversationCount,
    int MessageTableCount,
    bool ImageKeyAvailable,
    bool VoiceAvailable,
    string? SourceSessionId = null);

public sealed record OfflineImportResult(
    string Source,
    string Schema,
    int ConversationCount,
    int MessageCount,
    string? SourceSessionId = null);

public sealed record SceneItem(
    string Id,
    string Name,
    string Description = "",
    [property: JsonPropertyName("conversation_id")] string? ConversationId = null,
    int? Days = null,
    string? Start = null,
    string? End = null,
    string? Query = null,
    [property: JsonPropertyName("include_image_pages")] bool IncludeImagePages = true,
    bool Markdown = true,
    [property: JsonPropertyName("json_output")] bool JsonOutput = true,
    string Target = "folder",
    string Prompt = "",
    int? Shortcut = null,
    bool Builtin = false)
{
    public string Label => Days is { } days ? $"{Name} · 最近 {days} 天" : Name;
    public bool IsBuiltin => Builtin;
}

public sealed record SceneList(List<SceneItem> Items, List<string>? Warnings = null);

public sealed record JobSummary(
    string JobId = "",
    string Status = "",
    string? CreatedAt = null,
    string? ConversationName = null,
    int? MessageCount = null,
    string? SceneName = null,
    string? Target = null,
    string? Destination = null,
    string? Directory = null,
    string? Error = null,
    string? UpdatedAt = null,
    string? ConversationId = null,
    string? Detail = null,
    int? Attempts = null,
    bool? Retryable = null,
    List<JobArtifact>? Artifacts = null)
{
    public string StatusLabel => Status switch
    {
        "ready" => "待投递",
        "delivered" => "已投递",
        "copied" => "已复制",
        "expired" => "已过期",
        "failed" => "失败",
        "running" => "执行中",
        "pending" => "等待中",
        _ => string.IsNullOrWhiteSpace(Status) ? "未知状态" : Status,
    };
    public string Summary => $"{(string.IsNullOrWhiteSpace(ConversationName) ? "会话未知" : ConversationName)} · {(MessageCount is >= 0 ? $"{MessageCount} 条" : "消息数未知")} · {StatusLabel}";
    public bool CanRedeliver => Status is "ready" or "failed";
    public string TimeLabel => FormatTime(CreatedAt, "创建时间未知", "MM-dd HH:mm");
    public string CreatedAtDetailLabel => FormatTime(CreatedAt, "创建时间未提供", "yyyy-MM-dd HH:mm:ss");
    public string UpdatedTimeLabel => FormatTime(UpdatedAt, "更新时间未知", "yyyy-MM-dd HH:mm");
    public string UpdatedAtDetailLabel => FormatTime(UpdatedAt, "更新时间未提供", "yyyy-MM-dd HH:mm:ss");

    private static string FormatTime(string? text, string missing, string format) =>
        string.IsNullOrWhiteSpace(text) ? missing :
        DateTimeOffset.TryParse(text, CultureInfo.InvariantCulture, DateTimeStyles.AssumeUniversal, out var value)
            ? value.ToOffset(TimeSpan.FromHours(8)).ToString(format, CultureInfo.InvariantCulture)
            : (text.Length <= 80 ? text : "时间格式未知");
}

public sealed record JobArtifact(string Name, string? Kind = null, long? Size = null, string? Sha256 = null);

public sealed record JobList(List<JobSummary> Items, List<string>? Warnings = null);

/// <summary>Safe, optional-field view of the existing get_job response.</summary>
public sealed record JobDetail(
    JobSummary Job,
    string? SourceLabel = null,
    string? Start = null,
    string? End = null,
    string? Query = null)
{
    public static JobDetail Parse(JsonElement response)
    {
        if (response.ValueKind != JsonValueKind.Object)
            throw new WorkerException("PROTOCOL_ERROR", "任务详情格式不正确。");

        JsonElement summary = Object(response, "job") ?? response;
        JsonElement state = Object(response, "state") ?? default;
        JsonElement manifest = Object(response, "manifest") ?? default;
        JsonElement source = Object(manifest, "source") ?? default;
        JsonElement window = Object(manifest, "window") ?? default;

        string? Text(string name) => ReadText(summary, name) ?? ReadText(response, name);
        int? Count(string name) => ReadCount(summary, name) ?? ReadCount(response, name);
        string? stateText(string name) => ReadText(state, name);
        int? stateCount(string name) => ReadCount(state, name);

        var job = new JobSummary(
            JobId: Text("jobId") ?? "",
            Status: Text("status") ?? stateText("status") ?? "",
            CreatedAt: Text("createdAt"),
            ConversationName: Text("conversationName") ?? ReadText(Object(manifest, "conversation") ?? default, "name"),
            MessageCount: Count("messageCount"),
            SceneName: Text("sceneName") ?? ReadText(Object(manifest, "scene") ?? default, "name"),
            Target: Text("target") ?? stateText("target"),
            Destination: Text("destination") ?? stateText("destination"),
            Directory: Text("directory"),
            Error: Text("error") ?? stateText("error"),
            UpdatedAt: Text("updatedAt") ?? stateText("updatedAt"),
            ConversationId: Text("conversationId") ?? ReadText(Object(manifest, "conversation") ?? default, "id"),
            Detail: Text("detail") ?? stateText("detail"),
            Attempts: Count("attempts") ?? stateCount("attempts"),
            Retryable: ReadBoolean(summary, "retryable") ?? ReadBoolean(state, "retryable") ?? ReadBoolean(response, "retryable"),
            Artifacts: ReadArtifacts(summary, response));

        if (string.IsNullOrWhiteSpace(job.JobId))
            throw new WorkerException("PROTOCOL_ERROR", "任务详情缺少有效的任务标识。");

        return new JobDetail(
            job,
            ReadText(source, "label"),
            ReadText(window, "start"),
            ReadText(window, "end"),
            ReadText(window, "query"));
    }

    private static JsonElement? Object(JsonElement element, string name) =>
        element.ValueKind == JsonValueKind.Object && element.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.Object
            ? value : null;

    private static string? ReadText(JsonElement element, string name) =>
        element.ValueKind == JsonValueKind.Object && element.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.String
            ? value.GetString() : null;

    private static int? ReadCount(JsonElement element, string name) =>
        element.ValueKind == JsonValueKind.Object && element.TryGetProperty(name, out var value) &&
        value.ValueKind == JsonValueKind.Number && value.TryGetInt32(out int count) && count >= 0
            ? count : null;

    private static bool? ReadBoolean(JsonElement element, string name)
    {
        if (element.ValueKind != JsonValueKind.Object || !element.TryGetProperty(name, out var value))
            return null;
        return value.ValueKind switch
        {
            JsonValueKind.True => true,
            JsonValueKind.False => false,
            _ => null,
        };
    }

    private static List<JobArtifact>? ReadArtifacts(JsonElement summary, JsonElement response)
    {
        JsonElement owner = summary.ValueKind == JsonValueKind.Object && summary.TryGetProperty("artifacts", out _)
            ? summary : response;
        if (owner.ValueKind != JsonValueKind.Object || !owner.TryGetProperty("artifacts", out var items) || items.ValueKind != JsonValueKind.Array)
            return null;

        var result = new List<JobArtifact>();
        foreach (var item in items.EnumerateArray())
        {
            if (item.ValueKind != JsonValueKind.Object || !item.TryGetProperty("name", out var name) || name.ValueKind != JsonValueKind.String)
                return null;
            string? artifactName = name.GetString();
            if (string.IsNullOrWhiteSpace(artifactName)) return null;
            string? kind = ReadText(item, "kind");
            long? size = item.TryGetProperty("size", out var sizeElement) && sizeElement.ValueKind == JsonValueKind.Number &&
                sizeElement.TryGetInt64(out long bytes) && bytes >= 0 ? bytes : null;
            result.Add(new JobArtifact(artifactName, kind, size, ReadText(item, "sha256")));
        }
        return result;
    }
}

public sealed record PrunePreview(
    int Days,
    string Cutoff,
    int CandidateCount,
    long KnownArtifactBytes,
    int UnknownSizeCount,
    Dictionary<string, int> SkippedReasonCounts,
    string PreviewToken)
{
    public bool HasCandidates => CandidateCount > 0;

    public static PrunePreview Parse(JsonElement response)
    {
        if (response.ValueKind != JsonValueKind.Object || !ReadBoolean(response, "dryRun", expected: true) ||
            !ReadInt(response, "days", out int days) || days < 1 ||
            !ReadText(response, "cutoff", out string cutoff) ||
            !ReadInt(response, "candidateCount", out int candidates) || candidates < 0 ||
            !ReadLong(response, "knownArtifactBytes", out long bytes) || bytes < 0 ||
            !ReadInt(response, "unknownSizeCount", out int unknown) || unknown < 0 ||
            !ReadText(response, "previewToken", out string token))
            throw new WorkerException("PROTOCOL_ERROR", "清理预览结果格式不正确。");
        return new PrunePreview(days, cutoff, candidates, bytes, unknown,
            ReadReasonCounts(response), token);
    }

    public static string FormatReasonCounts(IReadOnlyDictionary<string, int> counts)
    {
        static string Label(string reason) => reason switch
        {
            "unfinished" => "未完成或可恢复",
            "not_old" => "未满足保留期限",
            "recent_write" => "近期仍有写入",
            "changed" => "检测到预览后变化",
            "duplicate_id" => "任务标识重复",
            "unsafe_path" => "路径受保护",
            "unreadable" => "无法安全读取",
            "unreadable_metadata" => "元数据不可读",
            "unsupported_schema" => "格式不受支持",
            "not_found" => "任务已不存在",
            "in_use" => "文件正在使用中",
            "protection_failed" => "无法建立安全删除保护",
            "safe_delete_unavailable" => "当前卷不支持安全删除（仅支持 Windows 本地固定 NTFS）",
            "delete_failed" => "删除过程中失败（可能已删除部分文件）",
            "other" => "其他原因",
            "unknown" => "原因未知",
            _ => $"其他保护原因（{reason}）",
        };
        return string.Join("、", counts.Where(item => item.Value > 0)
            .OrderBy(item => item.Key, StringComparer.Ordinal)
            .Select(item => $"{Label(item.Key)} {item.Value:N0} 项"));
    }

    internal static Dictionary<string, int> ReadReasonCounts(JsonElement response)
    {
        var result = new Dictionary<string, int>(StringComparer.Ordinal);
        if (response.ValueKind != JsonValueKind.Object || !response.TryGetProperty("skippedReasonCounts", out var counts) || counts.ValueKind != JsonValueKind.Object)
            return result;
        foreach (var property in counts.EnumerateObject())
            if (property.Value.ValueKind == JsonValueKind.Number && property.Value.TryGetInt32(out int count) && count >= 0)
                result[property.Name] = count;
        return result;
    }

    internal static bool ReadInt(JsonElement element, string name, out int result)
    {
        result = 0;
        return element.ValueKind == JsonValueKind.Object && element.TryGetProperty(name, out var value) &&
            value.ValueKind == JsonValueKind.Number && value.TryGetInt32(out result);
    }

    internal static bool ReadLong(JsonElement element, string name, out long result)
    {
        result = 0;
        return element.ValueKind == JsonValueKind.Object && element.TryGetProperty(name, out var value) &&
            value.ValueKind == JsonValueKind.Number && value.TryGetInt64(out result);
    }

    internal static bool ReadText(JsonElement element, string name, out string result)
    {
        result = "";
        if (element.ValueKind != JsonValueKind.Object || !element.TryGetProperty(name, out var value) || value.ValueKind != JsonValueKind.String)
            return false;
        result = value.GetString() ?? "";
        return !string.IsNullOrWhiteSpace(result);
    }

    internal static bool ReadBoolean(JsonElement element, string name, bool expected) =>
        element.ValueKind == JsonValueKind.Object && element.TryGetProperty(name, out var value) &&
        (expected ? value.ValueKind == JsonValueKind.True : value.ValueKind == JsonValueKind.False);
}

public sealed record PruneResult(
    int Days,
    string Cutoff,
    int RemovedCount,
    int SkippedCount,
    Dictionary<string, int> SkippedReasonCounts)
{
    public static PruneResult Parse(JsonElement response)
    {
        if (response.ValueKind != JsonValueKind.Object || !PrunePreview.ReadBoolean(response, "dryRun", expected: false) ||
            !PrunePreview.ReadInt(response, "days", out int days) || days < 1 ||
            !PrunePreview.ReadText(response, "cutoff", out string cutoff) ||
            !PrunePreview.ReadInt(response, "removedCount", out int removed) || removed < 0 ||
            !PrunePreview.ReadInt(response, "skippedCount", out int skipped) || skipped < 0)
            throw new WorkerException("PROTOCOL_ERROR", "清理执行结果格式不正确。");
        return new PruneResult(days, cutoff, removed, skipped, PrunePreview.ReadReasonCounts(response));
    }
}

public sealed record ArtifactPath(string Path);
public sealed record VoiceStatus(string Model, bool Installed, bool ComponentAvailable, bool RequiresDownload, bool ConnectedVoice)
{
    public string Summary => !ComponentAvailable ? "缺少语音组件" :
        ConnectedVoice ? (Installed ? "语音模型已就绪 · 已连接语音" : "可转写 · 首次需下载模型") :
        (Installed ? "语音模型已就绪 · 连接时未启用语音" : "语音未启用");
}
public sealed record PrepareModelResult(bool Ready, string Model);
public sealed record TranscribeResult(int Total, int Transcribed);
public sealed record DisconnectResult(bool Connected);

/// <summary>
/// A scene window resolved to the calendar dates the native UI can show.
/// Explicit start/end values win over <see cref="SceneItem.Days"/>, matching
/// <c>Scene.window()</c> in the worker. A trailing-days window is interpreted as
/// the last N calendar days in UTC+08:00, ending today (inclusive), which is how
/// this app has always shown the built-in scenes.
/// </summary>
public sealed record SceneWindow(DateOnly? Start, DateOnly? End)
{
    public static readonly SceneWindow Unbounded = new(null, null);

    public DateTimeOffset? StartDate => Start is { } value
        ? new DateTimeOffset(value.ToDateTime(TimeOnly.MinValue), DateRange.Offset)
        : null;

    public DateTimeOffset? EndDate => End is { } value
        ? new DateTimeOffset(value.ToDateTime(TimeOnly.MinValue), DateRange.Offset)
        : null;

    public DateRange ToDateRange() => DateRange.Create(StartDate, EndDate);
}

public static class SceneWindowResolver
{
    /// <summary>
    /// Resolves the date pickers for a scene without touching the UI. Returns
    /// false with a user-safe Chinese message when the stored scene is invalid.
    /// </summary>
    public static bool TryResolve(SceneItem scene, DateTimeOffset now, out SceneWindow window, out string error)
    {
        window = SceneWindow.Unbounded;
        error = "";
        if (!string.IsNullOrWhiteSpace(scene.Start) || !string.IsNullOrWhiteSpace(scene.End))
        {
            if (!TryParseDate(scene.Start, out var start))
            {
                error = "场景的开始日期无效。";
                return false;
            }
            if (!TryParseDate(scene.End, out var end))
            {
                error = "场景的结束日期无效。";
                return false;
            }
            if (start is { } first && end is { } last && first > last)
            {
                error = "场景的开始日期不能晚于结束日期。";
                return false;
            }
            window = new(start, end);
            return true;
        }
        if (scene.Days is { } days)
        {
            if (days < 1)
            {
                error = "场景的天数必须至少为 1。";
                return false;
            }
            var today = DateOnly.FromDateTime(now.ToOffset(DateRange.Offset).DateTime);
            window = new(today.AddDays(-(days - 1)), today);
        }
        return true;
    }

    private static bool TryParseDate(string? text, out DateOnly? date)
    {
        date = null;
        if (string.IsNullOrWhiteSpace(text)) return true;
        if (DateTimeOffset.TryParse(text, CultureInfo.InvariantCulture, DateTimeStyles.AllowWhiteSpaces, out var parsed))
        {
            // Scene windows are wall-clock values; use the clock as written, not
            // the machine's local zone. Date-only values also land on this date.
            date = DateOnly.FromDateTime(parsed.DateTime);
            return true;
        }
        return false;
    }
}

/// <summary>
/// The <c>scene</c> object sent to <c>save_scene</c>. Field names follow the
/// vendored worker's <c>Scene.from_dict</c> schema, including its snake_case
/// names; the UI only ever edits the window, query and output flags.
/// </summary>
public sealed record SceneSavePayload
{
    [JsonPropertyName("id")] public string Id { get; init; } = "";
    [JsonPropertyName("name")] public string Name { get; init; } = "";
    [JsonPropertyName("description")] public string Description { get; init; } = "";
    [JsonPropertyName("conversation_id")] public string? ConversationId { get; init; }
    [JsonPropertyName("days")] public int? Days { get; init; }
    [JsonPropertyName("start")] public string? Start { get; init; }
    [JsonPropertyName("end")] public string? End { get; init; }
    [JsonPropertyName("query")] public string? Query { get; init; }
    [JsonPropertyName("include_image_pages")] public bool IncludeImagePages { get; init; } = true;
    [JsonPropertyName("markdown")] public bool Markdown { get; init; } = true;
    [JsonPropertyName("json_output")] public bool JsonOutput { get; init; } = true;
    [JsonPropertyName("target")] public string Target { get; init; } = "folder";
    [JsonPropertyName("prompt")] public string Prompt { get; init; } = "";
}

public static class SceneDraft
{
    /// <summary>
    /// Builds the save payload from the filters currently shown in the UI. A
    /// concrete date selection becomes an explicit start/end window; an open
    /// range leaves all three window fields null (whole conversation).
    /// </summary>
    public static SceneSavePayload Create(
        string id,
        string name,
        DateTimeOffset? start,
        DateTimeOffset? end,
        string? query,
        bool includeImagePages,
        bool markdown,
        bool jsonOutput,
        string target,
        string prompt,
        string description = "由当前筛选保存")
    {
        if (string.IsNullOrWhiteSpace(id))
            throw new WorkerException("INVALID_ARGUMENT", "场景标识不能为空。");
        if (string.IsNullOrWhiteSpace(name))
            throw new WorkerException("INVALID_ARGUMENT", "场景名称不能为空。");
        var range = DateRange.Create(start, end);
        return new SceneSavePayload
        {
            Id = id,
            Name = name,
            Description = description,
            ConversationId = null,
            Days = null,
            Start = range.StartDate,
            End = range.EndDate,
            Query = query,
            IncludeImagePages = includeImagePages,
            Markdown = markdown,
            JsonOutput = jsonOutput,
            Target = target,
            Prompt = prompt,
        };
    }

    public static string NewId(DateTimeOffset now) =>
        "custom-" + now.UtcDateTime.ToString("yyyyMMddHHmmssfff", CultureInfo.InvariantCulture);
}

/// <summary>
/// The <c>delivery</c> object returned by <c>deliver_job</c>. A failed copy is
/// still an <c>ok: true</c> protocol response, so callers must inspect this
/// object instead of treating the response itself as success.
/// </summary>
public sealed record DeliveryOutcome(
    string Status,
    string? Code = null,
    string? Error = null,
    string? Detail = null,
    string? Destination = null)
{
    public bool Succeeded => Status is "delivered" or "copied" or "none";
    public bool Failed => Status == "failed";
    public string FailureMessage => !string.IsNullOrWhiteSpace(Error) ? Error! :
        !string.IsNullOrWhiteSpace(Detail) ? Detail! : "投递失败，任务已保留，可稍后重试。";

    public static DeliveryOutcome Parse(JsonElement response)
    {
        if (!response.TryGetProperty("delivery", out var delivery) ||
            delivery.ValueKind != JsonValueKind.Object ||
            !delivery.TryGetProperty("status", out var statusElement) ||
            statusElement.ValueKind != JsonValueKind.String)
            throw new WorkerException("PROTOCOL_ERROR", "投递结果格式不正确。");
        var status = statusElement.GetString() ?? "";
        if (status is not ("delivered" or "copied" or "failed" or "none"))
            throw new WorkerException("PROTOCOL_ERROR", "投递结果格式不正确。");
        return new DeliveryOutcome(
            status,
            Text(delivery, "code"),
            Text(delivery, "error"),
            Text(delivery, "detail"),
            Text(delivery, "destination"));
    }

    public void ThrowIfFailed()
    {
        if (!Failed) return;
        var code = string.IsNullOrWhiteSpace(Code) ? "DELIVERY_FAILED" : Code!;
        throw new WorkerException(code, FailureMessage);
    }

    private static string? Text(JsonElement element, string property) =>
        element.TryGetProperty(property, out var value) && value.ValueKind == JsonValueKind.String
            ? value.GetString()
            : null;
}

/// <summary>
/// Media capabilities captured by the last successful <c>connect</c>. The
/// worker builds its image/voice caches only during connect, so enabling an
/// option afterwards cannot change the existing source; the UI must reconnect
/// (with the user's confirmation) instead of pretending the option took effect.
/// </summary>
public sealed record ConnectionMedia(
    bool ImagesRequested,
    bool ImageKeyAvailable,
    bool VoiceRequested,
    bool VoiceAvailable)
{
    public bool ImagesAvailable => ImagesRequested && ImageKeyAvailable;
    public bool VoiceReady => VoiceRequested && VoiceAvailable;
    public bool NeedsImageReconnect(bool enableImages) => enableImages && !ImagesAvailable;
    public bool NeedsVoiceReconnect(bool enableVoice) => enableVoice && !VoiceReady;
}
