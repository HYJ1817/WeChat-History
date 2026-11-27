using System.Text.Json;
using WechatMemory.NativeApp.Services;
using Xunit;

namespace NativeApp.Tests;

public class ProtocolTests
{
    [Fact]
    public void RequestUsesV1AndKeepsIdAndUnicode()
    {
        var wire = Protocol.Request("req-😀", "hello", new { text = "中文\nsecond" });
        using var doc = JsonDocument.Parse(wire);
        Assert.Equal(1, doc.RootElement.GetProperty("protocolVersion").GetInt32());
        Assert.Equal("req-😀", doc.RootElement.GetProperty("requestId").GetString());
        Assert.Equal("中文\nsecond", doc.RootElement.GetProperty("payload").GetProperty("text").GetString());
        Assert.DoesNotContain('\n', wire);
    }
    [Fact]
    public void SuccessOwnsItsJsonLifetime()
    {
        var r = Protocol.ReadResponse("{\"protocolVersion\":1,\"requestId\":\"r\",\"type\":\"response\",\"ok\":true,\"result\":{\"x\":42}}", "r");
        Assert.Equal(42, r!.Value.GetProperty("x").GetInt32());
    }
    [Theory]
    [InlineData("{\"protocolVersion\":1,\"requestId\":\"other\",\"type\":\"response\",\"ok\":true,\"result\":{}}")]
    [InlineData("{\"protocolVersion\":2,\"requestId\":\"r\",\"type\":\"response\",\"ok\":true,\"result\":{}}")]
    [InlineData("{}")] [InlineData("not json")]
    public void MismatchedAndMalformedResponsesFail(string wire) => Assert.Equal("PROTOCOL_ERROR", Assert.Throws<WorkerException>(() => Protocol.ReadResponse(wire,"r")).Code);

    [Fact]
    public void ErrorIsNotSuccess()
    {
        var ex = Assert.Throws<WorkerException>(() => Protocol.ReadResponse("{\"protocolVersion\":1,\"requestId\":\"r\",\"type\":\"response\",\"ok\":false,\"error\":{\"code\":\"OUTPUT_EXISTS\",\"message\":\"文件存在\"}}", "r"));
        Assert.Equal("OUTPUT_EXISTS", ex.Code); Assert.Equal("文件存在", ex.Message);
    }
    [Fact]
    public void ProgressIsNotAFinalResult() => Assert.Null(Protocol.ReadResponse("{\"protocolVersion\":1,\"requestId\":\"r\",\"type\":\"progress\"}", "r"));

    [Fact]
    public void DatesIncludeTheEndDayAndIgnoreMachineZone()
    {
        var range = DateRange.Create(new DateTimeOffset(2026,11,24,0,0,0,TimeSpan.FromHours(-5)), new DateTimeOffset(2026,11,24,0,0,0,TimeSpan.FromHours(-5)));
        var bounds = range.Bounds();
        Assert.Equal("2026-11-24", range.StartDate);
        Assert.Equal(TimeSpan.FromHours(8), bounds.Start!.Value.Offset);
        Assert.Equal(new DateTimeOffset(2026,11,25,0,0,0,TimeSpan.FromHours(8)), bounds.EndExclusive);
    }
    [Fact] public void OpenDatesHaveOpenBounds() => Assert.Equal((null, null), DateRange.Create(null, null).Bounds());
    [Fact] public void ReversedDatesAreRejected() => Assert.Throws<WorkerException>(() => DateRange.Create(new DateTimeOffset(2026,11,25,0,0,0,TimeSpan.Zero), new DateTimeOffset(2026,11,24,0,0,0,TimeSpan.Zero)));
}
