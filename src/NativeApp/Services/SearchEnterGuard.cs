namespace WechatMemory.NativeApp.Services;

/// <summary>Enter commits an IME candidate before it can execute a search.</summary>
public sealed class SearchEnterGuard
{
    private bool _composing;
    private bool _enterConsumed;

    public void CompositionStarted() => _composing = true;

    public void CompositionEnded(bool enterIsDown)
    {
        _composing = false;
        // Some IMEs raise CompositionEnded before routing the committing KeyDown.
        _enterConsumed |= enterIsDown;
    }

    public bool TryExecuteEnter()
    {
        bool execute = !_composing && !_enterConsumed;
        _enterConsumed = true; // Also suppress key repeat until the key is released.
        return execute;
    }

    public void EnterReleased() => _enterConsumed = false;
    public void Reset() { _composing = false; _enterConsumed = false; }
}
