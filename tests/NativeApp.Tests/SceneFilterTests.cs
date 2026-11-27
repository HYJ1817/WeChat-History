using System.Text.Json;
using WechatMemory.NativeApp.Models;
using WechatMemory.NativeApp.Services;
using Xunit;

namespace NativeApp.Tests;

public class SceneFilterTests
{
    private static readonly DateTimeOffset Now =
        new(2026, 11, 27, 15, 30, 0, TimeSpan.FromHours(8));

    [Fact]
    public void WorkerScenePayloadDeserializesItsSnakeCaseFields()
    {
        // Shape copied from wechat_context_exporter.scenes.models.Scene.to_dict
        // (task service adds "builtin"). System.Text.Json's camelCase policy does
        // not map snake_case, so the explicit names are part of the protocol.
        const string json = """
        {
          "id": "decision-review",
          "name": "项目决策复盘",
          "description": "把一段时间的讨论还原成可追溯的决策记录。",
          "conversation_id": "wxid_demo",
          "days": 30,
          "start": null,
          "end": null,
          "query": "项目",
          "include_image_pages": false,
          "markdown": true,
          "json_output": false,
          "target": "obsidian",
          "prompt": "总结",
          "shortcut": 2,
          "builtin": true
        }
        """;

        var scene = JsonSerializer.Deserialize<SceneItem>(json, Protocol.Json);

        Assert.NotNull(scene);
        Assert.Equal("wxid_demo", scene!.ConversationId);
        Assert.Equal(30, scene.Days);
        Assert.False(scene.IncludeImagePages);
        Assert.False(scene.JsonOutput);
        Assert.Equal("obsidian", scene.Target);
        Assert.True(scene.Builtin);
    }

    [Fact]
    public void DaysSceneUsesInclusiveCalendarDaysEndingToday()
    {
        var scene = new SceneItem("custom-1", "最近七天", Days: 7);

        Assert.True(SceneWindowResolver.TryResolve(scene, Now, out var window, out var error), error);

        Assert.Equal(new DateOnly(2026, 11, 21), window.Start);
        Assert.Equal(new DateOnly(2026, 11, 27), window.End);
        Assert.Equal(new DateTimeOffset(2026, 11, 21, 0, 0, 0, TimeSpan.FromHours(8)), window.StartDate);
        Assert.Equal(new DateTimeOffset(2026, 11, 27, 0, 0, 0, TimeSpan.FromHours(8)), window.EndDate);
    }

    [Fact]
    public void ExplicitSceneWindowWinsOverDays()
    {
        var scene = new SceneItem("custom-2", "固定区间",
            Days: 30,
            Start: "2026-11-01T08:30:00",
            End: "2026-11-24T23:59:59");

        Assert.True(SceneWindowResolver.TryResolve(scene, Now, out var window, out var error), error);

        Assert.Equal(new DateOnly(2026, 11, 1), window.Start);
        Assert.Equal(new DateOnly(2026, 11, 24), window.End);
    }

    [Fact]
    public void ExplicitSceneWindowKeepsAnOpenSide()
    {
        var startOnly = new SceneItem("custom-3", "开始", Days: 30, Start: "2026-11-01");
        Assert.True(SceneWindowResolver.TryResolve(startOnly, Now, out var started, out var startError), startError);
        Assert.Equal(new DateOnly(2026, 11, 1), started.Start);
        Assert.Null(started.End);

        var endOnly = new SceneItem("custom-4", "结束", End: "2026-11-24");
        Assert.True(SceneWindowResolver.TryResolve(endOnly, Now, out var ended, out var endError), endError);
        Assert.Null(ended.Start);
        Assert.Equal(new DateOnly(2026, 11, 24), ended.End);
    }

    [Fact]
    public void SceneWithoutAWindowClearsTheDateSelection()
    {
        var scene = new SceneItem("custom-5", "全部日期", Query: "回复");

        Assert.True(SceneWindowResolver.TryResolve(scene, Now, out var window, out var error), error);

        Assert.Null(window.Start);
        Assert.Null(window.End);
        Assert.Null(window.StartDate);
        Assert.Null(window.EndDate);
    }

    [Fact]
    public void InvalidSceneWindowIsRejectedWithASafeMessage()
    {
        var reversed = new SceneItem("custom-6", "反向", Start: "2026-11-25", End: "2026-11-24");
        Assert.False(SceneWindowResolver.TryResolve(reversed, Now, out _, out var rangeError));
        Assert.Contains("开始日期", rangeError);

        var badDate = new SceneItem("custom-7", "坏日期", Start: "not-a-date");
        Assert.False(SceneWindowResolver.TryResolve(badDate, Now, out _, out var dateError));
        Assert.Contains("开始日期", dateError);

        var badDays = new SceneItem("custom-8", "零天", Days: 0);
        Assert.False(SceneWindowResolver.TryResolve(badDays, Now, out _, out var daysError));
        Assert.Contains("天数", daysError);
    }

    [Fact]
    public void SavedSceneKeepsTheSelectedWindowAndProtocolFieldNames()
    {
        var start = new DateTimeOffset(2026, 11, 24, 0, 0, 0, TimeSpan.FromHours(-5));
        var end = new DateTimeOffset(2026, 11, 24, 0, 0, 0, TimeSpan.FromHours(-5));

        var payload = SceneDraft.Create(
            "custom-1", "固定区间", start, end, " 项目 ",
            includeImagePages: true, markdown: true, jsonOutput: false,
            target: "obsidian", prompt: "提示词");

        using var document = JsonDocument.Parse(JsonSerializer.Serialize(payload, Protocol.Json));
        var root = document.RootElement;

        Assert.Equal("2026-11-24", root.GetProperty("start").GetString());
        Assert.Equal("2026-11-24", root.GetProperty("end").GetString());
        Assert.Equal(JsonValueKind.Null, root.GetProperty("days").ValueKind);
        Assert.True(root.GetProperty("include_image_pages").GetBoolean());
        Assert.True(root.GetProperty("markdown").GetBoolean());
        Assert.False(root.GetProperty("json_output").GetBoolean());
        Assert.Equal("obsidian", root.GetProperty("target").GetString());
        Assert.Equal(" 项目 ", root.GetProperty("query").GetString());
        Assert.Equal("提示词", root.GetProperty("prompt").GetString());

        var roundTrip = JsonSerializer.Deserialize<SceneItem>(
            JsonSerializer.Serialize(payload, Protocol.Json), Protocol.Json);
        Assert.NotNull(roundTrip);
        Assert.Equal("2026-11-24", roundTrip!.Start);
        Assert.Equal("2026-11-24", roundTrip.End);
    }

    [Fact]
    public void SavedSceneWithoutDatesWritesAllWindowFieldsAsNull()
    {
        var payload = SceneDraft.Create(
            "custom-1", "全部日期", null, null, null,
            includeImagePages: false, markdown: true, jsonOutput: true,
            target: "folder", prompt: "提示词");

        using var document = JsonDocument.Parse(JsonSerializer.Serialize(payload, Protocol.Json));
        var root = document.RootElement;

        Assert.Equal(JsonValueKind.Null, root.GetProperty("days").ValueKind);
        Assert.Equal(JsonValueKind.Null, root.GetProperty("start").ValueKind);
        Assert.Equal(JsonValueKind.Null, root.GetProperty("end").ValueKind);
        Assert.False(root.GetProperty("include_image_pages").GetBoolean());
    }

    [Fact]
    public void SavedSceneRejectsReversedDates()
    {
        var error = Assert.Throws<WorkerException>(() => SceneDraft.Create(
            "custom-1", "反向",
            new DateTimeOffset(2026, 11, 25, 0, 0, 0, TimeSpan.FromHours(8)),
            new DateTimeOffset(2026, 11, 24, 0, 0, 0, TimeSpan.FromHours(8)),
            null, true, true, true, "folder", "提示词"));

        Assert.Equal("INVALID_ARGUMENT", error.Code);
    }

    [Fact]
    public void GeneratedSceneIdMatchesTheWorkerIdPattern()
    {
        var id = SceneDraft.NewId(Now);

        Assert.Matches("^[a-z0-9][a-z0-9._-]{0,39}$", id);
        Assert.StartsWith("custom-", id);
    }

    [Fact]
    public void FailedDeliveryIsNotSuccessEvenThoughTheResponseIsOk()
    {
        const string json = """
        {
          "job": { "jobId": "job-1", "status": "failed" },
          "delivery": {
            "status": "failed",
            "target": "folder",
            "kind": "folder",
            "destination": null,
            "detail": "投递失败，任务已保留，可稍后重试。",
            "error": "投递失败，任务已保留，可稍后重试。",
            "code": "DELIVERY_FAILED"
          }
        }
        """;
        using var document = JsonDocument.Parse(json);

        var outcome = DeliveryOutcome.Parse(document.RootElement);

        Assert.True(outcome.Failed);
        Assert.False(outcome.Succeeded);
        var error = Assert.Throws<WorkerException>(outcome.ThrowIfFailed);
        Assert.Equal("DELIVERY_FAILED", error.Code);
        Assert.Equal("投递失败，任务已保留，可稍后重试。", error.Message);
    }

    [Theory]
    [InlineData("delivered")]
    [InlineData("copied")]
    [InlineData("none")]
    public void SuccessfulDeliveryStatusesAreAccepted(string status)
    {
        var json = $$"""
        { "delivery": { "status": "{{status}}", "target": "folder", "detail": "" } }
        """;
        using var document = JsonDocument.Parse(json);

        var outcome = DeliveryOutcome.Parse(document.RootElement);

        Assert.True(outcome.Succeeded);
        Assert.False(outcome.Failed);
        outcome.ThrowIfFailed();
    }

    [Fact]
    public void MissingOrUnknownDeliveryIsAProtocolError()
    {
        using var missing = JsonDocument.Parse("""{ "job": { "jobId": "job-1" } }""");
        var missingError = Assert.Throws<WorkerException>(() => DeliveryOutcome.Parse(missing.RootElement));
        Assert.Equal("PROTOCOL_ERROR", missingError.Code);

        using var unknown = JsonDocument.Parse("""{ "delivery": { "status": "partial" } }""");
        var unknownError = Assert.Throws<WorkerException>(() => DeliveryOutcome.Parse(unknown.RootElement));
        Assert.Equal("PROTOCOL_ERROR", unknownError.Code);
    }

    [Fact]
    public void DeliveryFailureFallsBackToDetailOrAFixedMessage()
    {
        using var detailOnly = JsonDocument.Parse("""
            { "delivery": { "status": "failed", "detail": "目标笔记已存在，请选择其他 vault 或先移动旧笔记。" } }
            """);
        var fromDetail = DeliveryOutcome.Parse(detailOnly.RootElement);
        Assert.Equal("目标笔记已存在，请选择其他 vault 或先移动旧笔记。", fromDetail.FailureMessage);

        using var bare = JsonDocument.Parse("""{ "delivery": { "status": "failed" } }""");
        var fallback = DeliveryOutcome.Parse(bare.RootElement);
        Assert.Equal("投递失败，任务已保留，可稍后重试。", fallback.FailureMessage);
        Assert.Equal("DELIVERY_FAILED", Assert.Throws<WorkerException>(fallback.ThrowIfFailed).Code);
    }

    [Fact]
    public void ImageOptionNeedsReconnectOnlyWhenTheConnectedSourceLacksImages()
    {
        var withoutImages = new ConnectionMedia(
            ImagesRequested: false, ImageKeyAvailable: false,
            VoiceRequested: false, VoiceAvailable: false);
        Assert.True(withoutImages.NeedsImageReconnect(enableImages: true));
        Assert.False(withoutImages.NeedsImageReconnect(enableImages: false));

        var withImages = new ConnectionMedia(true, true, false, false);
        Assert.False(withImages.NeedsImageReconnect(enableImages: true));
        Assert.False(withImages.NeedsImageReconnect(enableImages: false));

        var requestedButNoKey = new ConnectionMedia(true, false, false, false);
        Assert.True(requestedButNoKey.NeedsImageReconnect(enableImages: true));
    }

    [Fact]
    public void VoiceReadinessFollowsTheConnectCapability()
    {
        var withoutVoice = new ConnectionMedia(true, true, VoiceRequested: false, VoiceAvailable: false);
        Assert.False(withoutVoice.VoiceReady);
        Assert.True(withoutVoice.NeedsVoiceReconnect(enableVoice: true));

        var withVoice = new ConnectionMedia(false, false, VoiceRequested: true, VoiceAvailable: true);
        Assert.True(withVoice.VoiceReady);
        Assert.False(withVoice.NeedsVoiceReconnect(enableVoice: true));
    }
}
