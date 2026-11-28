using System.Diagnostics;
using System.Text.Json;
using WechatMemory.NativeApp.Models;
using WechatMemory.NativeApp.Services;
using WechatMemory.NativeApp.ViewModels;
using Xunit;

namespace NativeApp.Tests;

public sealed class MessageCardViewModelTests
{
    [Theory]
    [InlineData("cursor")]
    [InlineData("legacy")]
    public async Task ProductionPreviewReadsOptionalCamelCaseFieldsAndCollapseNeverChangesCanonicalOrExportScope(string scenario)
    {
        await using var app = new TestApp(scenario);
        await app.InitializeAsync();
        var vm = app.Vm;
        var original = vm.Messages.ToArray();
        Assert.Equal(4, original.Length);
        Assert.Equal(original[0].Id, original[1].Id); // A card belongs to a row, not a public ID cache.
        Assert.Equal("引用", original[0].KindLabel);
        Assert.Equal("合并转发", original[1].KindLabel);
        Assert.Equal("reference-42", original[0].ReplyTo);
        var quote = new MessageCardViewModel(original[0]);
        Assert.True(quote.HasQuote);
        Assert.False(quote.IsQuoteExpanded);
        Assert.Contains("原成员", quote.QuoteHeader);
        Assert.Contains("<script>plain text</script>", quote.QuoteSummary);
        Assert.Contains("reference-42", quote.ReplyRelationLabel);
        Assert.Contains("不跳转", quote.ReplyRelationLabel);
        Assert.True(new MessageCardViewModel(original[2]).HasReplyRelation);
        Assert.False(new MessageCardViewModel(original[2]).HasQuote);
        Assert.False(new MessageCardViewModel(original[3]).HasCard);

        var forward = new MessageCardViewModel(original[1]);
        Assert.False(forward.IsForwardExpanded);
        Assert.Empty(forward.ForwardItems);
        Assert.Equal(3, forward.ForwardItemCount);
        Assert.Contains("部分解析", forward.Notice);
        Assert.Contains("内容已截断", forward.Notice);
        Assert.Contains("数量未知", forward.Notice);
        forward.IsForwardExpanded = true;
        Assert.Equal(3, forward.ForwardItems.Count);
        Assert.Contains("1. 内部成员", forward.ForwardItems[0].Label);
        Assert.Contains("2.1. 未知发送者", forward.ForwardItems[2].Label);
        Assert.Contains("1730000000", forward.ForwardItems[2].Label); // Not parsed as an ISO timestamp.
        Assert.Contains("C:/must-not-open.png", forward.ForwardItems[2].Content);
        var displayed = forward.ForwardItems;
        quote.IsQuoteExpanded = true;
        forward.IsForwardExpanded = false;
        Assert.Empty(forward.ForwardItems);
        forward.IsForwardExpanded = true;
        Assert.Same(displayed, forward.ForwardItems);
        quote.IsQuoteExpanded = false;
        Assert.Equal(original, vm.Messages);
        Assert.Equal("conv-1", vm.SelectedConversation?.Id);
        Assert.Empty(app.Requests("get_message_context"));
        Assert.Empty(app.Requests("search_messages"));
        Assert.Single(app.Requests(scenario == "legacy" ? "get_messages" : "get_messages_page"));
        Assert.Equal(-1, vm.ContextFocusIndex);
        Assert.True(vm.CanExport, vm.Error);
        await vm.ExportAsync(app.PathFor("canonical.md"));
        var export = Assert.Single(app.Requests("export_markdown"));
        Assert.Equal("conv-1", export.GetProperty("conversationId").GetString());
        Assert.False(export.TryGetProperty("cursor", out _));
        Assert.False(export.TryGetProperty("content", out _));
        Assert.False(export.TryGetProperty("forwardItems", out _));
        Assert.Equal(original, vm.Messages);
    }

    [Theory]
    [InlineData("cursor")]
    [InlineData("legacy")]
    public async Task SearchAndVerifiedContextCarryTheSameProjectionWithoutCardNavigation(string scenario)
    {
        await using var app = new TestApp(scenario);
        await app.InitializeAsync();
        var vm = app.Vm;
        vm.SwitchToGlobalSearchCommand.Execute(null);
        vm.GlobalSearchQuery = "子条目";
        await vm.ApplyFiltersCommand.ExecuteAsync(null);
        Assert.Equal(2, vm.GlobalSearchHits.Count);
        var hit = vm.GlobalSearchHits[1];
        Assert.Equal("合并转发", hit.KindLabel);
        var card = new MessageCardViewModel(hit) { IsForwardExpanded = true };
        Assert.Equal(3, card.ForwardItems.Count);
        Assert.Equal("conv-1", vm.SelectedConversation?.Id);
        Assert.Empty(app.Requests("get_message_context")); // Expanding is not a locator click.
        Assert.False(vm.CanExport);
        Assert.True(await vm.OpenGlobalHitAsync(hit, 123.5), vm.Error);
        var focused = vm.ContextMessages[vm.ContextFocusIndex];
        Assert.Equal(1, vm.ContextFocusIndex);
        Assert.True(focused.IsContextFocus);
        Assert.Equal(hit.DisplayKind, focused.DisplayKind);
        Assert.Equal(hit.ForwardItems!.Count, focused.ForwardItems!.Count);
        Assert.All(vm.ContextMessages, item => Assert.Null(item.AttachmentPath));
        string canonical = focused.Content;
        var contextCard = new MessageCardViewModel(focused) { IsForwardExpanded = true };
        contextCard.IsForwardExpanded = false;
        Assert.Equal(canonical, vm.ContextMessages[vm.ContextFocusIndex].Content);
        Assert.Equal(1, vm.ContextFocusIndex);
        Assert.False(vm.CanLoadMore);
        Assert.False(vm.CanLoadNextMessageBatch);
        Assert.Single(app.Requests("get_message_context"));
        await vm.ReturnToSearchCommand.ExecuteAsync(null);
        Assert.Equal(123.5, vm.GlobalReturnScrollOffset);
        Assert.Same(hit, vm.GlobalSearchHits[1]);
        Assert.True(await vm.OpenGlobalHitAsync(vm.GlobalSearchHits[0]), vm.Error);
        var quote = vm.ContextMessages[vm.ContextFocusIndex];
        Assert.Equal("reference-42", quote.ReplyTo);
        Assert.True(new MessageCardViewModel(quote).HasQuote);
    }

    [Theory]
    [InlineData("absent")]
    [InlineData("null")]
    public async Task WorkersWithNoProjectionKeepPlainContentInPreviewSearchAndContext(string scenario)
    {
        await using var app = new TestApp(scenario);
        await app.InitializeAsync();
        var vm = app.Vm;
        Assert.All(vm.Messages, item =>
        {
            Assert.Null(item.DisplayKind);
            Assert.Null(item.ReplyTo);
            Assert.False(new MessageCardViewModel(item).HasCard);
        });
        Assert.Equal("当前回复\n引用 原成员: 原始摘要", vm.Messages[0].Content);
        Assert.Equal("文件", vm.Messages[1].KindLabel);
        vm.SwitchToGlobalSearchCommand.Execute(null);
        vm.GlobalSearchQuery = "合成";
        await vm.ApplyFiltersCommand.ExecuteAsync(null);
        Assert.All(vm.GlobalSearchHits, hit => Assert.False(new MessageCardViewModel(hit).HasCard));
        Assert.True(await vm.OpenGlobalHitAsync(vm.GlobalSearchHits[0]), vm.Error);
        Assert.All(vm.ContextMessages, item => Assert.False(new MessageCardViewModel(item).HasCard));
        Assert.Contains("当前回复", vm.ContextMessages[vm.ContextFocusIndex].Content);
    }

    [Fact]
    public async Task InvalidOptionalMetadataDoesNotReplaceTheAppliedWindowOrFilters()
    {
        await using var app = new TestApp("invalid-refresh");
        await app.InitializeAsync();
        var original = app.Vm.Messages.ToArray();
        string applied = app.Vm.RangeLabel;
        app.Vm.StartDate = new DateTimeOffset(2026, 11, 1, 0, 0, 0, DateRange.Offset);
        await app.Vm.RefreshMessagesCommand.ExecuteAsync(null);
        Assert.Contains("消息展示字段", app.Vm.Error);
        Assert.Equal(original, app.Vm.Messages);
        Assert.Equal(applied, app.Vm.RangeLabel);
        Assert.True(app.Vm.FiltersDirty);
    }

    [Fact]
    public void DisplayListHasItsOwnBoundKeepsDuplicateRowsAndAccurateOmissionNotices()
    {
        var item = new ForwardMessageItem("同一发送者", null, "相同正文", "text");
        var message = new ChatMessage("id", "outer", "2026-11-25T12:00:00+08:00", "完整规范正文，不裁剪", "file")
        {
            DisplayKind = "forward", ForwardItems = Enumerable.Repeat(item, 205).ToArray(),
            Truncated = true, OmittedCount = 7, ParseStatus = "partial",
        };
        var card = new MessageCardViewModel(message) { IsForwardExpanded = true };
        Assert.Equal(MessageProjectionReader.MaxForwardItems, card.ForwardItems.Count);
        Assert.Equal("相同正文", card.ForwardItems[0].Content);
        Assert.Equal("相同正文", card.ForwardItems[1].Content);
        Assert.NotEqual(card.ForwardItems[0].Label, card.ForwardItems[1].Label);
        Assert.Contains("另有 7 条", card.Notice);
        Assert.Contains("最多展示 200 条", card.Notice);
        card.IsForwardExpanded = false;
        Assert.Equal("完整规范正文，不裁剪", message.Content);
        Assert.Equal(205, message.ForwardItems.Count);
        Assert.False(new MessageCardViewModel(message).IsForwardExpanded);
    }

    private sealed class TestApp : IAsyncDisposable
    {
        private readonly string _root = Path.Combine(Path.GetTempPath(), "wechat-native-cards-" + Guid.NewGuid().ToString("N"));
        public MainViewModel Vm { get; }
        public TestApp(string scenario)
        {
            Directory.CreateDirectory(_root);
            Vm = new MainViewModel(action => action(), () =>
            {
                string? project = AppContext.BaseDirectory;
                while (project is not null && !File.Exists(Path.Combine(project, "global.json"))) project = Directory.GetParent(project)?.FullName;
                Assert.NotNull(project);
                var info = new ProcessStartInfo(Path.Combine(project!, ".venv", "Scripts", "python.exe")) { WorkingDirectory = _root };
                foreach (string arg in new[] { "-I", "-X", "utf8", "-u", Path.Combine(AppContext.BaseDirectory, "fixtures", "message_cards_vm_worker.py"), scenario, _root })
                    info.ArgumentList.Add(arg);
                return WorkerClient.Start(info);
            }, true, (_, _) => { });
        }
        public async Task InitializeAsync()
        {
            await Vm.InitializeAsync();
            Assert.Equal("", Vm.Error);
        }
        public string PathFor(string name) => Path.Combine(_root, name);
        public List<JsonElement> Requests(string command)
        {
            using var file = new FileStream(PathFor("requests.jsonl"), FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
            using var reader = new StreamReader(file);
            var result = new List<JsonElement>();
            while (reader.ReadLine() is { } line)
            {
                using var document = JsonDocument.Parse(line);
                if (document.RootElement.GetProperty("command").GetString() == command)
                    result.Add(document.RootElement.GetProperty("payload").Clone());
            }
            return result;
        }
        public async ValueTask DisposeAsync()
        {
            await Vm.DisposeAsync();
            Directory.Delete(_root, recursive: true);
        }
    }
}
