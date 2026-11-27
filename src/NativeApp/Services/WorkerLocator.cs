using System.Diagnostics;

namespace WechatMemory.NativeApp.Services;

public static class WorkerLocator
{
    public static ProcessStartInfo Locate()
    {
        string basePath = AppContext.BaseDirectory;
        string bundled = Path.Combine(basePath, "worker", "WechatMemory.Worker.exe");
        if (File.Exists(bundled))
        {
            var info = new ProcessStartInfo(bundled) { WorkingDirectory = basePath };
            AddDemoArguments(info);
            return info;
        }
        // Only inspect explicit project markers, not user profiles or WeChat locations.
        for (DirectoryInfo? dir = new(basePath); dir is not null; dir = dir.Parent)
        {
            string script = Path.Combine(dir.FullName, "src", "PythonWorker", "worker.py");
            if (!File.Exists(script) || !File.Exists(Path.Combine(dir.FullName, "global.json"))) continue;
            string python = Path.Combine(dir.FullName, ".venv", "Scripts", "python.exe");
            if (!File.Exists(python)) throw new WorkerException("WORKER_START_FAILED", "未找到项目的 Python 环境，请先运行 scripts/Prepare-Python.ps1。");
            var info = new ProcessStartInfo(python) { WorkingDirectory = dir.FullName };
            foreach (var arg in new[] { "-I", "-u", script }) info.ArgumentList.Add(arg);
            AddDemoArguments(info, Path.Combine(dir.FullName, "demo", "conversations.json"));
            return info;
        }
        throw new WorkerException("WORKER_START_FAILED", "未找到独立工作进程。请保留完整发布文件夹，不要只复制主程序 EXE。");
    }

    private static void AddDemoArguments(ProcessStartInfo info, string? fallback = null)
    {
        if (!StartupOptions.DemoMode) return;
        string? demo = StartupOptions.DemoPath;
        if (string.IsNullOrWhiteSpace(demo)) demo = fallback;
        if (string.IsNullOrWhiteSpace(demo)) return;
        info.ArgumentList.Add("--demo");
        info.ArgumentList.Add(demo);
    }
}
