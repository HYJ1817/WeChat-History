using System.Text.Json;
using System.Text.Json.Nodes;
using WechatMemory.NativeApp.Models;
using WechatMemory.NativeApp.Services;
using Xunit;

namespace NativeApp.Tests;

public sealed class GlobalSearchValidationTests
{
    [Theory]
    [InlineData("missing-total")]
    [InlineData("negative-total")]
    [InlineData("more-items-than-total")]
    [InlineData("next-past-total")]
    [InlineData("missing-next-page")]
    [InlineData("wrong-result-limit")]
    [InlineData("wrong-truncation")]
    [InlineData("duplicate-hit")]
    [InlineData("invalid-fields")]
    [InlineData("empty-fields")]
    [InlineData("invalid-time")]
    [InlineData("missing-sender")]
    [InlineData("coverage-array")]
    [InlineData("coverage-missing-complete")]
    [InlineData("coverage-negative-count")]
    [InlineData("coverage-complete-with-partials")]
    [InlineData("coverage-counts-inconsistent")]
    [InlineData("coverage-bad-warnings")]
    public async Task FirstPageSemanticFailuresAreRejectedBeforePublishing(string defect)
    {
        var page = Page();
        switch (defect)
        {
            case "missing-total": page.Remove("total"); break;
            case "negative-total": page["total"] = -1; break;
            case "more-items-than-total": page["total"] = 0; break;
            case "next-past-total": page["nextOffset"] = 1; break;
            case "missing-next-page": page["total"] = 2; break;
            case "wrong-result-limit": page["resultLimit"] = 999; break;
            case "wrong-truncation": page["truncated"] = true; break;
            case "duplicate-hit":
                page["items"]!.AsArray().Add(page["items"]![0]!.DeepClone()); page["total"] = 2; break;
            case "invalid-fields": page["items"]![0]!["matchedFields"] = new JsonArray("physicalPath"); break;
            case "empty-fields": page["items"]![0]!["matchedFields"] = new JsonArray(); break;
            case "invalid-time": page["items"]![0]!["timestamp"] = "not-a-date"; break;
            case "missing-sender": page["items"]![0]!.AsObject().Remove("sender"); break;
            case "coverage-array": page["coverage"] = new JsonArray(); break;
            case "coverage-missing-complete": page["coverage"] = new JsonObject(); break;
            case "coverage-negative-count": page["coverage"]!["partialMessages"] = -1; break;
            case "coverage-complete-with-partials": page["coverage"]!["partialMessages"] = 1; break;
            case "coverage-counts-inconsistent":
                page["coverage"]!["complete"] = false;
                page["coverage"]!["partialMessages"] = 2;
                page["coverage"]!["scannedMessages"] = 1; break;
            case "coverage-bad-warnings": page["coverage"]!["warnings"] = "incomplete"; break;
        }
        var workflow = new GlobalSearchWorkflow();
        var error = await Assert.ThrowsAsync<WorkerException>(() => workflow.SearchFirstPageAsync(
            new Responses(page), "demo", "source-1", Criteria));
        Assert.Equal("PROTOCOL_ERROR", error.Code);
    }

    [Theory]
    [InlineData("search-id")]
    [InlineData("source-session")]
    [InlineData("total")]
    [InlineData("coverage")]
    [InlineData("duplicate-hit")]
    [InlineData("extra-items")]
    [InlineData("empty-page")]
    [InlineData("discontinuous-offset")]
    public async Task ContinuationCannotChangeMetadataRepeatHitsOverrunTotalOrSkipOffsets(string defect)
    {
        var first = Page(total: 2);
        first["nextOffset"] = 1;
        var next = Page(total: 2, hitId: "hit-2");
        switch (defect)
        {
            case "search-id": next["searchId"] = "wrong"; break;
            case "source-session": next["sourceSessionId"] = "wrong"; break;
            case "total": next["total"] = 3; next["nextOffset"] = 2; break;
            case "coverage": next["coverage"]!["scannedMessages"] = 100; break;
            case "duplicate-hit": next["items"]![0]!["hitId"] = "hit-1"; break;
            case "extra-items":
                next["items"]!.AsArray().Add(next["items"]![0]!.DeepClone());
                next["items"]![1]!["hitId"] = "hit-3"; break;
            case "empty-page": next["items"] = new JsonArray(); break;
            case "discontinuous-offset": next["nextOffset"] = 4; break;
        }
        var workflow = new GlobalSearchWorkflow();
        var worker = new Responses(first, next);
        var snapshot = await workflow.SearchFirstPageAsync(worker, "demo", "source-1", Criteria);
        var error = await Assert.ThrowsAsync<WorkerException>(() => workflow.SearchNextPageAsync(worker, "demo", snapshot));
        Assert.Equal("PROTOCOL_ERROR", error.Code);
        Assert.Equal("hit-1", Assert.Single(snapshot.Items).HitId);
        Assert.Equal(1, snapshot.NextOffset);
    }

    [Theory]
    [InlineData("wrong-conversation")]
    [InlineData("wrong-kind")]
    [InlineData("wrong-message")]
    [InlineData("wrong-sender")]
    [InlineData("wrong-timestamp")]
    [InlineData("wrong-message-kind")]
    [InlineData("out-of-range-focus")]
    [InlineData("too-many-before")]
    [InlineData("empty-items")]
    public async Task ContextMustVerifyConversationDisplayIdentityAndExactFocus(string defect)
    {
        var context = Context();
        switch (defect)
        {
            case "wrong-conversation": context["conversation"]!["id"] = "wrong"; break;
            case "wrong-kind": context["conversation"]!["kind"] = "direct"; break;
            case "wrong-message": context["items"]![0]!["id"] = "wrong"; break;
            case "wrong-sender": context["items"]![0]!["sender"] = "wrong"; break;
            case "wrong-timestamp": context["items"]![0]!["timestamp"] = "2026-11-25T12:00:01+08:00"; break;
            case "wrong-message-kind": context["items"]![0]!["kind"] = "voice"; break;
            case "out-of-range-focus": context["focusIndex"] = 1; break;
            case "too-many-before":
                for (int i = 0; i < 21; i++) context["items"]!.AsArray().Add(context["items"]![0]!.DeepClone());
                context["focusIndex"] = 21; break;
            case "empty-items": context["items"] = new JsonArray(); break;
        }
        var workflow = new GlobalSearchWorkflow();
        var worker = new Responses(Page(), context);
        var snapshot = await workflow.SearchFirstPageAsync(worker, "demo", "source-1", Criteria);
        var error = await Assert.ThrowsAsync<WorkerException>(() => workflow.GetContextAsync(worker, "demo", snapshot, snapshot.Items[0]));
        Assert.Equal("PROTOCOL_ERROR", error.Code);
    }

    [Fact]
    public async Task OpaqueHitUsesItsSnapshotIdentityAndDuplicateMessageIdsAreNotDeduplicated()
    {
        var context = Context();
        context["items"]!.AsArray().Add(context["items"]![0]!.DeepClone());
        context["items"]!.AsArray().Add(context["items"]![0]!.DeepClone());
        context["focusIndex"] = 1;
        foreach (var message in context["items"]!.AsArray()) message!["attachmentPath"] = "C:/must-not-read.png";
        var workflow = new GlobalSearchWorkflow();
        var worker = new Responses(Page(), context);
        var snapshot = await workflow.SearchFirstPageAsync(worker, "demo", "source-1", Criteria);
        var result = await workflow.GetContextAsync(worker, "demo", snapshot,
            snapshot.Items[0] with { ConversationId = "forged", MessageId = "forged", Sender = "forged" });
        Assert.Equal(3, result.Items.Count);
        Assert.Equal(1, result.FocusIndex);
        Assert.Single(result.Items, item => item.IsContextFocus);
        Assert.All(result.Items, item => Assert.Null(item.AttachmentPath));
    }

    [Fact]
    public async Task EmptyIncompleteSearchAndShortBudgetLimitedPagesAreValid()
    {
        var page = Page(total: 0);
        page["items"] = new JsonArray();
        page["coverage"] = new JsonObject { ["complete"] = false, ["partialMessages"] = 2, ["scannedMessages"] = 10 };
        var workflow = new GlobalSearchWorkflow();
        var empty = await workflow.SearchFirstPageAsync(new Responses(page), "offline", "source-1", Criteria);
        Assert.Empty(empty.Items);
        Assert.Contains("2", empty.CoverageNotice);
        Assert.Null(empty.NextOffset);
        var shortPage = Page(total: 1_001);
        shortPage["truncated"] = true;
        shortPage["nextOffset"] = 1;
        var result = await workflow.SearchFirstPageAsync(new Responses(shortPage), "wechat4", "source-1", Criteria);
        Assert.Single(result.Items);
        Assert.True(result.Truncated);
        Assert.Equal(1_001, result.Total);
        Assert.Contains("1,000", result.Summary);
    }

    [Fact]
    public async Task QueryDatesOrderAndLimitAreValidatedBeforeSending()
    {
        var worker = new Responses();
        var workflow = new GlobalSearchWorkflow();
        foreach (var criteria in new[]
        {
            Criteria with { Query = " \t " }, Criteria with { Order = "random" },
            Criteria with { StartDate = "bad-date" }, Criteria with { StartDate = "2026-11-28", EndDate = "2026-11-01" },
        })
        {
            var error = await Assert.ThrowsAsync<WorkerException>(() => workflow.SearchFirstPageAsync(worker, "demo", "source-1", criteria));
            Assert.Equal("INVALID_ARGUMENT", error.Code);
        }
        await Assert.ThrowsAsync<WorkerException>(() => workflow.SearchFirstPageAsync(worker, "demo", "source-1", Criteria, limit: 101));
        Assert.Equal(0, worker.Requests);
    }

    private static GlobalSearchCriteria Criteria => new("needle", null, null, "desc");
    private static JsonObject Page(int total = 1, string hitId = "hit-1") => new()
    {
        ["searchId"] = "search-1", ["sourceSessionId"] = "source-1", ["total"] = total,
        ["resultLimit"] = 1000, ["truncated"] = false, ["nextOffset"] = null,
        ["coverage"] = new JsonObject { ["complete"] = true },
        ["items"] = new JsonArray(new JsonObject
        {
            ["hitId"] = hitId, ["conversationId"] = "conv-1", ["conversationTitle"] = "合成会话",
            ["conversationKind"] = "group", ["messageId"] = "msg-1", ["sender"] = "合成成员",
            ["timestamp"] = "2026-11-25T12:00:00+08:00", ["kind"] = "text",
            ["snippet"] = "合成命中内容", ["matchedFields"] = new JsonArray("content"),
        }),
    };

    private static JsonObject Context() => new()
    {
        ["conversation"] = new JsonObject { ["id"] = "conv-1", ["title"] = "合成会话", ["kind"] = "group" },
        ["focusIndex"] = 0, ["hasEarlier"] = false, ["hasLater"] = false,
        ["items"] = new JsonArray(new JsonObject
        {
            ["id"] = "msg-1", ["sender"] = "合成成员", ["timestamp"] = "2026-11-25T12:00:00+08:00",
            ["kind"] = "text", ["content"] = "合成命中内容",
        }),
    };

    private sealed class Responses(params JsonObject[] responses) : IWorkerRequestClient
    {
        public int Requests { get; private set; }
        public Task<JsonElement> RequestAsync(string command, object payload, TimeSpan? timeout = null) =>
            Task.FromResult(JsonSerializer.SerializeToElement(responses[Requests++]));
    }
}
