using System.Text;
using System.Text.Json;
using WechatMemory.NativeApp.Models;

namespace WechatMemory.NativeApp.Services;

/// <summary>One optional-field boundary shared by search, context, legacy and cursor previews.</summary>
public static class MessageProjectionReader
{
    public const int MaxForwardItems = 200;
    public const int MaxForwardDepth = 2;
    public const int MaxQuoteBytes = 2 * 1024;
    // Allows the parser's 64 KiB display text plus labels/metadata, without letting
    // optional fields bypass the bounded native page window.
    public const int MaxProjectionTextBytes = 128 * 1024;

    public static T Read<T>(JsonElement value, T message) where T : MessageProjection
    {
        MessageProjection fields;
        try
        {
            fields = value.Deserialize<MessageProjection>(Protocol.Json)
                ?? throw InvalidProjection();
        }
        catch (JsonException error)
        {
            throw new WorkerException("PROTOCOL_ERROR", "消息展示字段格式不正确。", error);
        }
        Validate(fields);
        return message with
        {
            DisplayKind = fields.DisplayKind, ReplyTo = fields.ReplyTo,
            QuoteSummary = fields.QuoteSummary, QuoteSender = fields.QuoteSender,
            ForwardItems = fields.ForwardItems, ParseStatus = fields.ParseStatus,
            Truncated = fields.Truncated, OmittedCount = fields.OmittedCount,
        };
    }

    public static void Validate(MessageProjection projection)
    {
        if (projection.OmittedCount is < 0 || Utf8(projection.QuoteSummary) > MaxQuoteBytes)
            throw InvalidProjection();
        long textBytes = Utf8(projection.DisplayKind) + Utf8(projection.ReplyTo) + Utf8(projection.QuoteSummary) +
            Utf8(projection.QuoteSender) + Utf8(projection.ParseStatus);
        int count = 0;
        void Visit(IReadOnlyList<ForwardMessageItem>? items, int depth)
        {
            if (items is null || items.Count == 0) return;
            if (depth > MaxForwardDepth || items.Count > MaxForwardItems - count) throw InvalidProjection();
            foreach (var item in items)
            {
                if (item is null || item.Content is null || ++count > MaxForwardItems) throw InvalidProjection();
                textBytes += Utf8(item.Sender) + Utf8(item.Timestamp) + Utf8(item.Content) + Utf8(item.DisplayKind);
                if (textBytes > MaxProjectionTextBytes) throw InvalidProjection();
                Visit(item.ForwardItems, depth + 1);
            }
        }
        Visit(projection.ForwardItems, 1);
        if (textBytes > MaxProjectionTextBytes) throw InvalidProjection();
    }

    /// <summary>Retained UTF-16 strings and row overhead, even when cards are collapsed.</summary>
    public static long DisplayBytes(MessageProjection projection)
    {
        long bytes = 64L + Utf16(projection.DisplayKind) + Utf16(projection.ReplyTo) + Utf16(projection.QuoteSummary) +
            Utf16(projection.QuoteSender) + Utf16(projection.ParseStatus);
        void Visit(IReadOnlyList<ForwardMessageItem>? items)
        {
            if (items is null) return;
            foreach (var item in items)
            {
                bytes += 128L + Utf16(item.Sender) + Utf16(item.Timestamp) + Utf16(item.Content) + Utf16(item.DisplayKind);
                Visit(item.ForwardItems);
            }
        }
        // Called only after Validate has bounded the tree depth/count.
        Visit(projection.ForwardItems);
        return bytes;
    }

    private static long Utf8(string? text) => text is null ? 0 : Encoding.UTF8.GetByteCount(text);
    private static long Utf16(string? text) => 2L * (text?.Length ?? 0);
    private static WorkerException InvalidProjection() => new("PROTOCOL_ERROR", "消息展示字段无效或超出卡片展示预算。");
}
