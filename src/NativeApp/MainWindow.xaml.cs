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
        AppWindow.SetIcon(Path.Combine(AppContext.BaseDirectory, "Assets", "app.ico"));
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

    private async void ImportJson_Click(object sender, RoutedEventArgs e)
    {
        if (!Vm.CanImportJson) return;
        try
        {
            var picker = new FileOpenPicker { SuggestedStartLocation = PickerLocationId.DocumentsLibrary };
            WinRT.Interop.InitializeWithWindow.Initialize(picker, WinRT.Interop.WindowNative.GetWindowHandle(this));
            picker.FileTypeFilter.Add(".json");
            var file = await picker.PickSingleFileAsync();
            if (file is not null) await Vm.ImportJsonAsync(file.Path);
        }
        catch (Exception ex) when (ex is System.Runtime.InteropServices.COMException or UnauthorizedAccessException or IOException or ArgumentException)
        {
            Vm.ShowError("无法选择 JSON 文件。请选择当前用户有权限读取的本地 .json 文件。", "IMPORT_PICKER_FAILED");
        }
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
        if (!Vm.CanManageJobs || sender is not FrameworkElement { DataContext: JobSummary job }) return;
        // Resolve the directory from the validated get_job record, not from any
        // manifest/state field, and launch through the StorageFolder boundary.
        var detail = await Vm.LoadJobDetailAsync(job);
        string? directory = detail?.Job.Directory;
        if (string.IsNullOrWhiteSpace(directory) || !Directory.Exists(directory))
        {
            Vm.ShowError("任务目录不存在或已被移动。", "OPEN_FOLDER_FAILED");
            return;
        }
        await LaunchPathAsync(directory, "无法打开任务目录。", "OPEN_FOLDER_FAILED");
    }

    private async void ShowJobDetail_Click(object sender, RoutedEventArgs e)
    {
        if (!Vm.CanManageJobs || sender is not FrameworkElement { DataContext: JobSummary selected }) return;
        var detail = await Vm.LoadJobDetailAsync(selected);
        if (detail is null) return;

        var job = detail.Job;
        var content = new StackPanel { Spacing = 7, MaxWidth = 680 };
        void AddField(string label, string? value)
        {
            if (string.IsNullOrWhiteSpace(value)) return;
            content.Children.Add(new TextBlock
            {
                Text = $"{label}：{value}",
                TextWrapping = Microsoft.UI.Xaml.TextWrapping.Wrap,
                IsTextSelectionEnabled = true,
            });
        }

        AddField("状态", job.StatusLabel);
        AddField("创建时间", job.CreatedAtDetailLabel);
        AddField("更新时间", job.UpdatedAtDetailLabel);
        AddField("会话", string.IsNullOrWhiteSpace(job.ConversationName) ? "会话名称未提供" : job.ConversationName);
        AddField("消息数", job.MessageCount is int messageCount ? $"{messageCount:N0}" : "未提供");
        AddField("会话标识", job.ConversationId);
        AddField("场景", job.SceneName);
        AddField("来源", detail.SourceLabel);
        AddField("日期范围", detail.Start is null && detail.End is null ? null : $"{detail.Start ?? "未提供"} 至 {detail.End ?? "未提供"}");
        AddField("关键词", detail.Query);
        AddField("目标", job.Target);
        AddField("外部投递位置", job.Destination);
        AddField("任务说明", job.Detail);
        AddField("错误", job.Error);
        if (job.Attempts is int attempts) AddField("尝试次数", attempts.ToString("N0"));
        if (job.Retryable is bool retryable) AddField("可重试", retryable ? "是" : "否");
        if (job.Status is "ready" or "failed") AddField("重新投递", "当前状态允许");
        else if (job.Status is "delivered" or "copied" or "expired" or "running" or "pending")
            AddField("重新投递", "当前状态不允许");

        if (job.Artifacts is null)
        {
            AddField("工件", "工件信息未提供");
        }
        else if (job.Artifacts.Count == 0)
        {
            AddField("工件", "没有工件记录");
        }
        else
        {
            AddField("本地工件", $"{job.Artifacts.Count:N0} 项");
            foreach (var artifact in job.Artifacts.Take(20))
            {
                var row = new Grid { ColumnSpacing = 8 };
                row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
                row.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
                string size = artifact.Size is long bytes ? $"{bytes:N0} 字节" : "大小未提供";
                var label = new TextBlock
                {
                    Text = $"{artifact.Name} · {artifact.Kind ?? "类型未提供"} · {size}",
                    TextWrapping = Microsoft.UI.Xaml.TextWrapping.Wrap,
                    VerticalAlignment = Microsoft.UI.Xaml.VerticalAlignment.Center,
                    IsTextSelectionEnabled = true,
                };
                var open = new Button { Content = "打开文件", FontSize = 11, Padding = new Thickness(8, 2, 8, 2) };
                open.Click += async (_, _) =>
                {
                    if (!Vm.CanManageJobs) return;
                    string? path = await Vm.ResolveJobArtifactPathAsync(job, artifact);
                    if (path is not null)
                        await LaunchPathAsync(path, "无法打开任务文件；文件可能不存在或未通过安全路径检查。", "OPEN_JOB_ARTIFACT_FAILED");
                };
                row.Children.Add(label);
                Grid.SetColumn(open, 1);
                row.Children.Add(open);
                content.Children.Add(row);
            }
            if (job.Artifacts.Count > 20) AddField("工件列表", "仅显示前 20 项。");
        }

        if (!string.IsNullOrWhiteSpace(job.Directory))
        {
            var openDirectory = new Button { Content = "打开任务目录", HorizontalAlignment = Microsoft.UI.Xaml.HorizontalAlignment.Left };
            openDirectory.Click += async (_, _) =>
            {
                if (!Vm.CanManageJobs) return;
                if (!Directory.Exists(job.Directory))
                {
                    Vm.ShowError("任务目录不存在或已被移动。", "OPEN_FOLDER_FAILED");
                    return;
                }
                await LaunchPathAsync(job.Directory, "无法打开任务目录。", "OPEN_FOLDER_FAILED");
            };
            content.Children.Add(openDirectory);
        }

        var dialog = new ContentDialog
        {
            XamlRoot = Root.XamlRoot,
            Title = "任务详情",
            Content = new ScrollViewer
            {
                Content = content,
                MaxHeight = 540,
                VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
            },
            CloseButtonText = "关闭",
            DefaultButton = ContentDialogButton.Close,
        };
        await dialog.ShowAsync();
    }

    private async void CleanupJobs_Click(object sender, RoutedEventArgs e)
    {
        var workflow = Vm.TryBeginPruneWorkflow();
        if (workflow is null) return;
        try
        {
            var preview = await Vm.PreviewPruneJobsAsync(Vm.PruneRetentionDays);
            if (preview is null) return;

            var previewContent = new StackPanel { Spacing = 8, MaxWidth = 620 };
            void AddPreviewLine(string text) => previewContent.Children.Add(new TextBlock
            {
                Text = text,
                TextWrapping = Microsoft.UI.Xaml.TextWrapping.Wrap,
                IsTextSelectionEnabled = true,
            });
            AddPreviewLine($"保留期限：{preview.Days} 天；截止时间：{preview.Cutoff}");
            AddPreviewLine($"只读预览候选：{preview.CandidateCount:N0} 个旧的已完成任务。");
            AddPreviewLine($"可统计的本地工件逻辑字节：{preview.KnownArtifactBytes:N0} 字节；这不等于磁盘实际可释放空间。");
            AddPreviewLine($"跳过或无法统计：{preview.UnknownSizeCount:N0} 项。未统计条目不是额外的可删除候选。");
            string previewReasons = MainViewModel.FormatPruneReasonCounts(preview.SkippedReasonCounts);
            if (previewReasons.Length > 0) AddPreviewLine("跳过原因摘要：" + previewReasons);
            AddPreviewLine("此步骤只读，不会删除文件。不会后台自动清理。预览确认授权有效期为 10 分钟；过期后需重新预览。");

            var previewDialog = new ContentDialog
            {
                XamlRoot = Root.XamlRoot,
                Title = "任务历史清理预览",
                Content = new ScrollViewer { Content = previewContent, MaxHeight = 500, VerticalScrollBarVisibility = ScrollBarVisibility.Auto },
                PrimaryButtonText = preview.HasCandidates ? "继续到永久删除确认" : "",
                CloseButtonText = "关闭",
                DefaultButton = ContentDialogButton.Close,
            };
            if (await previewDialog.ShowAsync() != ContentDialogResult.Primary || !preview.HasCandidates) return;

            const string confirmationText = "这将永久删除应用管理的本地任务副本，且不移入回收站；发生错误时可能只删除部分任务。不会删除外部文件夹或 Obsidian 中已投递的副本，也不会删除用户提供的 JSON 原件。未完成或可恢复任务不会作为候选。\n\n安全删除仅支持 Windows 本地固定 NTFS 卷；文件占用、无法建立安全保护或当前卷不支持时会跳过。后端会重新检查可识别的状态和安全条件，但不能保证覆盖所有外部并发修改。\n\n重要：选中任务文件的 NTFS 备用数据流（ADS）不在预览、变化校验或防写保护范围内，会随宿主文件一起删除，包括确认后新增的流。如在 ADS 另存数据，请取消并先备份。\n\n若执行期间工作进程连接丢失，可能已有部分任务被永久删除，请重新打开并刷新任务历史核实。";
            var confirmation = new ContentDialog
            {
                XamlRoot = Root.XamlRoot,
                Title = "永久删除这些任务？",
                Content = new ScrollViewer
                {
                    Content = new TextBlock { Text = confirmationText, TextWrapping = Microsoft.UI.Xaml.TextWrapping.Wrap },
                    MaxHeight = 500,
                    VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
                },
                PrimaryButtonText = "永久删除这些任务",
                CloseButtonText = "取消",
                DefaultButton = ContentDialogButton.Close,
            };
            if (await confirmation.ShowAsync() != ContentDialogResult.Primary) return;

            var result = await Vm.ConfirmPruneJobsAsync(preview);
            if (result is null) return;
            var presentation = PruneResultPresentation.Create(result);
            var resultDialog = new ContentDialog
            {
                XamlRoot = Root.XamlRoot,
                Title = presentation.Title,
                Content = presentation.Summary,
                CloseButtonText = "关闭",
                DefaultButton = ContentDialogButton.Close,
            };
            await resultDialog.ShowAsync();
        }
        finally
        {
            workflow.Dispose();
        }
    }

    private async void DeliverJob_Click(object sender, RoutedEventArgs e)
    {
        if (!Vm.CanManageJobs || sender is not FrameworkElement { DataContext: JobSummary job }) return;
        if (!job.CanRedeliver)
        {
            Vm.ShowError("只有待投递或失败的任务可以重新投递。", "JOB_NOT_READY");
            return;
        }
        if (!Vm.CanRetryJob(job)) return;
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
        if (Vm.IsPruneExecuting)
        {
            _closeInProgress = true;
            ContentDialogResult choice;
            try
            {
                var warning = new ContentDialog
                {
                    XamlRoot = Root.XamlRoot,
                    Title = "清理仍在执行",
                    Content = "关闭会终止工作进程。清理可能已经永久删除部分任务，关闭时无法确认完整结果。建议继续等待；若仍要关闭，请在下次启动后刷新任务历史核实。",
                    PrimaryButtonText = "仍要关闭并停止工作进程",
                    CloseButtonText = "继续等待",
                    DefaultButton = ContentDialogButton.Close,
                };
                choice = await warning.ShowAsync();
            }
            catch (Exception ex) when (ex is System.Runtime.InteropServices.COMException or InvalidOperationException)
            {
                _closeInProgress = false;
                return;
            }
            _closeInProgress = false;
            if (choice != ContentDialogResult.Primary) return;
        }
        _closeInProgress = true;
        Root.IsHitTestVisible = false;
        try { await Vm.DisposeAsync(); AppLog.Write("app_closed"); }
        catch (WorkerException e) { AppLog.Write("worker_cleanup_failed", e.Code); }
        finally { _allowClose = true; Close(); }
    }
}
