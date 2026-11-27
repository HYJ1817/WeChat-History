param(
    [string]$Executable,
    [string]$OutputDirectory
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient,UIAutomationTypes,System.Drawing,System.Windows.Forms
Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class NativeFlowCapture {
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hwnd, out RECT rect);
    [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr hwnd, IntPtr hdc, uint flags);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hwnd);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern bool SetWindowText(IntPtr hwnd, string text);
    [DllImport("user32.dll")] public static extern IntPtr SendMessage(IntPtr hwnd, uint message, IntPtr wParam, IntPtr lParam);
    [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr hwnd, uint message, IntPtr wParam, IntPtr lParam);
}
'@
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $scriptDir
if (-not $Executable) { $Executable = Join-Path $root 'src/NativeApp/bin/x64/Debug/net10.0-windows10.0.19041.0/win-x64/NativeApp.exe' }
if (-not $OutputDirectory) { $OutputDirectory = Join-Path $root 'artifacts/native-flow' }
New-Item -ItemType Directory -Force $OutputDirectory | Out-Null
$evidenceCopy = Join-Path $OutputDirectory 'ui-export-2026-11-24.md'
if (Test-Path -LiteralPath $evidenceCopy) { Remove-Item -LiteralPath $evidenceCopy -Force }

$AE = [System.Windows.Automation.AutomationElement]
$TS = [System.Windows.Automation.TreeScope]
$CT = [System.Windows.Automation.ControlType]
$report = [ordered]@{ executable = (Resolve-Path -LiteralPath $Executable).Path; utc = [DateTime]::UtcNow.ToString('o'); stage = 'init' }
$app = $null
$window = $null
$workerPids = @()
$previousClipboard = $null

function Get-ById([System.Windows.Automation.AutomationElement]$parent, [string]$id) {
    $condition = New-Object System.Windows.Automation.PropertyCondition($AE::AutomationIdProperty, $id)
    return $parent.FindFirst($TS::Descendants, $condition)
}
function Find-All([System.Windows.Automation.AutomationElement]$parent, $controlType) {
    $condition = New-Object System.Windows.Automation.PropertyCondition($AE::ControlTypeProperty, $controlType)
    return $parent.FindAll($TS::Descendants, $condition)
}
function Wait-Until {
    param([scriptblock]$Condition, [int]$TimeoutSeconds = 30, [string]$Description = 'condition')
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        $value = & $Condition
        if ($value) { return $value }
        Start-Sleep -Milliseconds 250
    }
    throw "Timed out waiting for $Description"
}
function Invoke-Element([System.Windows.Automation.AutomationElement]$element) {
    $pattern = $element.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
    $pattern.Invoke()
}
function Get-Name([System.Windows.Automation.AutomationElement]$element) {
    if ($null -eq $element) { return '' }
    return $element.Current.Name
}
function Set-ClipboardText([string]$text) {
    # The shell's save dialog is a DirectUI host whose edit control has no
    # ValuePattern and cannot take UIA focus. Alt+N focuses it; pasting the whole
    # path at once avoids the navigation race that silently drops typed tails.
    $deadline = [DateTime]::UtcNow.AddSeconds(5)
    while ($true) {
        try { [System.Windows.Forms.Clipboard]::SetText($text); return }
        catch {
            if ([DateTime]::UtcNow -ge $deadline) {
                throw "Could not put the export path on the clipboard: $($_.Exception.Message)"
            }
            Start-Sleep -Milliseconds 200
        }
    }
}
function Save-Shot([string]$name) {
    if ($null -eq $window) { return }
    $rect = New-Object NativeFlowCapture+RECT
    if (-not [NativeFlowCapture]::GetWindowRect($app.MainWindowHandle, [ref]$rect)) { throw 'GetWindowRect failed.' }
    $bitmap = New-Object System.Drawing.Bitmap(($rect.Right-$rect.Left),($rect.Bottom-$rect.Top))
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    $dc = $graphics.GetHdc()
    try { if (-not [NativeFlowCapture]::PrintWindow($app.MainWindowHandle, $dc, 2)) { throw 'PrintWindow failed.' } }
    finally { $graphics.ReleaseHdc($dc); $graphics.Dispose() }
    try { $bitmap.Save((Join-Path $OutputDirectory $name), [System.Drawing.Imaging.ImageFormat]::Png) }
    finally { $bitmap.Dispose() }
}
function Select-Date($picker, [string]$day) {
    $picker.SetFocus()
    Invoke-Element $picker
    $calendar = Wait-Until { Get-ById $window 'CalendarView' } 10 'calendar view'
    $dayItem = Wait-Until {
        $candidates = @(Find-All $calendar $CT::DataItem | Where-Object { $_.Current.Name -match ('^' + [regex]::Escape($day) + '(，|$)') })
        if ($candidates.Count -gt 0) { $candidates[0] }
    } 10 "calendar day $day"
    $selection = $dayItem.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern)
    $selection.Select()
    Start-Sleep -Milliseconds 700
    return $picker
}
function Get-DirectChildren([int]$parentPid) {
    return @(Get-CimInstance Win32_Process -Filter "ParentProcessId = $parentPid" -ErrorAction SilentlyContinue)
}
function Test-Alive([int]$processId) {
    return $null -ne (Get-Process -Id $processId -ErrorAction SilentlyContinue)
}

try {
    $report.stage = 'launch'
    $demoPath = Join-Path $root 'demo/conversations.json'
    $app = Start-Process -FilePath $report.executable -ArgumentList @('--demo', $demoPath) -PassThru
    $handle = Wait-Until { $app.Refresh(); if ($app.HasExited) { throw "App exited early: $($app.ExitCode)" }; if ($app.MainWindowHandle -ne [IntPtr]::Zero) { $app.MainWindowHandle } } 30 'native window'
    [NativeFlowCapture]::SetForegroundWindow($handle) | Out-Null
    Start-Sleep -Seconds 2
    $window = $AE::FromHandle($app.MainWindowHandle)

    $report.stage = 'load-conversations'
    $list = Wait-Until { Get-ById $window 'ConversationList' } 30 'conversation list'
    $items = Wait-Until { $found = Find-All $list $CT::ListItem; if ($found.Count -ge 3) { $found } } 40 'three demo conversations'
    $report.conversationCount = $items.Count
    Save-Shot '01-loaded.png'

    $report.stage = 'select-conversation'
    $selected = $items[0]
    $selection = $selected.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern)
    $selection.Select()
    $report.selectedConversation = Get-Name $selected
    $status = Get-ById $window 'TaskStatus'
    Wait-Until { (Get-Name (Get-ById $window 'TaskStatus')) -match '消息已加载' } 30 'messages loaded'
    Save-Shot '02-selected.png'

    $report.stage = 'filter-dates'
    $startPicker = Get-ById $window 'StartDate'
    $endPicker = Get-ById $window 'EndDate'
    if ($null -eq $startPicker -or $null -eq $endPicker) { throw 'Date pickers not found.' }
    Select-Date $startPicker '24' | Out-Null
    Wait-Until { (Get-Name (Get-ById $startPicker 'DateText')) -match '2026' } 10 'start date text'
    Select-Date $endPicker '24' | Out-Null
    Wait-Until { (Get-Name (Get-ById $endPicker 'DateText')) -match '2026' } 10 'end date text'
    $report.datePickers = "$(Get-Name (Get-ById $startPicker 'DateText')) / $(Get-Name (Get-ById $endPicker 'DateText'))"
    $apply = Get-ById $window 'ApplyFilterButton'
    $apply.SetFocus()
    Invoke-Element $apply
    Wait-Until { (Get-Name (Get-ById $window 'MessageSummary')) -match '4\s*/\s*4' } 20 'filtered message count'
    $report.filterSummary = Get-Name (Get-ById $window 'MessageSummary')
    $messageList = Get-ById $window 'MessageList'
    $report.visibleMessageCount = (Find-All $messageList $CT::ListItem).Count
    # Give ItemsStackPanel one frame to arrange the new items before capturing evidence.
    Start-Sleep -Milliseconds 1200
    Save-Shot '03-filtered.png'
    if ($report.visibleMessageCount -ne 4) { throw "Expected 4 visible messages, got $($report.visibleMessageCount)." }

    $report.stage = 'export'
    $exportButton = Get-ById $window 'ExportButton'
    $exportButton.SetFocus()
    Invoke-Element $exportButton
    $dialog = Wait-Until {
        $condition = New-Object System.Windows.Automation.PropertyCondition($AE::ControlTypeProperty, $CT::Window)
        $windows = @($window.FindAll($TS::Descendants, $condition))
        $found = @($windows | Where-Object { $_.Current.ClassName -eq '#32770' -and $_.Current.Name -match '另存为|Save As' })
        if ($found.Count -gt 0) { $found[0] }
    } 20 'save dialog'
    $report.saveDialog = Get-Name $dialog

    $fileName = $null
    foreach ($candidate in $dialog.FindAll($TS::Descendants, [System.Windows.Automation.Condition]::TrueCondition)) {
        if ($null -eq $fileName -and $candidate.Current.AutomationId -eq '1001' -and
            $candidate.Current.ClassName -eq 'Edit' -and $candidate.Current.NativeWindowHandle -ne 0) {
            $fileName = $candidate
        }
    }
    if ($null -eq $fileName) { throw 'Save dialog file name field not found.' }
    Write-Output 'STEP dialog-found'

    # Enter a full ASCII path through the real save dialog: its edit control only
    # updates the committed name through EN_CHANGE, which SetWindowText does not
    # raise, and typing character by character races with folder navigation. A
    # single paste commits the whole path atomically and keeps the test file out
    # of the user's Documents folder.
    $exportDirectory = Join-Path $env:TEMP 'wechat-memory-e2e'
    New-Item -ItemType Directory -Force $exportDirectory | Out-Null
    $exportPath = Join-Path $exportDirectory 'ui-export-2026-11-24.md'
    if (Test-Path -LiteralPath $exportPath) { Remove-Item -LiteralPath $exportPath -Force }
    $dialogHandle = [IntPtr]$dialog.Current.NativeWindowHandle
    [NativeFlowCapture]::SetForegroundWindow($dialogHandle) | Out-Null
    Start-Sleep -Milliseconds 400
    # Alt+N focuses the "文件名" field. UIA SetFocus is not usable here: the
    # control is exposed as a Pane and rejects focus.
    [System.Windows.Forms.SendKeys]::SendWait('%n')
    Start-Sleep -Milliseconds 350
    [System.Windows.Forms.SendKeys]::SendWait('^a')
    try { $previousClipboard = [System.Windows.Forms.Clipboard]::GetDataObject() } catch { $previousClipboard = $null }
    Set-ClipboardText $exportPath
    Start-Sleep -Milliseconds 150
    [System.Windows.Forms.SendKeys]::SendWait('^v')
    Start-Sleep -Milliseconds 500
    $typedName = (Get-Name $fileName).Trim()
    if ($typedName -ne $exportPath) {
        throw "Save dialog did not accept the pasted path. Expected '$exportPath', field shows '$typedName'."
    }
    Write-Output "STEP filename-typed=$typedName"
    [System.Windows.Forms.SendKeys]::SendWait('{ENTER}')
    Write-Output 'STEP save-enter-sent'

    $exportLabel = $null
    $labelDeadline = [DateTime]::UtcNow.AddSeconds(12)
    while ([DateTime]::UtcNow -lt $labelDeadline -and $null -eq $exportLabel) {
        $exportLabel = Get-ById $window 'ExportPath'
        if ($null -eq $exportLabel) { Start-Sleep -Milliseconds 300 }
    }
    if ($null -eq $exportLabel) {
        # Enter may only have committed the file-name edit; press the dialog's Save button.
        [NativeFlowCapture]::PostMessage($dialogHandle, 0x0111, [IntPtr]1, [IntPtr]::Zero) | Out-Null
        Write-Output 'STEP save-posted'
        $exportLabel = Wait-Until { Get-ById $window 'ExportPath' } 35 'export path label'
    }
    $actualPath = (Get-Name $exportLabel).Trim()
    if ($actualPath -notmatch '^[A-Za-z]:\\.+\.md$') { throw "Unexpected export path reported by the app: $actualPath" }
    $report.exportPathShown = $actualPath
    $expectedFullPath = [System.IO.Path]::GetFullPath($exportPath)
    $actualFullPath = [System.IO.Path]::GetFullPath($actualPath)
    $report.expectedExportPath = $expectedFullPath
    # Entering a full path is only meaningful if the app exports there. A stale
    # or default save-location would otherwise leave ok=true while writing
    # outside the test directory and silently deleting the wrong file below.
    $report.customPathUsed = [string]::Equals(
        $actualFullPath, $expectedFullPath, [System.StringComparison]::OrdinalIgnoreCase)
    if (-not $report.customPathUsed) {
        throw "Save dialog did not use the entered path. Expected '$expectedFullPath', got '$actualFullPath'."
    }
    Wait-Until { Test-Path -LiteralPath $actualPath } 20 'exported markdown file'
    Write-Output 'STEP file-found'
    $document = Get-Content -LiteralPath $actualPath -Raw -Encoding UTF8
    if (-not $document.StartsWith('# ')) { throw 'Exported markdown has no title.' }
    if ($document -notmatch '## 2026-11-24') { throw 'Exported markdown has no selected date heading.' }
    if ($document -notmatch '23:59') { throw 'Exported markdown missed the end-of-day message.' }
    if ($document -match '08:15') { throw 'Exported markdown leaked a message outside the filter.' }
    if ($document -notmatch '✅') { throw 'Exported markdown lost emoji content.' }
    $report.exportBytes = (Get-Item -LiteralPath $actualPath).Length
    $report.exportTitle = ($document -split "`n")[0]
    $report.exportMessageCount = ([regex]::Matches($document, '(?m)^\*\*.+\*\* · ')).Count
    Copy-Item -LiteralPath $actualPath -Destination $evidenceCopy -Force
    $report.evidenceCopy = $evidenceCopy
    Save-Shot '04-exported.png'

    # Remove the file this test just created; the evidence copy stays in artifacts.
    Remove-Item -LiteralPath $actualPath -Force
    $report.testFileRemoved = -not (Test-Path -LiteralPath $actualPath)

    $report.stage = 'worker-cleanup'
    $children = Get-DirectChildren $app.Id
    $workerProcesses = @($children | Where-Object { $_.Name -match 'python|WechatMemory\.Worker' })
    $workerPids = @($workerProcesses | ForEach-Object { [int]$_.ProcessId })
    $report.workerProcessIds = $workerPids
    $report.workerProcessNames = @($workerProcesses | ForEach-Object { $_.Name })
    if ($workerPids.Count -lt 1) { throw 'No dedicated Python worker child process was observed.' }

    $report.stage = 'close'
    $closed = $app.CloseMainWindow()
    if (-not $closed) { throw 'CloseMainWindow failed.' }
    if (-not $app.WaitForExit(15000)) { throw 'App did not exit after window close.' }
    $report.appExitCode = $app.ExitCode
    Wait-Until { @($workerPids | Where-Object { Test-Alive $_ }).Count -eq 0 } 10 'worker process reaping'
    $report.workerReaped = $true
    $report.stage = 'done'
    $report.ok = $true
    $report | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $OutputDirectory 'report.json') -Encoding UTF8
    $report | ConvertTo-Json -Depth 6
} catch {
    $report.ok = $false
    $report.error = $_.Exception.Message
    try { Save-Shot '99-failure.png' } catch { }
    $report | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $OutputDirectory 'report.json') -Encoding UTF8
    throw
} finally {
    if ($null -ne $app) {
        try { if (-not $app.HasExited) { $app.Kill(); $app.WaitForExit() } } catch { }
    }
    foreach ($pidValue in $workerPids) {
        try { $process = Get-Process -Id $pidValue -ErrorAction SilentlyContinue; if ($process) { $process.Kill() } } catch { }
    }
    if ($null -ne $previousClipboard) {
        try { [System.Windows.Forms.Clipboard]::SetDataObject($previousClipboard, $true) } catch { }
    }
}
