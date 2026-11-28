using System.Text.Json.Serialization;

namespace WechatMemory.NativeApp.Models;

/// <summary>
/// Optional, row-bound presentation supplied by the worker, never parsed from Content
/// or looked up by a public message ID. Missing fields keep the plain-text UI intact.
/// Names mirror the parser projection in the camelCase stdio protocol.
/// </summary>
public record MessageProjection
{
    [JsonPropertyName("displayKind")] public string? DisplayKind { get; init; }
    [JsonPropertyName("replyTo")] public string? ReplyTo { get; init; }
    [JsonPropertyName("quoteSummary")] public string? QuoteSummary { get; init; }
    [JsonPropertyName("quoteSender")] public string? QuoteSender { get; init; }
    [JsonPropertyName("forwardItems")] public IReadOnlyList<ForwardMessageItem>? ForwardItems { get; init; }
    [JsonPropertyName("parseStatus")] public string? ParseStatus { get; init; }
    [JsonPropertyName("truncated")] public bool? Truncated { get; init; }
    [JsonPropertyName("omittedCount")] public int? OmittedCount { get; init; }

    public static string KindLabelFor(string? displayKind, string kind) => displayKind switch
    {
        "quote" => "引用", "forward" => "合并转发", "link" => "链接", "file" => "文件",
        "unknown" => "卡片", _ => kind switch
        {
            "image" => "图片", "file" => "文件", "voice" => "语音", "system" => "系统", _ => "文本",
        },
    };
}

public sealed record ForwardMessageItem(
    [property: JsonPropertyName("sender")] string? Sender = null,
    [property: JsonPropertyName("timestamp")] string? Timestamp = null,
    [property: JsonPropertyName("content")] string Content = "",
    [property: JsonPropertyName("displayKind")] string? DisplayKind = null,
    [property: JsonPropertyName("forwardItems")] IReadOnlyList<ForwardMessageItem>? ForwardItems = null);
