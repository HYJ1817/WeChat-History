namespace WechatMemory.NativeApp.Services;

/// <summary>
/// Command-line switches. The published app is real-WeChat-only; <c>--demo</c>
/// exists solely for automated UI tests and never ships demo data with the app.
/// </summary>
public static class StartupOptions
{
    public static bool DemoMode { get; private set; }
    public static string? DemoPath { get; private set; }

    public static void Parse(IReadOnlyList<string> args)
    {
        for (int index = 1; index < args.Count; index++)
        {
            if (!string.Equals(args[index], "--demo", StringComparison.OrdinalIgnoreCase)) continue;
            DemoMode = true;
            if (index + 1 < args.Count && !args[index + 1].StartsWith("--", StringComparison.Ordinal))
            {
                DemoPath = args[index + 1];
                index++;
            }
        }
    }
}
