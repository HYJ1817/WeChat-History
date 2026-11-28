using CommunityToolkit.Mvvm.ComponentModel;
using WechatMemory.NativeApp.Models;
using WechatMemory.NativeApp.Services;

namespace WechatMemory.NativeApp.ViewModels;

/// <summary>
/// Per-realized-card display state only. No content setter, ID lookup, worker,
/// attachment launch, or navigation; recycling a view starts collapsed again.
/// </summary>
public sealed class MessageCardViewModel : ObservableObject
{
    private readonly IReadOnlyList<ForwardItemPresentation> _forwardItems;
    private bool _isQuoteExpanded;
    private bool _isForwardExpanded;

    public bool IsQuoteExpanded
    {
        get => _isQuoteExpanded;
        set => SetProperty(ref _isQuoteExpanded, value);
    }

    public bool IsForwardExpanded
    {
        get => _isForwardExpanded;
        set
        {
            if (SetProperty(ref _isForwardExpanded, value)) OnPropertyChanged(nameof(ForwardItems));
        }
    }

    public bool HasQuote { get; }
    public string QuoteHeader { get; }
    public string QuoteSummary { get; }
    public string ReplyRelationLabel { get; }
    public bool HasReplyRelation => ReplyRelationLabel.Length > 0;
    public int ForwardItemCount => _forwardItems.Count;
    public bool HasForwardItems => ForwardItemCount > 0;
    public string ForwardHeader => $"合并转发 · {ForwardItemCount:N0} 条展示项";
    // Do not instantiate hundreds of XAML rows while the expander is closed.
    public IReadOnlyList<ForwardItemPresentation> ForwardItems => IsForwardExpanded ? _forwardItems : [];
    public string Notice { get; }
    public bool HasNotice => Notice.Length > 0;
    public bool HasCard => HasQuote || HasReplyRelation || HasForwardItems || HasNotice;

    public MessageCardViewModel(MessageProjection projection)
    {
        HasQuote = !string.IsNullOrWhiteSpace(projection.QuoteSummary) || !string.IsNullOrWhiteSpace(projection.QuoteSender);
        QuoteHeader = string.IsNullOrWhiteSpace(projection.QuoteSender) ? "引用摘要" : $"引用摘要 · {projection.QuoteSender}";
        QuoteSummary = projection.QuoteSummary ?? "引用内容未提供；请以消息正文为准。";
        ReplyRelationLabel = string.IsNullOrWhiteSpace(projection.ReplyTo)
            ? "" : $"引用消息 ID：{projection.ReplyTo}（仅显示关系，不跳转）";

        var rows = new List<ForwardItemPresentation>();
        bool displayLimited = false;
        void Add(IReadOnlyList<ForwardMessageItem>? items, string prefix, int depth)
        {
            if (items is null || items.Count == 0) return;
            if (depth > MessageProjectionReader.MaxForwardDepth) { displayLimited = true; return; }
            for (int index = 0; index < items.Count; ++index)
            {
                if (rows.Count == MessageProjectionReader.MaxForwardItems) { displayLimited = true; return; }
                var item = items[index];
                string number = prefix + (index + 1).ToString(System.Globalization.CultureInfo.InvariantCulture);
                string sender = string.IsNullOrWhiteSpace(item.Sender) ? "未知发送者" : item.Sender;
                // Forward timestamps may be display text/epoch strings, not ISO dates.
                // All fields remain literal text; links/media never become native I/O.
                string label = $"{number}. {sender}" + (string.IsNullOrWhiteSpace(item.Timestamp) ? "" : $" · {item.Timestamp}");
                rows.Add(new ForwardItemPresentation(label, item.Content));
                Add(item.ForwardItems, number + ".", depth + 1);
            }
        }
        Add(projection.ForwardItems, "", 1);
        _forwardItems = rows.ToArray();

        var notices = new List<string>();
        string? status = projection.ParseStatus switch
        {
            null or "" or "ok" => null,
            "partial" => "此卡片仅部分解析。",
            "unsupported" => "此卡片结构暂不支持完整展示。",
            "invalid" => "此卡片格式有误，已使用可读正文回退。",
            "rejected" => "此卡片未通过安全解析，已使用可读正文回退。",
            "limit_exceeded" => "此卡片超出解析预算，未完整展开。",
            _ => "此卡片解析状态未知，请以消息正文为准。",
        };
        if (status is not null) notices.Add(status);
        if (projection.Truncated == true) notices.Add("内容已截断。");
        if (projection.OmittedCount is > 0) notices.Add($"另有 {projection.OmittedCount:N0} 条转发条目未展开。");
        else if (projection.Truncated == true && projection.OmittedCount is null)
            notices.Add("省略条目数量未知。");
        if (displayLimited) notices.Add($"卡片最多展示 {MessageProjectionReader.MaxForwardItems} 条、{MessageProjectionReader.MaxForwardDepth} 层；部分条目未显示。");
        if (notices.Count > 0) notices.Add("折叠或展开不改变规范正文。");
        Notice = string.Join("", notices);
    }
}

public sealed record ForwardItemPresentation(string Label, string Content);
