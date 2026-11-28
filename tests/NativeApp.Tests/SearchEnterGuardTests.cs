using WechatMemory.NativeApp.Services;
using Xunit;

namespace NativeApp.Tests;

public sealed class SearchEnterGuardTests
{
    [Fact]
    public void OrdinaryEnterExecutesOnceAndDoesNotRepeatWhileHeld()
    {
        var guard = new SearchEnterGuard();
        Assert.True(guard.TryExecuteEnter());
        Assert.False(guard.TryExecuteEnter());
        guard.EnterReleased();
        Assert.True(guard.TryExecuteEnter());
    }

    [Fact]
    public void EnterDuringCompositionOnlyCommitsTheCandidate()
    {
        var guard = new SearchEnterGuard();
        guard.CompositionStarted();
        Assert.False(guard.TryExecuteEnter());
        guard.CompositionEnded(enterIsDown: true);
        Assert.False(guard.TryExecuteEnter());
        guard.EnterReleased();
        Assert.True(guard.TryExecuteEnter());
    }

    [Fact]
    public void CompositionEndedBeforeKeyDownStillDoesNotTurnCommitIntoSearch()
    {
        var guard = new SearchEnterGuard();
        guard.CompositionStarted();
        guard.CompositionEnded(enterIsDown: true);
        Assert.False(guard.TryExecuteEnter());
        guard.EnterReleased();
        Assert.True(guard.TryExecuteEnter());
    }

    [Fact]
    public void MouseCommitDoesNotSwallowTheNextOrdinaryEnterAndFocusLossResetsState()
    {
        var guard = new SearchEnterGuard();
        guard.CompositionStarted();
        guard.CompositionEnded(enterIsDown: false);
        Assert.True(guard.TryExecuteEnter());
        guard.CompositionStarted();
        guard.Reset();
        Assert.True(guard.TryExecuteEnter());
    }
}
