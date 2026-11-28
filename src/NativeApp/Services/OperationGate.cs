namespace WechatMemory.NativeApp.Services;

/// <summary>
/// A small, platform-independent single-operation gate shared by regular VM
/// operations and UI workflows that remain open across modal dialogs.
/// </summary>
public sealed class OperationGate
{
    private int _occupied;

    public bool IsOccupied => Volatile.Read(ref _occupied) != 0;

    public bool CanStart(bool isBusy, bool closing) => !isBusy && !closing && !IsOccupied;

    public bool CanReconnect(bool isBusy, bool closing) => CanStart(isBusy, closing);

    public bool CanImportJson(bool isBusy, bool closing, bool workerAvailable) =>
        CanStart(isBusy, closing) && workerAvailable;

    public bool CanExport(bool isBusy, bool closing, bool connected, bool hasConversation, bool filtersClean) =>
        CanStart(isBusy, closing) && connected && hasConversation && filtersClean;

    public bool CanRetry(bool isBusy, bool closing, bool workerAvailable, bool retryable) =>
        CanStart(isBusy, closing) && workerAvailable && retryable;

    public bool CanManageJobs(bool isBusy, bool closing, bool workerAvailable) =>
        CanStart(isBusy, closing) && workerAvailable;

    public bool CanConnectWechat(bool isBusy, bool closing, bool connected, bool hasAccount) =>
        CanStart(isBusy, closing) && !connected && hasAccount;

    public bool CanDeleteScene(bool isBusy, bool closing, bool hasCustomScene) =>
        CanStart(isBusy, closing) && hasCustomScene;

    public IDisposable? TryEnter()
    {
        if (Interlocked.CompareExchange(ref _occupied, 1, 0) != 0) return null;
        return new Lease(this);
    }

    private void Exit() => Volatile.Write(ref _occupied, 0);

    private sealed class Lease(OperationGate owner) : IDisposable
    {
        private OperationGate? _owner = owner;

        public void Dispose() => Interlocked.Exchange(ref _owner, null)?.Exit();
    }
}

/// <summary>
/// Keeps a conservative cleanup-result warning from being replaced by a late
/// disconnect callback belonging to the same worker instance.
/// </summary>
public sealed class DisconnectStatusGuard
{
    private object? _protectedWorker;

    public void Preserve(object worker) => Volatile.Write(ref _protectedWorker, worker);

    public bool ShouldPreserve(object worker) =>
        ReferenceEquals(Volatile.Read(ref _protectedWorker), worker);

    public void Clear(object worker) => Interlocked.CompareExchange(ref _protectedWorker, null, worker);
}
