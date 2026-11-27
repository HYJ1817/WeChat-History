using Microsoft.UI.Windowing;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media;
using WechatMemory.NativeApp.Models;
using WechatMemory.NativeApp.Services;
using WechatMemory.NativeApp.ViewModels;
using Windows.Graphics;
using Windows.Storage;
using Windows.Storage.Pickers;
using Windows.System;
using Windows.UI;

namespace WechatMemory.NativeApp;

public sealed partial class MainWindow : Window
{
    public MainViewModel Vm { get; }
    private bool _loaded, _allowClose, _closeInProgress;
    private const int DwmwaUseImmersiveDarkMode = 20;
    private const int DwmwaUseImmersiveDarkModeBefore20H1 = 19;

    [System.Runtime.InteropServices.DllImport("user32.dll")]
    private static extern uint GetDpiForWindow(nint hwnd);

    [System.Runtime.InteropServices.DllImport("dwmapi.dll")]
    private static extern int DwmSetWindowAttribute(nint hwnd, int attribute, ref int value, int size);

    public MainWindow()
    {
        Vm = new MainViewModel(DispatcherQueue);
        InitializeComponent();
        Title = "微信记忆";
        ExtendsContentIntoTitleBar = true;
        SetTitleBar(TitleBarDragRegion);
        ApplyTitleBarTheme();
        Root.ActualThemeChanged += (_, _) => DispatcherQueue.TryEnqueue(ApplyTitleBarTheme);
        double scale = GetDpiForWindow(WinRT.Interop.WindowNative.GetWindowHandle(this)) / 96.0;
        var workArea = DisplayArea.GetFromWindowId(AppWindow.Id, DisplayAreaFallback.Primary).WorkArea;
        AppWindow.Resize(new SizeInt32(Math.Min((int)(1260 * scale), workArea.Width - 40), Math.Min((int)(800 * scale), workArea.Height - 40)));
        if (AppWindow.Presenter is OverlappedPresenter presenter)
        {
            presenter.PreferredMinimumWidth = (int)(940 * scale);
            presenter.PreferredMinimumHeight = (int)(640 * scale);
        }
        AppWindow.Closing += OnClosing;
    }

    private void ApplyTitleBarTheme()
    {
        Color background = (Root.Background as SolidColorBrush)?.Color
            ?? (Root.ActualTheme == ElementTheme.Dark
                ? Color.FromArgb(255, 32, 32, 32)
                : Color.FromArgb(255, 243, 243, 243));
        bool dark = (0.2126 * background.R + 0.7152 * background.G + 0.0722 * background.B) / 255.0 < 0.5;
        Color foreground = dark
            ? Color.FromArgb(255, 255, 255, 255)
            : Color.FromArgb(255, 0, 0, 0);
        Color inactiveForeground = Blend(background, foreground, 0.55);
        Color hover = Blend(background, foreground, 0.08);
        Color pressed = Blend(background, foreground, 0.14);

        ApplyDwmTheme(dark);
        if (!AppWindowTitleBar.IsCustomizationSupported()) return;

        var bar = AppWindow.TitleBar;
        bar.BackgroundColor = background;
        bar.ForegroundColor = foreground;
        bar.InactiveBackgroundColor = background;
        bar.InactiveForegroundColor = inactiveForeground;
        bar.ButtonBackgroundColor = background;
        bar.ButtonForegroundColor = foreground;
        bar.ButtonHoverBackgroundColor = hover;
        bar.ButtonHoverForegroundColor = foreground;
        bar.ButtonPressedBackgroundColor = pressed;
        bar.ButtonPressedForegroundColor = foreground;
        bar.ButtonInactiveBackgroundColor = background;
        bar.ButtonInactiveForegroundColor = inactiveForeground;
    }

    private void ApplyDwmTheme(bool dark)
    {
        int value = dark ? 1 : 0;
        nint hwnd = WinRT.Interop.WindowNative.GetWindowHandle(this);
        if (DwmSetWindowAttribute(hwnd, DwmwaUseImmersiveDarkMode, ref value, sizeof(int)) != 0)
            DwmSetWindowAttribute(hwnd, DwmwaUseImmersiveDarkModeBefore20H1, ref value, sizeof(int));
    }

    private static Color Blend(Color background, Color foreground, double amount) =>
        Color.FromArgb(255,
            (byte)Math.Round(background.R + (foreground.R - background.R) * amount),
            (byte)Math.Round(background.G + (foreground.G - background.G) * amount),
            (byte)Math.Round(background.B + (foreground.B - background.B) * amount));

    private async void Root_Loaded(object sender, RoutedEventArgs e)
    {
        if (_loaded) return;
        _loaded = true;
        bool logging = AppLog.Write("app_started");
        await Vm.InitializeAsync();
        if (!logging) Vm.ShowError("日志目录不可写；请将应用放到有写入权限的文件夹。导出仍需选择可写位置。", "LOG_NOT_WRITABLE");
    }

    private void Root_SizeChanged(object sender, SizeChangedEventArgs e)
    {
        if (DetailTabs is null) return;
        bool compact = e.NewSize.Width < 1140;
        ConversationColumn.Width = new GridLength(compact ? 216 : 252);
        ExportColumn.Width = new GridLength(compact ? 0 : 324);
        ExportRow.Height = compact ? new GridLength(286) : new GridLength(0);
        Grid.SetColumn(DetailTabs, compact ? 0 : 2);
        Grid.SetColumnSpan(DetailTabs, compact ? 2 : 1);
        Grid.SetRow(DetailTabs, compact ? 1 : 0);
    }

    private async void ConnectWechat_Click(object sender, RoutedEventArgs e)
    {
        if (!Vm.CanConnectWechat) return;
        var dialog = new ContentDialog
        {
            XamlRoot = Root.XamlRoot,
            Title = "连接微信？",
            Content = "应用将以只读方式读取本机微信 4.x 的会话与消息。连接时需要关闭并重新启动微信一次，以获取仅保存在内存中的数据库密钥；密钥不会写入磁盘或日志，微信数据库不会被修改。请先保存微信中未发送的内容。若微信要求手机确认，请在手机上完成确认。",
            PrimaryButtonText = "连接（会重启微信一次）",
            CloseButtonText = "取消",
            DefaultButton = ContentDialogButton.Close,
        };
        if (await dialog.ShowAsync() != ContentDialogResult.Primary) return;
        await Vm.ConnectWechatCommand.ExecuteAsync(null);
    }

    private async void Export_Click(object sender, RoutedEventArgs e)
    {
        if (!Vm.CanExport || sender is not Button button) return;
        string format = button.Tag as string ?? "markdown";
        var (label, extension) = format switch
        {
            "json" => ("JSON", ".json"),
            "pdf" => ("PDF", ".pdf"),
            _ => ("Markdown", ".md"),
        };
        try
        {
            var picker = new FileSavePicker { SuggestedStartLocation = PickerLocationId.DocumentsLibrary };
            WinRT.Interop.InitializeWithWindow.Initialize(picker, WinRT.Interop.WindowNative.GetWindowHandle(this));
            picker.FileTypeChoices.Add(label, new List<string> { extension });
            string name = Vm.SelectedConversation?.Title ?? "wechat";
            foreach (char ch in Path.GetInvalidFileNameChars()) name = name.Replace(ch, '_');
            picker.SuggestedFileName = name + "-" + DateTime.Now.ToString("yyyyMMdd-HHmmss");
            var file = await picker.PickSaveFileAsync();
            if (file is not null) await Vm.ExportAsync(file.Path, format, format == "pdf" && Vm.IncludePngPages);
        }
        catch (Exception ex) when (ex is System.Runtime.InteropServices.COMException or UnauthorizedAccessException or IOException or ArgumentException)
        { Vm.ShowError($"无法选择保存位置。请选择当前用户有权限写入的 {label} 文件。", "SAVE_PICKER_FAILED"); }
    }

    private async void MessageSearchBox_KeyDown(object sender, Microsoft.UI.Xaml.Input.KeyRoutedEventArgs e)
    {
        if (e.Key != Windows.System.VirtualKey.Enter || !Vm.ApplyFiltersCommand.CanExecute(null)) return;
        e.Handled = true;
        await Vm.ApplyFiltersCommand.ExecuteAsync(null);
    }

    private async void ChooseTargetFolder_Click(object sender, RoutedEventArgs e)
    {
        string? path = await PickFolderAsync();
        if (path is not null) Vm.TargetFolder = path;
    }

    private async void ChooseVault_Click(object sender, RoutedEventArgs e)
    {
        string? path = await PickFolderAsync();
        if (path is not null) Vm.ObsidianVault = path;
    }

    private async Task<string?> PickFolderAsync()
    {
        try
        {
            var picker = new FolderPicker { SuggestedStartLocation = PickerLocationId.DocumentsLibrary };
            WinRT.Interop.InitializeWithWindow.Initialize(picker, WinRT.Interop.WindowNative.GetWindowHandle(this));
            picker.FileTypeFilter.Add("*");
            var folder = await picker.PickSingleFolderAsync();
            if (folder is null) return null;
            if (string.IsNullOrWhiteSpace(folder.Path))
            {
                Vm.ShowError("请选择一个本机磁盘上的普通文件夹，而不是虚拟位置。", "FOLDER_PICKER_FAILED");
                return null;
            }
            return folder.Path;
        }
        catch (Exception ex) when (ex is System.Runtime.InteropServices.COMException or UnauthorizedAccessException or IOException or ArgumentException)
        {
            Vm.ShowError("无法选择文件夹。请检查权限后重试。", "FOLDER_PICKER_FAILED");
            return null;
        }
    }

    private async void OpenAttachment_Click(object sender, RoutedEventArgs e)
    {
        if (sender is not FrameworkElement { DataContext: ChatMessage message } || !message.HasAttachment) return;
        await LaunchPathAsync(message.AttachmentPath!, "无法打开已恢复的图片，临时文件可能已被清理。", "OPEN_ATTACHMENT_FAILED");
    }

    private async void OpenJobFolder_Click(object sender, RoutedEventArgs e)
    {
        if (sender is not FrameworkElement { DataContext: JobSummary job }) return;
        if (string.IsNullOrWhiteSpace(job.Directory) || !Directory.Exists(job.Directory))
        {
            Vm.ShowError("任务目录不存在或已被移动。", "OPEN_FOLDER_FAILED");
            return;
        }
        await LaunchPathAsync(job.Directory, "无法打开任务目录。", "OPEN_FOLDER_FAILED");
    }

    private async void DeliverJob_Click(object sender, RoutedEventArgs e)
    {
        if (sender is not FrameworkElement { DataContext: JobSummary job }) return;
        if (!job.CanRedeliver)
        {
            Vm.ShowError("只有待投递或失败的任务可以重新投递。", "JOB_NOT_READY");
            return;
        }
        if (Vm.TargetKind == "none")
        {
            Vm.ShowError("请先在任务页选择“复制到文件夹”或“复制到 Obsidian 仓库”。", "INVALID_ARGUMENT");
            return;
        }
        await Vm.DeliverJobAsync(job);
    }

    private async void OpenFile_Click(object sender, RoutedEventArgs e)
    {
        if (string.IsNullOrWhiteSpace(Vm.ResultPath)) return;
        await LaunchPathAsync(Vm.ResultPath, "无法打开结果，文件可能已移动或删除。", "OPEN_FILE_FAILED");
    }

    private async void OpenFolder_Click(object sender, RoutedEventArgs e)
    {
        if (string.IsNullOrWhiteSpace(Vm.ResultPath)) return;
        string folder = Directory.Exists(Vm.ResultPath) ? Vm.ResultPath : Path.GetDirectoryName(Vm.ResultPath) ?? "";
        await LaunchPathAsync(folder, "无法打开所在文件夹，目录可能已移动或删除。", "OPEN_FOLDER_FAILED");
    }

    private async void PrepareVoice_Click(object sender, RoutedEventArgs e)
    {
        var dialog = new ContentDialog
        {
            XamlRoot = Root.XamlRoot,
            Title = "下载语音模型？",
            Content = "将在本机下载 faster-whisper 语音识别模型（约 460 MB），保存在当前用户的应用数据目录。转写完全在本机进行，不上传音频或聊天内容。下载和使用受模型许可证约束。",
            PrimaryButtonText = "下载并准备",
            CloseButtonText = "取消",
            DefaultButton = ContentDialogButton.Close,
        };
        if (await dialog.ShowAsync() == ContentDialogResult.Primary) await Vm.PrepareVoiceModelAsync();
    }

    private async Task<bool> LaunchPathAsync(string path, string failureMessage, string code)
    {
        try
        {
            if (Directory.Exists(path))
            {
                if (await Launcher.LaunchFolderAsync(await StorageFolder.GetFolderFromPathAsync(path))) return true;
                throw new IOException("LaunchFolderAsync returned false.");
            }
            if (!File.Exists(path)) throw new FileNotFoundException(path);
            if (await Launcher.LaunchFileAsync(await StorageFile.GetFileFromPathAsync(path))) return true;
            throw new IOException("LaunchFileAsync returned false.");
        }
        catch (Exception ex) when (ex is System.Runtime.InteropServices.COMException or IOException or UnauthorizedAccessException or ArgumentException)
        {
            AppLog.Write("open_path_failed", code);
            Vm.ShowError(failureMessage, code);
            return false;
        }
    }

    private async void OnClosing(AppWindow sender, AppWindowClosingEventArgs args)
    {
        if (_allowClose) return;
        args.Cancel = true;
        if (_closeInProgress) return;
        _closeInProgress = true;
        Root.IsHitTestVisible = false;
        try { await Vm.DisposeAsync(); AppLog.Write("app_closed"); }
        catch (WorkerException e) { AppLog.Write("worker_cleanup_failed", e.Code); }
        finally { _allowClose = true; Close(); }
    }
}
