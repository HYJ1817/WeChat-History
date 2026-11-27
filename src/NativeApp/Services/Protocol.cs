using System.Text.Json;

namespace WechatMemory.NativeApp.Services;

public sealed class WorkerException(string code, string message, Exception? inner = null) : Exception(message, inner)
{
    public string Code { get; } = code;
}

public sealed record WorkerProgress(int Current, int Total, string Label);

public static class Protocol
{
    public const int Version = 1;
    public static readonly JsonSerializerOptions Json = new(JsonSerializerDefaults.Web);

    public static string Request(string requestId, string command, object payload) => JsonSerializer.Serialize(
        new { protocolVersion = Version, requestId, type = "request", command, payload }, Json);

    public static bool TryReadProgress(string line, string expectedId, out WorkerProgress progress)
    {
        progress = new WorkerProgress(0, 0, "");
        try
        {
            using var doc = JsonDocument.Parse(line);
            var root = doc.RootElement;
            if (root.GetProperty("protocolVersion").GetInt32() != Version ||
                root.GetProperty("requestId").GetString() != expectedId ||
                root.GetProperty("type").GetString() != "progress")
                return false;
            progress = new WorkerProgress(
                root.TryGetProperty("current", out var current) ? current.GetInt32() : 0,
                root.TryGetProperty("total", out var total) ? total.GetInt32() : 0,
                root.TryGetProperty("label", out var label) ? label.GetString() ?? "" : "");
            return true;
        }
        catch (Exception e) when (e is JsonException or KeyNotFoundException or InvalidOperationException or FormatException)
        {
            return false;
        }
    }

    public static JsonElement? ReadResponse(string line, string expectedId)
    {
        try
        {
            using var doc = JsonDocument.Parse(line);
            var root = doc.RootElement;
            if (root.GetProperty("protocolVersion").GetInt32() != Version ||
                root.GetProperty("requestId").GetString() != expectedId)
                throw new WorkerException("PROTOCOL_ERROR", "工作进程返回了不匹配的响应，请重新连接。");
            var type = root.GetProperty("type").GetString();
            if (type == "progress") return null;
            if (type != "response") throw new JsonException("Expected final response.");
            if (!root.GetProperty("ok").GetBoolean())
            {
                var error = root.GetProperty("error");
                throw new WorkerException(error.GetProperty("code").GetString() ?? "EXPORT_FAILED",
                    error.GetProperty("message").GetString() ?? "工作进程执行失败。");
            }
            return root.GetProperty("result").Clone();
        }
        catch (WorkerException) { throw; }
        catch (Exception e) when (e is JsonException or KeyNotFoundException or InvalidOperationException or FormatException)
        {
            throw new WorkerException("PROTOCOL_ERROR", "工作进程响应格式不正确，请重新连接。", e);
        }
    }
}
