using Microsoft.UI.Xaml;

namespace WechatMemory.NativeApp;

public partial class App : Application
{
    private Window? _window;
    public App()
    {
        InitializeComponent();
        Services.StartupOptions.Parse(Environment.GetCommandLineArgs());
        UnhandledException += (_, args) => Services.AppLog.Write("unhandled_exception", args.Exception.GetType().Name);
    }
    protected override void OnLaunched(LaunchActivatedEventArgs args)
    {
        _window = new MainWindow();
        _window.Activate();
    }
}
