using System.Diagnostics;
using WechatMemory.NativeApp.Services;
using Xunit;

namespace NativeApp.Tests;

public class WorkerClientTests
{
    private static WorkerClient Start(string mode)
    {
        string? root = AppContext.BaseDirectory;
        while (root is not null && !File.Exists(Path.Combine(root,"global.json"))) root = Directory.GetParent(root)?.FullName;
        Assert.NotNull(root);
        string python = Path.Combine(root, ".venv", "Scripts", "python.exe");
        Assert.True(File.Exists(python), "Run scripts/Prepare-Python.ps1 before C# pipe integration tests.");
        var info = new ProcessStartInfo(python) { WorkingDirectory = root };
        foreach (var arg in new[] { "-I", "-X", "utf8", "-u", Path.Combine(AppContext.BaseDirectory,"fixtures","fake_worker.py"), mode }) info.ArgumentList.Add(arg);
        return WorkerClient.Start(info);
    }

    [Theory] [InlineData("normal")] [InlineData("noisy")] [InlineData("progress")]
    public async Task UnicodeAndBothPipesAreReadAsynchronously(string mode)
    {
        await using var client = Start(mode);
        var result = await client.RequestAsync("hello", new { });
        Assert.Equal("中文 😀", result.GetProperty("hello").GetString());
        Assert.True(client.IsAvailable);
    }
    [Fact]
    public async Task RemoteErrorDoesNotPoisonConnection()
    {
        await using var client = Start("error");
        var e = await Assert.ThrowsAsync<WorkerException>(() => client.RequestAsync("hello", new { }));
        Assert.Equal("OUTPUT_NOT_WRITABLE", e.Code); Assert.True(client.IsAvailable);
    }
    [Theory] [InlineData("exit","WORKER_EXITED")] [InlineData("badjson","PROTOCOL_ERROR")] [InlineData("mismatch","PROTOCOL_ERROR")]
    public async Task ExitAndProtocolViolationFailPendingRequestAndState(string mode, string code)
    {
        await using var client = Start(mode);
        var e = await Assert.ThrowsAsync<WorkerException>(() => client.RequestAsync("hello", new { }));
        Assert.Equal(code, e.Code); Assert.False(client.IsAvailable);
        await Assert.ThrowsAsync<WorkerException>(() => client.RequestAsync("hello", new { }));
    }
    [Fact]
    public async Task TimeoutKillsOnlyOwnedProcess()
    {
        await using var client = Start("hang");
        int pid = client.ProcessId;
        var e = await Assert.ThrowsAsync<WorkerException>(() => client.RequestAsync("hello", new { }, TimeSpan.FromMilliseconds(250)));
        Assert.Equal("REQUEST_TIMEOUT", e.Code); Assert.False(client.IsAvailable);
        Assert.False(IsAlive(pid));
    }
    [Fact]
    public async Task ClosingDuringPendingTaskDoesNotWaitForItsDeadline()
    {
        var client = Start("hang"); int pid = client.ProcessId;
        var request = client.RequestAsync("export_markdown", new { }, TimeSpan.FromSeconds(60));
        var clock = Stopwatch.StartNew();
        await client.DisposeAsync();
        await Assert.ThrowsAsync<WorkerException>(() => request);
        Assert.True(clock.Elapsed < TimeSpan.FromSeconds(6)); Assert.False(IsAlive(pid));
    }
    [Fact]
    public async Task ShutdownReapsDedicatedProcess()
    {
        var client = Start("normal"); int pid = client.ProcessId;
        await client.RequestAsync("hello", new { });
        await client.DisposeAsync(); Assert.False(IsAlive(pid));
    }
    [Fact]
    public async Task ShutdownRemovesTheWorkerPrivateTempRoot()
    {
        var client = Start("normal");
        string root = Assert.IsType<string>(client.TempRootPath);
        Assert.True(Directory.Exists(root));
        await client.RequestAsync("hello", new { });
        await client.DisposeAsync();
        Assert.False(Directory.Exists(root));
    }
    [Fact]
    public async Task TimeoutCleansTempRootButDoesNotKillProcessesTheWorkerStarted()
    {
        var client = Start("spawn");
        try
        {
            string root = Assert.IsType<string>(client.TempRootPath);
            string pidFile = Path.Combine(root, "child.pid");
            for (int i = 0; i < 40 && !File.Exists(pidFile); i++) Thread.Sleep(50);
            Assert.True(File.Exists(pidFile), "fake worker did not record its child");
            int childPid = int.Parse(File.ReadAllText(pidFile));
            var e = await Assert.ThrowsAsync<WorkerException>(() => client.RequestAsync("hello", new { }, TimeSpan.FromMilliseconds(300)));
            Assert.Equal("REQUEST_TIMEOUT", e.Code);
            Assert.True(IsAlive(childPid), "worker timeout must not kill the WeChat-like child");
            Assert.False(Directory.Exists(root));
            try { Process.GetProcessById(childPid).Kill(); } catch (ArgumentException) { }
        }
        finally
        {
            await client.DisposeAsync();
        }
    }
    [Fact]
    public void MissingExecutableHasClearStartError()
    {
        var e = Assert.Throws<WorkerException>(() => WorkerClient.Start(new ProcessStartInfo(Path.Combine(Path.GetTempPath(),Guid.NewGuid()+".exe"))));
        Assert.Equal("WORKER_START_FAILED", e.Code);
    }
    private static bool IsAlive(int pid)
    {
        try { using var p = Process.GetProcessById(pid); return !p.HasExited; }
        catch (ArgumentException) { return false; }
    }
}
