using System.Text.Json;
using System.Text.Json.Nodes;
using WechatMemory.NativeApp.Services;
using Xunit;

namespace NativeApp.Tests;

public sealed class MessagePagingWorkflowTests
{
    [Fact]
    public async Task EmptyStringRemainsAKeywordPayloadRatherThanBecomingNull()
    {
        var worker = new PagingPeer(1) { Keyword = true };
        await new MessagePagingWorkflow().KeywordFirstPageAsync(worker, "demo", "source-1", "c",
            new DateRange(null, null), "");
        Assert.Equal(JsonValueKind.String, worker.Requests[0].Payload.GetProperty("query").ValueKind);
        Assert.Equal("", worker.Requests[0].Payload.GetProperty("query").GetString());
    }

    [Fact]
    public async Task KeywordPagesUseTheExactB3PayloadAndValidateTheSnapshotMetadata()
    {
        var worker = new PagingPeer(151) { Keyword = true };
        var workflow = new MessagePagingWorkflow();
        var first = await workflow.KeywordFirstPageAsync(worker, "offline", "source-1", "conversation-1",
            new DateRange("2026-11-01", "2026-11-28"), "needle");
        var request = worker.Requests[0];
        Assert.Equal(new[] { "conversationId", "endDate", "limit", "order", "query", "source", "sourceSessionId", "startDate" },
            request.Payload.EnumerateObject().Select(p => p.Name).Order(StringComparer.Ordinal));
        Assert.Equal("needle", request.Payload.GetProperty("query").GetString());
        Assert.Equal("keyword", first.First.Mode);
        Assert.Equal(200L, first.First.CandidateCount);
        Assert.Equal("desc", first.First.Order);
        Assert.Equal("canonical-appmsg-v1", first.First.ProjectionVersion);
        Assert.Equal(151, first.Total);

        var next = await workflow.NavigateAsync(worker, "offline", first, false);
        Assert.Equal(100, next.Items.Count());
        Assert.Equal("keyword", next.Last.Mode);
        Assert.Equal(new[] { "cursor", "limit", "pageSessionId", "source", "sourceSessionId" },
            worker.Requests[1].Payload.EnumerateObject().Select(p => p.Name).Order(StringComparer.Ordinal));
    }

    [Theory]
    [InlineData("candidate")]
    [InlineData("source")]
    [InlineData("order")]
    [InlineData("projection")]
    [InlineData("mode")]
    public async Task KeywordContinuationRejectsChangedSnapshotIdentity(string defect)
    {
        var worker = new PagingPeer(151) { Keyword = true };
        var workflow = new MessagePagingWorkflow();
        var first = await workflow.KeywordFirstPageAsync(worker, "offline", "source-1", "c",
            new DateRange(null, null), "needle");
        worker.Mutate = node =>
        {
            switch (defect)
            {
                case "candidate": node["candidateCount"] = 201; break;
                case "source": node["sourceSessionId"] = "other-source"; break;
                case "order": node["order"] = "asc"; break;
                case "projection": node["projectionVersion"] = "other-projection"; break;
                case "mode": node["mode"] = "cursor"; break;
            }
        };
        var error = await Assert.ThrowsAsync<WorkerException>(() => workflow.NavigateAsync(worker, "offline", first, false));
        Assert.Equal("PROTOCOL_ERROR", error.Code);
        Assert.Single(first.Pages);
    }

    [Fact]
    public async Task FirstAndBothDirectionsUseOnlyThePlannedPayloadAndPreserveWorkerOrder()
    {
        var worker = new PagingPeer(1_101);
        var workflow = new MessagePagingWorkflow();
        var first = await workflow.FirstPageAsync(worker, "demo", "source-1", "conversation-1",
            new DateRange("2026-11-01", "2026-11-28"));
        var request = worker.Requests[0];
        Assert.Equal("get_messages_page", request.Command);
        Assert.Equal(new[] { "conversationId", "endDate", "limit", "order", "query", "source", "sourceSessionId", "startDate" },
            request.Payload.EnumerateObject().Select(p => p.Name).Order(StringComparer.Ordinal));
        Assert.Equal(JsonValueKind.Null, request.Payload.GetProperty("query").ValueKind);
        Assert.Equal("2026-11-01", request.Payload.GetProperty("startDate").GetString());
        Assert.Equal("2026-11-28", request.Payload.GetProperty("endDate").GetString());
        Assert.Equal("desc", request.Payload.GetProperty("order").GetString());
        Assert.Equal(50, request.Payload.GetProperty("limit").GetInt32());
        Assert.Equal(TimeSpan.FromSeconds(60), request.Timeout);
        Assert.Null(first.First.PreviousCursor);
        Assert.Equal(1_101, first.Total);

        var window = first;
        for (int i = 0; i < 20; ++i) window = await workflow.NavigateAsync(worker, "demo", window, false);
        Assert.Equal(20, window.Pages.Count);
        Assert.Equal(50, window.First.StartIndex);
        Assert.Equal(1050, window.Last.EndIndex);
        Assert.Equal("row-50", window.Items.First().Content);
        Assert.Equal("row-1049", window.Items.Last().Content);
        Assert.Equal(1000, window.Items.Count());
        Assert.Equal("duplicate-public-id", window.Items.First().Id); // Do not deduplicate physical rows.
        Assert.All(worker.Requests.Skip(1), call =>
        {
            Assert.Equal(new[] { "cursor", "limit", "pageSessionId", "source", "sourceSessionId" },
                call.Payload.EnumerateObject().Select(p => p.Name).Order(StringComparer.Ordinal));
            Assert.Equal("source-1", call.Payload.GetProperty("sourceSessionId").GetString());
            Assert.Equal(50, call.Payload.GetProperty("limit").GetInt32());
        });
        window = await workflow.NavigateAsync(worker, "demo", window, true);
        Assert.Equal("p:50", worker.Requests[^1].Payload.GetProperty("cursor").GetString());
        Assert.Equal(0, window.First.StartIndex);
        Assert.Null(window.First.PreviousCursor);
        Assert.Equal("row-0", window.Items.First().Content);
        Assert.Equal("row-999", window.Items.Last().Content);
        Assert.Equal(20, window.Pages.Count);
    }

    [Fact]
    public async Task DisplayByteBudgetEvictsWholePagesInBothDirectionsBeforeThePageCountLimit()
    {
        var worker = new PagingPeer(100) { PageLength = 1, Content = new string('x', 500_000) };
        var workflow = new MessagePagingWorkflow();
        var window = await workflow.FirstPageAsync(worker, "demo", "source-1", "c", new(null, null));
        for (int i = 0; i < 25; ++i)
        {
            window = await workflow.NavigateAsync(worker, "demo", window, false);
            Assert.InRange(window.DisplayBytes, 1, MessagePagingWorkflow.MaxDisplayBytes);
            Assert.InRange(window.Pages.Count, 1, MessagePagingWorkflow.MaxVisiblePages - 1);
        }
        long start = window.First.StartIndex;
        window = await workflow.NavigateAsync(worker, "demo", window, true);
        Assert.Equal(start - 1, window.First.StartIndex);
        Assert.InRange(window.DisplayBytes, 1, MessagePagingWorkflow.MaxDisplayBytes);
    }

    [Fact]
    public async Task ShortAndEmptyTerminalPagesAndTotalsBeyondLegacyIntLimitAreValid()
    {
        var workflow = new MessagePagingWorkflow();
        var worker = new PagingPeer(51);
        var window = await workflow.FirstPageAsync(worker, "offline", "source-1", "c", new(null, null));
        window = await workflow.NavigateAsync(worker, "offline", window, false);
        Assert.Single(window.Last.Items);
        Assert.Null(window.Last.NextCursor);
        await Assert.ThrowsAsync<WorkerException>(() => workflow.NavigateAsync(worker, "offline", window, false));
        var empty = await workflow.FirstPageAsync(new PagingPeer(0), "demo", "source-1", "c", new(null, null));
        Assert.Empty(empty.Items);
        Assert.Null(empty.First.NextCursor);
        var large = new PagingPeer(1) { Mutate = node => { node["total"] = (long)int.MaxValue + 1; node["nextCursor"] = "opaque-next"; } };
        Assert.Equal((long)int.MaxValue + 1,
            (await workflow.FirstPageAsync(large, "demo", "source-1", "c", new(null, null))).Total);
    }

    [Theory]
    [InlineData("missing-session")]
    [InlineData("blank-session")]
    [InlineData("wrong-sort")]
    [InlineData("negative-total")]
    [InlineData("fraction-total")]
    [InlineData("string-total")]
    [InlineData("missing-next")]
    [InlineData("invalid-cursor")]
    [InlineData("blank-cursor")]
    [InlineData("first-previous")]
    [InlineData("missing-next-boundary")]
    [InlineData("same-cursors")]
    [InlineData("wrong-source-echo")]
    [InlineData("too-many-items")]
    [InlineData("empty-nonterminal")]
    [InlineData("bad-timestamp")]
    [InlineData("null-content")]
    [InlineData("bad-outgoing")]
    [InlineData("bad-duration")]
    [InlineData("bad-attachment")]
    [InlineData("bad-transcript")]
    [InlineData("oversize")]
    public async Task InvalidFirstResponsesAreRejectedWithoutPublishingAPartialPage(string defect)
    {
        var worker = new PagingPeer(100) { Mutate = node =>
        {
            var items = node["items"]!.AsArray();
            switch (defect)
            {
                case "missing-session": node.Remove("pageSessionId"); break;
                case "blank-session": node["pageSessionId"] = " "; break;
                case "wrong-sort": node["sortVersion"] = "legacy"; break;
                case "negative-total": node["total"] = -1; break;
                case "fraction-total": node["total"] = 100.5; break;
                case "string-total": node["total"] = "100"; break;
                case "missing-next": node.Remove("nextCursor"); break;
                case "invalid-cursor": node["nextCursor"] = 10; break;
                case "blank-cursor": node["nextCursor"] = " "; break;
                case "first-previous": node["previousCursor"] = "unexpected"; break;
                case "missing-next-boundary": node["nextCursor"] = null; break;
                case "same-cursors": node["previousCursor"] = node["nextCursor"]!.DeepClone(); break;
                case "wrong-source-echo": node["sourceSessionId"] = "wrong"; break;
                case "too-many-items": items.Add(items[0]!.DeepClone()); break;
                case "empty-nonterminal": items.Clear(); break;
                case "bad-timestamp": items[0]!["timestamp"] = "not-a-date"; break;
                case "null-content": items[0]!["content"] = null; break;
                case "bad-outgoing": items[0]!["isOutgoing"] = "false"; break;
                case "bad-duration": items[0]!["durationMs"] = -1; break;
                case "bad-attachment": items[0]!["attachmentPath"] = false; break;
                case "bad-transcript": items[0]!["transcript"] = new JsonObject(); break;
                case "oversize": items[0]!["content"] = new string('x', MessagePagingWorkflow.MaxResponseBytes); break;
            }
        } };
        var error = await Assert.ThrowsAsync<WorkerException>(() => new MessagePagingWorkflow()
            .FirstPageAsync(worker, "demo", "source-1", "c", new(null, null)));
        Assert.Equal("PROTOCOL_ERROR", error.Code);
        Assert.Single(worker.Requests); // Even an oversized malformed response is not a negotiated size rejection.
    }

    [Theory]
    [InlineData("session")]
    [InlineData("total")]
    [InlineData("previous")]
    [InlineData("next")]
    public async Task ContinuationMustMatchSessionTotalAndExactBoundaries(string defect)
    {
        var worker = new PagingPeer(100);
        var workflow = new MessagePagingWorkflow();
        var original = await workflow.FirstPageAsync(worker, "demo", "source-1", "c", new(null, null));
        worker.Mutate = node =>
        {
            switch (defect)
            {
                case "session": node["pageSessionId"] = "another-session"; break;
                case "total": node["total"] = 101; break;
                case "previous": node["previousCursor"] = null; break;
                case "next": node["nextCursor"] = "unexpected-at-end"; break;
            }
        };
        var error = await Assert.ThrowsAsync<WorkerException>(() => workflow.NavigateAsync(worker, "demo", original, false));
        Assert.Equal("PROTOCOL_ERROR", error.Code);
        Assert.Single(original.Pages);
        Assert.Equal(50, original.Items.Count());
    }

    [Theory]
    [InlineData("PAGE_EXPIRED")]
    [InlineData("PAGE_SOURCE_CHANGED")]
    public async Task ExpiryAndGenerationChangesNeverAutoRefreshOrFallBack(string code)
    {
        var worker = new PagingPeer(100);
        var workflow = new MessagePagingWorkflow();
        var original = await workflow.FirstPageAsync(worker, "demo", "source-1", "c", new(null, null));
        worker.Failure = code;
        Assert.Equal(code, (await Assert.ThrowsAsync<WorkerException>(() => workflow.NavigateAsync(worker, "demo", original, false))).Code);
        Assert.Equal(2, worker.Requests.Count);
        Assert.Single(original.Pages);
    }

    [Fact]
    public async Task ExplicitSizeRejectionsShrinkFirstAndBothDirectionalRequestsAtTheSameBoundary()
    {
        var worker = new PagingPeer(2_351) { Reject = request =>
            request.GetProperty("limit").GetInt32() > 10 ? "PAGE_RESPONSE_TOO_LARGE" : null };
        var workflow = new MessagePagingWorkflow();
        var window = await workflow.FirstPageAsync(worker, "offline", "source-1", "c", new(null, null));
        Assert.Equal(new[] { 50, 25, 12, 6 }, worker.Requests.Select(call => call.Payload.GetProperty("limit").GetInt32()));
        Assert.Equal(Enumerable.Range(0, 6).Select(i => "row-" + i), window.Items.Select(item => item.Content));
        for (int i = 0; i < 21; ++i) window = await workflow.NavigateAsync(worker, "offline", window, false);
        Assert.Equal(12, window.First.StartIndex);
        Assert.Equal(132, window.Last.EndIndex);
        var original = window;
        int before = worker.Requests.Count;
        window = await workflow.NavigateAsync(worker, "offline", window, true);
        Assert.All(worker.Requests.Skip(before), call => Assert.Equal("p:12", call.Payload.GetProperty("cursor").GetString()));
        Assert.Equal(6, window.First.StartIndex);
        Assert.Equal(126, window.Last.EndIndex);
        Assert.Equal(Enumerable.Range(6, 120).Select(i => "row-" + i), window.Items.Select(item => item.Content));
        Assert.Equal(12, original.First.StartIndex); // Navigation never edits an already committed window.
        Assert.All(worker.Requests, call => Assert.Equal("get_messages_page", call.Command));
    }

    [Fact]
    public async Task SingleMessageSizeFailureTerminatesAfterSixAttemptsWithoutChangingTheWindow()
    {
        var worker = new PagingPeer(100);
        var workflow = new MessagePagingWorkflow();
        var original = await workflow.FirstPageAsync(worker, "offline", "source-1", "c", new(null, null));
        worker.Failure = "PAGE_RESPONSE_TOO_LARGE";
        var error = await Assert.ThrowsAsync<WorkerException>(() => workflow.NavigateAsync(worker, "offline", original, false));
        Assert.Equal("PAGE_RESPONSE_TOO_LARGE", error.Code);
        Assert.Contains("1 条", error.Message);
        Assert.Equal(new[] { 50, 25, 12, 6, 3, 1 }, worker.Requests.Skip(1).Select(call => call.Payload.GetProperty("limit").GetInt32()));
        Assert.All(worker.Requests.Skip(1), call => Assert.Equal("n:50", call.Payload.GetProperty("cursor").GetString()));
        Assert.Single(original.Pages);
        Assert.Equal(50, original.Last.EndIndex);
    }

    [Theory]
    [InlineData("PAGE_BUDGET_EXCEEDED")]
    [InlineData("SEARCH_BUDGET_EXCEEDED")]
    [InlineData("RESPONSE_LIMIT_EXCEEDED")]
    [InlineData("MESSAGE_TOO_LARGE")]
    [InlineData("PAGE_RESPONSE_INVALID")]
    [InlineData("PROTOCOL_ERROR")]
    [InlineData("PAGE_EXPIRED")]
    [InlineData("PAGE_SOURCE_CHANGED")]
    public async Task OtherErrorsAreNeverTreatedAsPageSizeNegotiation(string code)
    {
        var worker = new PagingPeer(100) { Failure = code };
        var workflow = new MessagePagingWorkflow();
        Assert.Equal(code, (await Assert.ThrowsAsync<WorkerException>(() => workflow.FirstPageAsync(
            worker, "offline", "source-1", "c", new(null, null)))).Code);
        Assert.Single(worker.Requests);
    }

    [Fact]
    public async Task FailureDuringShrinkingStopsImmediatelyAndMalformedReducedPagesAreNotRetried()
    {
        var worker = new PagingPeer(100) { Reject = request =>
            request.GetProperty("limit").GetInt32() == 50 ? "PAGE_RESPONSE_TOO_LARGE" : "PAGE_EXPIRED" };
        var workflow = new MessagePagingWorkflow();
        Assert.Equal("PAGE_EXPIRED", (await Assert.ThrowsAsync<WorkerException>(() => workflow.FirstPageAsync(
            worker, "offline", "source-1", "c", new(null, null)))).Code);
        Assert.Equal(2, worker.Requests.Count);

        worker = new PagingPeer(100) { Reject = request =>
            request.GetProperty("limit").GetInt32() == 50 ? "PAGE_RESPONSE_TOO_LARGE" : null,
            Mutate = node => node["items"]!.AsArray().Add(node["items"]![0]!.DeepClone()) };
        Assert.Equal("PROTOCOL_ERROR", (await Assert.ThrowsAsync<WorkerException>(() => workflow.FirstPageAsync(
            worker, "offline", "source-1", "c", new(null, null)))).Code);
        Assert.Equal(2, worker.Requests.Count); // Validate against the reduced limit, not the original 50.
    }

    [Fact]
    public async Task KeywordSnapshotFailuresDoNotRetryAnInvalidatedSession()
    {
        var worker = new PagingPeer(100) { Keyword = true };
        var workflow = new MessagePagingWorkflow();
        var original = await workflow.KeywordFirstPageAsync(worker, "offline", "source-1", "c", new(null, null), "needle");
        worker.Failure = "PAGE_RESPONSE_TOO_LARGE";
        await Assert.ThrowsAsync<WorkerException>(() => workflow.NavigateAsync(worker, "offline", original, false));
        Assert.Equal(2, worker.Requests.Count);
    }

    internal sealed class PagingPeer(int total) : IWorkerRequestClient
    {
        public List<(string Command, JsonElement Payload, TimeSpan? Timeout)> Requests { get; } = [];
        public Action<JsonObject>? Mutate { get; set; }
        public string? Failure { get; set; }
        public Func<JsonElement, string?>? Reject { get; set; }
        public int PageLength { get; set; } = 50;
        public string? Content { get; set; }
        public bool Keyword { get; set; }
        public Task<JsonElement> RequestAsync(string command, object payload, TimeSpan? timeout = null)
        {
            var request = JsonSerializer.SerializeToElement(payload, Protocol.Json);
            Requests.Add((command, request, timeout));
            string? failure = Failure ?? Reject?.Invoke(request);
            if (failure is not null) throw new WorkerException(failure, "synthetic");
            int length = Math.Min(PageLength, request.GetProperty("limit").GetInt32());
            int start = 0;
            int? previousBoundary = null;
            if (request.TryGetProperty("cursor", out var token))
            {
                string[] parts = token.GetString()!.Split(':');
                int boundary = int.Parse(parts[1], System.Globalization.CultureInfo.InvariantCulture);
                if (parts[0] == "p") previousBoundary = boundary;
                start = previousBoundary is not null ? Math.Max(0, boundary - length) : boundary;
            }
            int end = previousBoundary ?? Math.Min(total, start + length);
            var result = JsonSerializer.SerializeToNode(new
            {
                pageSessionId = "page-1", sortVersion = "precise-v1", total,
                previousCursor = start > 0 ? "p:" + start : null,
                nextCursor = end < total ? "n:" + end : null,
                items = Enumerable.Range(start, end - start).Select(i => new
                {
                    id = "duplicate-public-id", sender = "合成成员", timestamp = "2026-11-25T12:00:00.123456+08:00",
                    content = Content ?? "row-" + i, kind = "text", isOutgoing = false,
                }),
            })!.AsObject();
            if (Keyword)
            {
                result["sourceSessionId"] = "source-1";
                result["mode"] = "keyword";
                result["candidateCount"] = total + 49;
                result["order"] = "desc";
                result["projectionVersion"] = "canonical-appmsg-v1";
            }
            Mutate?.Invoke(result);
            return Task.FromResult(JsonSerializer.SerializeToElement(result));
        }
    }
}
