using System.Text.Json;
using System.Text.Json.Nodes;
using WechatMemory.NativeApp.Models;
using WechatMemory.NativeApp.Services;
using WechatMemory.NativeApp.ViewModels;
using Xunit;

namespace NativeApp.Tests;

public sealed class MessageCardWorkflowTests
{
    [Fact]
    public async Task TheFullBoundedForwardListIsAcceptedWithoutDeduplication()
    {
        var peer = new MessagePagingWorkflowTests.PagingPeer(1)
        {
            Mutate = page =>
            {
                var items = new JsonArray();
                for (int i = 0; i < MessageProjectionReader.MaxForwardItems; ++i)
                    items.Add(new JsonObject { ["sender"] = "重复成员", ["content"] = "重复正文" });
                page["items"]![0]!["displayKind"] = "forward";
                page["items"]![0]!["forwardItems"] = items;
            },
        };
        var window = await new MessagePagingWorkflow().FirstPageAsync(peer, "demo", "source-1", "c", new(null, null));
        var card = new MessageCardViewModel(Assert.Single(window.Items)) { IsForwardExpanded = true };
        Assert.Equal(200, card.ForwardItems.Count);
        Assert.False(card.HasNotice);
    }

    [Theory]
    [InlineData(false, false)]
    [InlineData(true, false)]
    [InlineData(false, true)]
    [InlineData(true, true)]
    public async Task SearchAndContextRejectMalformedOrOversizedOptionalFields(bool context, bool oversized)
    {
        var hit = new JsonObject
        {
            ["hitId"] = "hit-1", ["conversationId"] = "conv-1", ["conversationTitle"] = "会话",
            ["conversationKind"] = "direct", ["messageId"] = "msg-1", ["sender"] = "成员",
            ["timestamp"] = "2026-11-25T12:00:00+08:00", ["kind"] = "text", ["snippet"] = "摘要",
            ["matchedFields"] = new JsonArray("content"),
        };
        var page = new JsonObject
        {
            ["searchId"] = "search-1", ["sourceSessionId"] = "source-1", ["items"] = new JsonArray(hit),
            ["total"] = 1, ["resultLimit"] = 1000, ["truncated"] = false, ["nextOffset"] = null,
            ["coverage"] = new JsonObject { ["complete"] = true },
        };
        var message = new JsonObject
        {
            ["id"] = "msg-1", ["sender"] = "成员", ["timestamp"] = "2026-11-25T12:00:00+08:00",
            ["kind"] = "text", ["content"] = "规范正文",
        };
        var response = new JsonObject
        {
            ["conversation"] = new JsonObject { ["id"] = "conv-1", ["title"] = "会话", ["kind"] = "direct" },
            ["items"] = new JsonArray(message), ["focusIndex"] = 0, ["hasEarlier"] = false, ["hasLater"] = false,
        };
        var target = context ? message : hit;
        if (oversized) target["quoteSender"] = new string('x', GlobalSearchWorkflow.MaxResponseBytes);
        else target["forwardItems"] = "not-an-array";
        var workflow = new GlobalSearchWorkflow();
        var peer = new Responses(page, response);
        var criteria = new GlobalSearchCriteria("摘要", null, null, "desc");
        WorkerException error;
        if (context)
        {
            var snapshot = await workflow.SearchFirstPageAsync(peer, "demo", "source-1", criteria);
            error = await Assert.ThrowsAsync<WorkerException>(() => workflow.GetContextAsync(peer, "demo", snapshot, snapshot.Items[0]));
        }
        else error = await Assert.ThrowsAsync<WorkerException>(() => workflow.SearchFirstPageAsync(peer, "demo", "source-1", criteria));
        Assert.Equal("PROTOCOL_ERROR", error.Code);
    }

    private sealed class Responses(params JsonObject[] replies) : IWorkerRequestClient
    {
        private int _index;
        public Task<JsonElement> RequestAsync(string command, object payload, TimeSpan? timeout = null) =>
            Task.FromResult(JsonSerializer.SerializeToElement(replies[_index++]));
    }

    [Fact]
    public async Task CollapsibleProjectionTextCountsTowardTheExistingPageWindowBudgetInBothDirections()
    {
        var worker = new MessagePagingWorkflowTests.PagingPeer(2_000);
        var workflow = new MessagePagingWorkflow();
        var plain = await workflow.FirstPageAsync(worker, "demo", "source-1", "c", new(null, null));
        worker.Mutate = page =>
        {
            foreach (var item in page["items"]!.AsArray())
            {
                item!["displayKind"] = "forward";
                item["forwardItems"] = new JsonArray(new JsonObject
                {
                    ["sender"] = "内部成员", ["content"] = new string('x', 7_000), ["displayKind"] = "text",
                });
            }
        };
        var window = await workflow.FirstPageAsync(worker, "demo", "source-1", "c", new(null, null));
        Assert.True(window.DisplayBytes >= plain.DisplayBytes + 50 * 2 * 7_000);
        for (int index = 0; index < 21; ++index)
        {
            window = await workflow.NavigateAsync(worker, "demo", window, previous: false);
            Assert.InRange(window.DisplayBytes, 1, MessagePagingWorkflow.MaxDisplayBytes);
        }
        Assert.True(window.First.StartIndex > 0);
        Assert.True(window.Pages.Count < MessagePagingWorkflow.MaxVisiblePages);
        long start = window.First.StartIndex;
        window = await workflow.NavigateAsync(worker, "demo", window, previous: true);
        Assert.Equal(start - 50, window.First.StartIndex);
        Assert.InRange(window.DisplayBytes, 1, MessagePagingWorkflow.MaxDisplayBytes);
        Assert.All(window.Items, item => Assert.NotNull(item.ForwardItems));
    }

    [Theory]
    [InlineData("reply-type")]
    [InlineData("summary-type")]
    [InlineData("quote-budget")]
    [InlineData("forward-type")]
    [InlineData("null-item")]
    [InlineData("null-content")]
    [InlineData("entry-budget")]
    [InlineData("nesting-budget")]
    [InlineData("text-budget")]
    [InlineData("negative-omitted")]
    [InlineData("truncated-type")]
    public async Task CursorWorkflowRejectsInvalidOptionalCardsBeforePublishing(string defect)
    {
        var peer = new MessagePagingWorkflowTests.PagingPeer(1)
        {
            Mutate = page =>
            {
                var message = page["items"]![0]!;
                switch (defect)
                {
                    case "reply-type": message["replyTo"] = 42; break;
                    case "summary-type": message["quoteSummary"] = new JsonObject(); break;
                    case "quote-budget": message["quoteSummary"] = new string('x', MessageProjectionReader.MaxQuoteBytes + 1); break;
                    case "forward-type": message["forwardItems"] = new JsonObject(); break;
                    case "null-item": message["forwardItems"] = new JsonArray((JsonNode?)null); break;
                    case "null-content": message["forwardItems"] = new JsonArray(new JsonObject { ["content"] = null }); break;
                    case "entry-budget":
                        var items = new JsonArray();
                        for (int i = 0; i <= MessageProjectionReader.MaxForwardItems; ++i)
                            items.Add(new JsonObject { ["content"] = "text" });
                        message["forwardItems"] = items;
                        break;
                    case "nesting-budget":
                        JsonNode nested = new JsonObject { ["content"] = "deepest" };
                        for (int i = 1; i <= MessageProjectionReader.MaxForwardDepth; ++i)
                            nested = new JsonObject { ["content"] = "parent", ["forwardItems"] = new JsonArray(nested) };
                        message["forwardItems"] = new JsonArray(nested);
                        break;
                    case "text-budget":
                        message["forwardItems"] = new JsonArray(new JsonObject { ["content"] = new string('x', MessageProjectionReader.MaxProjectionTextBytes + 1) });
                        break;
                    case "negative-omitted": message["omittedCount"] = -1; break;
                    case "truncated-type": message["truncated"] = "true"; break;
                }
            },
        };
        var error = await Assert.ThrowsAsync<WorkerException>(() => new MessagePagingWorkflow()
            .FirstPageAsync(peer, "demo", "source-1", "c", new(null, null)));
        Assert.Equal("PROTOCOL_ERROR", error.Code);
    }
}
