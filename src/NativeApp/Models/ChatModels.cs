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
    string? Transcript = null)
{
    public string TimeLabel => DateTimeOffset.Parse(Timestamp, CultureInfo.InvariantCulture)
        .ToOffset(TimeSpan.FromHours(8)).ToString("yyyy-MM-dd HH:mm:ss", CultureInfo.InvariantCulture);
    public string KindLabel => Kind switch
    {
        "image" => "图片",
        "file" => "文件",
        "voice" => "语音",
        "system" => "系统",
        _ => "文本",
    };
    public bool HasAttachment => !string.IsNullOrWhiteSpace(AttachmentPath) && File.Exists(AttachmentPath);
    public bool HasTranscript => !string.IsNullOrWhiteSpace(Transcript);
    public string AttachmentLabel => HasAttachment ? "打开已恢复图片" : "";
}

public sealed record ConversationResult(List<Conversation> Items);
public sealed record MessageResult(List<ChatMessage> Items, int Total, int? NextOffset);
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
    bool VoiceAvailable);

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
    string JobId,
    string Status,
    string CreatedAt,
    string ConversationName,
    int MessageCount,
    string? SceneName = null,
    string? Target = null,
    string? Destination = null,
    string? Directory = null,
    string? Error = null)
{
    public string StatusLabel => Status switch
    {
        "ready" => "待投递",
        "delivered" => "已投递",
        "copied" => "已复制",
        "failed" => "失败",
        "running" => "执行中",
        "pending" => "等待中",
        _ => Status,
    };
    public string Summary => $"{ConversationName} · {MessageCount} 条 · {StatusLabel}";
    public bool CanRedeliver => Status is "ready" or "failed";
    public string TimeLabel => DateTimeOffset.TryParse(CreatedAt, CultureInfo.InvariantCulture, DateTimeStyles.AssumeUniversal, out var value)
        ? value.ToOffset(TimeSpan.FromHours(8)).ToString("MM-dd HH:mm", CultureInfo.InvariantCulture) : CreatedAt;
}

public sealed record JobList(List<JobSummary> Items, List<string>? Warnings = null);
public sealed record JobDetail(JobSummary Job, string? Prompt = null);
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
