using System.Text;

namespace WechatMemory.NativeApp.Services;

public static class AppLog
{
    private static readonly object Sync = new();
    public static string DirectoryPath { get; } = Path.Combine(AppContext.BaseDirectory, "logs");
    public static bool Write(string eventName, string code = "OK")
    {
        // Call sites supply only constant event names and known error codes. Never log
        // requests, paths, exception.Message, stderr, sender names or message bodies.
        lock (Sync)
        {
            try
            {
                Directory.CreateDirectory(DirectoryPath);
                string path = Path.Combine(DirectoryPath, $"native-{DateTime.UtcNow:yyyy-MM-dd}.log");
                if (File.Exists(path) && new FileInfo(path).Length > 1024 * 1024) File.WriteAllText(path, "", Encoding.UTF8);
                File.AppendAllText(path, $"{DateTimeOffset.UtcNow:O} {eventName} {code}{Environment.NewLine}", new UTF8Encoding(false));
                return true;
            }
            catch (Exception e) when (e is IOException or UnauthorizedAccessException) { return false; }
        }
    }
}
