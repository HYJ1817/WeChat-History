using Microsoft.UI.Dispatching;
using WechatMemory.NativeApp.Services;

namespace WechatMemory.NativeApp.ViewModels;

public partial class MainViewModel
{
    public MainViewModel(DispatcherQueue dispatcher)
        : this(action => { dispatcher.TryEnqueue(() => action()); },
            () => WorkerClient.Start(WorkerLocator.Locate()), StartupOptions.DemoMode)
    {
    }
}
