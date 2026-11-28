using System.Diagnostics;
using System.Text;
using System.Text.Json;

namespace WechatMemory.NativeApp.Services;

// One dedicated child, one outstanding request, two independently drained pipes.
// This file is UI-independent and compiled directly into NativeApp.Tests.
public sealed class WorkerClient : IAsyncDisposable, IWorkerRequestClient
{
    private readonly Process _process;
    private readonly SemaphoreSlim _serial = new(1, 1);
    private readonly object _sync = new();
    private readonly Task _stdout;
    private readonly Task _stderr;
    private TaskCompletionSource<JsonElement>? _pending;
    private string? _pendingId;
    private WorkerException? _terminal;
    private string? _tempRoot;
    private bool _closing;
    private bool _disposed;
    public int ProcessId => _process.Id;
    public string? TempRootPath => _tempRoot;
    public bool IsAvailable { get { lock (_sync) return _terminal is null && !_closing; } }
    public event Action<WorkerException>? Disconnected;
    public event Action? DiagnosticReceived;
    public event Action<WorkerProgress>? ProgressReceived;

    public static WorkerClient Start(ProcessStartInfo info)
    {
        info.UseShellExecute = false;
        info.CreateNoWindow = true;
        info.RedirectStandardInput = info.RedirectStandardOutput = info.RedirectStandardError = true;
        info.StandardInputEncoding = info.StandardOutputEncoding = info.StandardErrorEncoding = new UTF8Encoding(false, true);
        info.Environment["PYTHONUTF8"] = "1";
        // A unique root makes cleanup reliable even when the worker is killed
        // mid-export: Python cannot run its own cleanup in that case.
        string? tempRoot = CreateTempRoot();
        if (tempRoot is not null) info.Environment["WECHAT_MEMORY_TEMP_ROOT"] = tempRoot;
        try
        {
            var process = new Process { StartInfo = info, EnableRaisingEvents = true };
            if (!process.Start()) throw new InvalidOperationException("Process.Start returned false.");
            return new WorkerClient(process, tempRoot);
        }
        catch (Exception e) when (e is System.ComponentModel.Win32Exception or IOException or InvalidOperationException)
        {
            DeleteTempRoot(tempRoot);
            throw new WorkerException("WORKER_START_FAILED", "无法启动 Python 工作进程。请运行环境准备脚本，或检查发布目录是否完整。", e);
        }
    }

    private static string? CreateTempRoot()
    {
        try
        {
            string local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
            if (string.IsNullOrWhiteSpace(local)) local = Path.GetTempPath();
            string parent = Path.Combine(local, "WechatMemoryNative", "worker-temp");
            Directory.CreateDirectory(parent);
            string root = Path.Combine(parent, "worker-" + Guid.NewGuid().ToString("N")[..12]);
            Directory.CreateDirectory(root);
            return root;
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException or NotSupportedException)
        {
            // Without a private root the worker falls back to its own location;
            // that is still safe, only less prompt to clean after a kill.
            return null;
        }
    }

    private static void DeleteTempRoot(string? root)
    {
        if (root is null) return;
        for (int attempt = 0; attempt < 5; attempt++)
        {
            try
            {
                if (Directory.Exists(root)) Directory.Delete(root, recursive: true);
                return;
            }
            catch (Exception e) when (e is IOException or UnauthorizedAccessException)
            {
                Thread.Sleep(100 * (attempt + 1));
            }
        }
    }

    private WorkerClient(Process process, string? tempRoot)
    {
        _process = process;
        _tempRoot = tempRoot;
        _stdout = ReadOutputAsync();
        _stderr = ReadErrorAsync();
        // EOF on stdout handles pending responses, including an exit racing the last response.
        // Monitoring the process also handles workers that close stdout but continue running.
        _ = ObserveExitAsync();
    }

    private async Task ObserveExitAsync()
    {
        try { await _process.WaitForExitAsync().ConfigureAwait(false); await _stdout.ConfigureAwait(false); }
        catch (ObjectDisposedException) { return; }
        if (!_closing)
        {
            Fail(new("WORKER_EXITED", "Python 工作进程意外退出，请重新连接。"));
            DeleteTempRoot(_tempRoot);
        }
    }

    public async Task<JsonElement> RequestAsync(string command, object payload, TimeSpan? timeout = null)
    {
        await _serial.WaitAsync().ConfigureAwait(false);
        try
        {
            TaskCompletionSource<JsonElement> completion = new(TaskCreationOptions.RunContinuationsAsynchronously);
            string id = Guid.NewGuid().ToString("N");
            lock (_sync)
            {
                if (_terminal is not null) throw _terminal;
                if (_disposed) throw new WorkerException("WORKER_EXITED", "工作进程已关闭。");
                _pending = completion;
                _pendingId = id;
            }
            try
            {
                var deadline = timeout ?? TimeSpan.FromSeconds(30);
                using var timer = new CancellationTokenSource(deadline);
                await _process.StandardInput.WriteLineAsync(Protocol.Request(id, command, payload).AsMemory(), timer.Token).ConfigureAwait(false);
                await _process.StandardInput.FlushAsync(timer.Token).ConfigureAwait(false);
                return await completion.Task.WaitAsync(timer.Token).ConfigureAwait(false);
            }
            catch (OperationCanceledException e)
            {
                var failure = new WorkerException("REQUEST_TIMEOUT", "工作进程响应超时，任务已停止。请重新连接后重试。", e);
                Fail(failure);
                KillWorkerProcess();
                try { await ReapAsync().ConfigureAwait(false); }
                finally { DeleteTempRoot(_tempRoot); }
                throw failure;
            }
            catch (Exception e) when (e is IOException or ObjectDisposedException or InvalidOperationException)
            {
                var failure = new WorkerException("WORKER_EXITED", "无法连接工作进程，请重新连接。", e);
                Fail(failure);
                throw failure;
            }
            finally
            {
                lock (_sync) { _pending = null; _pendingId = null; }
            }
        }
        finally { _serial.Release(); }
    }

    private async Task ReadOutputAsync()
    {
        try
        {
            while (await _process.StandardOutput.ReadLineAsync().ConfigureAwait(false) is { } line)
            {
                TaskCompletionSource<JsonElement>? completion;
                string? id;
                lock (_sync) { completion = _pending; id = _pendingId; }
                if (completion is null || id is null)
                    throw new WorkerException("PROTOCOL_ERROR", "工作进程发出了未关联的输出，请重新连接。");
                if (Protocol.TryReadProgress(line, id, out var progress))
                {
                    ProgressReceived?.Invoke(progress);
                    continue;
                }
                try
                {
                    var result = Protocol.ReadResponse(line, id);
                    if (result is { } value && !completion.TrySetResult(value))
                        throw new WorkerException("PROTOCOL_ERROR", "工作进程重复返回了结果，请重新连接。");
                }
                catch (WorkerException e) when (e.Code != "PROTOCOL_ERROR") { completion.TrySetException(e); }
            }
            if (!_closing) Fail(new("WORKER_EXITED", "Python 工作进程连接已关闭，请重新连接。"));
        }
        catch (Exception e) when (e is IOException or DecoderFallbackException or WorkerException or ObjectDisposedException)
        {
            if (!_closing)
            {
                Fail(e as WorkerException ?? new("PROTOCOL_ERROR", "读取工作进程响应失败，请重新连接。", e));
                KillWorkerProcess();
                DeleteTempRoot(_tempRoot);
            }
        }
    }

    private async Task ReadErrorAsync()
    {
        try
        {
            // Deliberately do NOT forward stderr text: third-party diagnostics may contain data.
            while (await _process.StandardError.ReadLineAsync().ConfigureAwait(false) is not null)
                DiagnosticReceived?.Invoke();
        }
        catch (Exception e) when (e is IOException or ObjectDisposedException or DecoderFallbackException) { }
    }

    private void Fail(WorkerException failure)
    {
        bool notify;
        lock (_sync)
        {
            notify = _terminal is null;
            _terminal ??= failure;
            _pending?.TrySetException(_terminal);
        }
        if (notify && !_closing) Disconnected?.Invoke(failure);
    }

    private void KillWorkerProcess()
    {
        // Deliberately not entireProcessTree: the worker starts Weixin.exe as
        // an ordinary child. Killing the tree would also kill the user's
        // WeChat (and any unsent draft) whenever an export times out or the
        // window closes. Child helpers are short-lived and exit on their own.
        try { if (!_process.HasExited) _process.Kill(); }
        catch (Exception e) when (e is InvalidOperationException or System.ComponentModel.Win32Exception) { }
    }

    private async Task ReapAsync()
    {
        try { await _process.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(3)).ConfigureAwait(false); }
        catch (TimeoutException) { throw new WorkerException("WORKER_EXITED", "工作进程未能及时退出。"); }
        catch (InvalidOperationException) { }
    }

    public async ValueTask DisposeAsync()
    {
        if (_disposed) return;
        _closing = true;
        try
        {
            bool hasPending;
            lock (_sync) hasPending = _pending is not null;
            // A running task cannot be interrupted by a second serial request. Stop the owned
            // process first instead of waiting up to the export timeout on window close.
            if (hasPending) KillWorkerProcess();
            else if (IsProcessAlive() && _terminal is null)
                await RequestAsync("shutdown", new { }, TimeSpan.FromSeconds(2)).ConfigureAwait(false);
        }
        catch (WorkerException) { }
        finally
        {
            Fail(new("WORKER_EXITED", "工作进程已关闭。"));
            _disposed = true;
            KillWorkerProcess();
            try { await ReapAsync().ConfigureAwait(false); }
            finally { DeleteTempRoot(_tempRoot); }
            await Task.WhenAll(_stdout, _stderr).ConfigureAwait(false);
            _process.Dispose();
        }
    }

    private bool IsProcessAlive()
    {
        try { return !_process.HasExited; }
        catch (InvalidOperationException) { return false; }
    }
}
