param(
    [string]$Executable,
    [string]$TestDirectory
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient,UIAutomationTypes,System.Drawing,System.Windows.Forms
Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class NativeSearchPagingCapture {
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hwnd, out RECT rect);
    [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr hwnd, IntPtr hdc, uint flags);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hwnd);
}
'@

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $scriptDir
if (-not $Executable) { $Executable = Join-Path $root 'src/NativeApp/bin/x64/Debug/net10.0-windows10.0.19041.0/win-x64/WeChat-History.exe' }
if (-not $TestDirectory) { $TestDirectory = Join-Path $env:TEMP ('wechat-native-search-' + [Guid]::NewGuid().ToString('N')) }
$TestDirectory = [System.IO.Path]::GetFullPath($TestDirectory)
$tempRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
if (-not $TestDirectory.StartsWith($tempRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "TestDirectory must be under the system temporary directory: $tempRoot"
}
if (Test-Path -LiteralPath $TestDirectory) { throw "Refusing to reuse an existing test directory: $TestDirectory" }
$outputDirectory = Join-Path $TestDirectory 'output'
$isolatedTemp = Join-Path $TestDirectory 'temp'
New-Item -ItemType Directory -Path $outputDirectory | Out-Null
New-Item -ItemType Directory -Path $isolatedTemp | Out-Null
$env:TEMP = $isolatedTemp
$env:TMP = $isolatedTemp

$fixturePath = Join-Path $outputDirectory 'synthetic-offline.json'
$title = 'Synthetic UI Pagination Fixture'
$messages = for ($index = 0; $index -lt 1100; $index++) {
    $timestamp = [DateTimeOffset]::new(2026, 11, 20, 8, 0, 0, [TimeSpan]::FromHours(8)).AddMinutes($index)
    $content = 'Synthetic offline page item {0:D3}' -f $index
    if ($index -eq 73) { $content = 'SYNTHETIC_SEARCH_TARGET_073' }
    [ordered]@{
        id = ('ui-msg-{0:D3}' -f $index)
        sender = if ($index % 2 -eq 0) { 'Synthetic Alice' } else { 'Synthetic Bob' }
        timestamp = $timestamp.ToString('yyyy-MM-ddTHH:mm:sszzz', [Globalization.CultureInfo]::InvariantCulture)
        type = 'text'
        content = $content
        is_outgoing = $false
    }
}
$fixture = [ordered]@{
    version = 1
    conversations = @([ordered]@{
        id = 'ui-synthetic-pagination'
        name = $title
        kind = 'group'
        messages = @($messages)
    })
}
$fixture | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $fixturePath -Encoding UTF8

$AE = [System.Windows.Automation.AutomationElement]
$TS = [System.Windows.Automation.TreeScope]
$CT = [System.Windows.Automation.ControlType]
$report = [ordered]@{
    executable = (Resolve-Path -LiteralPath $Executable).Path
    testDirectory = $TestDirectory
    fixturePath = $fixturePath
    fixtureMessageCount = $messages.Count
    utc = [DateTime]::UtcNow.ToString('o')
    stage = 'init'
}
$app = $null
$window = $null
$previousClipboard = $null
$workerPids = @()

function Get-ById([System.Windows.Automation.AutomationElement]$parent, [string]$id) {
    $condition = New-Object System.Windows.Automation.PropertyCondition($AE::AutomationIdProperty, $id)
    return $parent.FindFirst($TS::Descendants, $condition)
}
function Find-All([System.Windows.Automation.AutomationElement]$parent, $controlType) {
    if ($null -eq $parent) { return @() }
    $condition = New-Object System.Windows.Automation.PropertyCondition($AE::ControlTypeProperty, $controlType)
    return $parent.FindAll($TS::Descendants, $condition)
}
function Get-Name([System.Windows.Automation.AutomationElement]$element) {
    if ($null -eq $element) { return '' }
    return $element.Current.Name
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
    if ($null -eq $element) { throw 'Cannot invoke a missing UI element.' }
    $pattern = $element.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
    $pattern.Invoke()
}
function Set-ElementText([System.Windows.Automation.AutomationElement]$element, [string]$text) {
    try {
        $pattern = $element.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
        $pattern.SetValue($text)
    } catch {
        $element.SetFocus()
        [System.Windows.Forms.SendKeys]::SendWait('^a')
        [System.Windows.Forms.Clipboard]::SetText($text)
        [System.Windows.Forms.SendKeys]::SendWait('^v')
    }
}
function Save-Shot([string]$name) {
    if ($null -eq $window) { return }
    $rect = New-Object NativeSearchPagingCapture+RECT
    if (-not [NativeSearchPagingCapture]::GetWindowRect($app.MainWindowHandle, [ref]$rect)) { throw 'GetWindowRect failed.' }
    $bitmap = New-Object System.Drawing.Bitmap(($rect.Right - $rect.Left), ($rect.Bottom - $rect.Top))
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    $dc = $graphics.GetHdc()
    try { if (-not [NativeSearchPagingCapture]::PrintWindow($app.MainWindowHandle, $dc, 2)) { throw 'PrintWindow failed.' } }
    finally { $graphics.ReleaseHdc($dc); $graphics.Dispose() }
    try { $bitmap.Save((Join-Path $outputDirectory $name), [System.Drawing.Imaging.ImageFormat]::Png) }
    finally { $bitmap.Dispose() }
}
function Get-DirectChildren([int]$parentPid) {
    return @(Get-CimInstance Win32_Process -Filter "ParentProcessId = $parentPid" -ErrorAction SilentlyContinue)
}
function Test-Alive([int]$processId) {
    return $null -ne (Get-Process -Id $processId -ErrorAction SilentlyContinue)
}

try {
    $report.stage = 'launch-demo-worker'
    $demoPath = Join-Path $root 'demo/conversations.json'
    $app = Start-Process -FilePath $report.executable -ArgumentList @('--demo', ('"{0}"' -f $demoPath)) -PassThru
    $handle = Wait-Until { $app.Refresh(); if ($app.HasExited) { throw "App exited early: $($app.ExitCode)" }; if ($app.MainWindowHandle -ne [IntPtr]::Zero) { $app.MainWindowHandle } } 30 'native window'
    [NativeSearchPagingCapture]::SetForegroundWindow($handle) | Out-Null
    Start-Sleep -Seconds 2
    $window = $AE::FromHandle($app.MainWindowHandle)
    Wait-Until { $list = Get-ById $window 'ConversationList'; if ($null -ne $list -and (Find-All $list $CT::ListItem).Count -ge 3) { $list } } 35 'demo conversations'

    $report.stage = 'create-offline-fixture'
    if (-not (Test-Path -LiteralPath $fixturePath)) { throw 'Synthetic offline JSON fixture was not created.' }
    $importButton = Get-ById $window 'ImportJsonButton'
    Invoke-Element $importButton

    $report.stage = 'select-offline-fixture'
    $picker = Wait-Until {
        $condition = New-Object System.Windows.Automation.PropertyCondition($AE::ControlTypeProperty, $CT::Window)
        $candidates = @($window.FindAll($TS::Descendants, $condition)) + @($AE::RootElement.FindAll($TS::Children, $condition))
        $openDialogName = -join [char[]](0x6253, 0x5F00)
        $found = @($candidates | Where-Object { ($_.Current.Name.Contains($openDialogName) -or $_.Current.Name -match 'Open') -and $_.Current.NativeWindowHandle -ne 0 })
        if ($found.Count -gt 0) { $found[0] }
    } 20 'offline JSON open dialog'
    [NativeSearchPagingCapture]::SetForegroundWindow([IntPtr]$picker.Current.NativeWindowHandle) | Out-Null
    Start-Sleep -Milliseconds 500
    [System.Windows.Forms.SendKeys]::SendWait('%n')
    Start-Sleep -Milliseconds 250
    [System.Windows.Forms.SendKeys]::SendWait('^a')
    try { $previousClipboard = [System.Windows.Forms.Clipboard]::GetDataObject() } catch { $previousClipboard = $null }
    [System.Windows.Forms.Clipboard]::SetText($fixturePath)
    [System.Windows.Forms.SendKeys]::SendWait('^v')
    Start-Sleep -Milliseconds 300
    [System.Windows.Forms.SendKeys]::SendWait('{ENTER}')
    $status = Wait-Until { $name = Get-Name (Get-ById $window 'TaskStatus'); if ($name -match '1100') { $name } } 45 'synthetic JSON import status'
    $report.importStatus = $status
    $conversationList = Get-ById $window 'ConversationList'
    $conversationItem = Wait-Until {
        $list = Get-ById $window 'ConversationList'
        $rows = @(Find-All $list $CT::ListItem)
        if ($rows.Count -eq 1) { $rows[0] }
    } 15 'synthetic conversation row'
    Wait-Until { (Get-Name (Get-ById $window 'SelectedTitle')) -eq $title } 15 'synthetic conversation selected'

    $report.stage = 'cursor-next-previous'
    $conversationItem.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern).Select()
    $summary = Get-ById $window 'MessageSummary'
    $firstBatch = Wait-Until { $name = Get-Name $summary; if ($name -match '50\s*/\s*1,?100') { $name } } 30 'first cursor page'
    $report.firstBatch = $firstBatch
    Save-Shot '01-first-batch.png'

    $lastBatch = $firstBatch
    for ($step = 1; $step -le 20; $step++) {
        $nextButton = Get-ById $window 'NextMessageBatchButton'
        if ($null -eq $nextButton -or -not $nextButton.Current.IsEnabled) {
            throw "Next cursor batch was not enabled at navigation step $step."
        }
        Invoke-Element $nextButton
        $lastBatch = Wait-Until { $name = Get-Name (Get-ById $window 'MessageSummary'); if ($name -and $name -ne $lastBatch) { $name } } 30 "next cursor batch $step"
    }
    $report.nextBatch = $lastBatch
    $report.nextBatchSteps = 20
    $previousButton = Wait-Until {
        $button = Get-ById $window 'PreviousMessageBatchButton'
        if ($null -ne $button -and $button.Current.IsEnabled) { $button }
    } 15 'previous cursor batch enabled after window eviction'
    Invoke-Element $previousButton
    $returnedBatch = Wait-Until { $name = Get-Name (Get-ById $window 'MessageSummary'); if ($name -and $name -ne $lastBatch) { $name } } 30 'previous cursor batch'
    $report.previousBatch = $returnedBatch
    Save-Shot '02-after-previous-batch.png'

    $report.stage = 'global-search-context-return'
    $globalSearchLabel = -join [char[]](0x5168, 0x5C40, 0x641C, 0x7D22)
    $globalModeButton = Wait-Until {
        $matches = @(Find-All $window $CT::Button | Where-Object { $_.Current.Name -eq $globalSearchLabel -and $_.Current.IsEnabled })
        if ($matches.Count -gt 0) { $matches[0] }
    } 15 'switch to global search button'
    Invoke-Element $globalModeButton
    $globalQuery = Get-ById $window 'GlobalMessageSearch'
    Wait-Until { $null -ne $globalQuery -and -not $globalQuery.Current.IsOffscreen } 10 'global search field'
    Set-ElementText $globalQuery 'SYNTHETIC_SEARCH_TARGET_073'
    $searchButton = Get-ById $window 'GlobalSearchButton'
    Wait-Until { $searchButton.Current.IsEnabled } 10 'global search enabled'
    Invoke-Element $searchButton

    $results = Get-ById $window 'GlobalSearchResults'
    Wait-Until { $results = Get-ById $window 'GlobalSearchResults'; if ($null -ne $results -and (Find-All $results $CT::ListItem).Count -eq 1) { $results } } 35 'unique global search hit'
    $openHit = Wait-Until {
        $hitText = @(Find-All $results $CT::Text | Where-Object { $_.Current.Name -eq 'SYNTHETIC_SEARCH_TARGET_073' })
        $button = Get-ById $results 'OpenGlobalHitButton'
        if ($null -eq $button) {
            $buttons = @(Find-All $results $CT::Button)
            if ($buttons.Count -eq 1) { $button = $buttons[0] }
        }
        if ($hitText.Count -eq 1 -and $null -ne $button) { $button }
    } 15 'exact global search hit'
    $report.globalHitName = Get-Name $openHit
    Invoke-Element $openHit

    $context = Get-ById $window 'MessageContext'
    Wait-Until { $context = Get-ById $window 'MessageContext'; if ($null -ne $context -and -not $context.Current.IsOffscreen) { $context } } 25 'exact message context view'
    $contextNames = @((Find-All $context $CT::Text | ForEach-Object { Get-Name $_ }))
    $focusLabel = -join [char[]](0x641C, 0x7D22, 0x547D, 0x4E2D)
    if (-not ($contextNames -contains 'SYNTHETIC_SEARCH_TARGET_073') -or -not ($contextNames -contains $focusLabel)) {
        throw "Context did not expose the exact focused result; text nodes were: $($contextNames -join ' | ')"
    }
    $report.contextIncludesExactHit = $true
    $report.contextFocusLabel = $focusLabel
    Save-Shot '02-exact-hit-context.png'

    $returnButton = Get-ById $window 'ReturnToGlobalSearch'
    Invoke-Element $returnButton
    Wait-Until {
        $list = Get-ById $window 'GlobalSearchResults'
        $button = $null
        if ($null -ne $list) {
            $texts = @(Find-All $list $CT::Text | Where-Object { $_.Current.Name -eq 'SYNTHETIC_SEARCH_TARGET_073' })
            $button = Get-ById $list 'OpenGlobalHitButton'
            if ($null -eq $button) {
                $buttons = @(Find-All $list $CT::Button)
                if ($buttons.Count -eq 1) { $button = $buttons[0] }
            }
        }
        if ($null -ne $list -and -not $list.Current.IsOffscreen -and $texts.Count -eq 1 -and $null -ne $button) { $button }
    } 25 'return to original global search result'
    $report.returnedToExactGlobalHit = $true
    Save-Shot '03-global-results-returned.png'

    $report.stage = 'worker-cleanup'
    $children = Get-DirectChildren $app.Id
    $workerProcesses = @($children | Where-Object { $_.Name -match 'python|WechatMemory\.Worker' })
    $workerPids = @($workerProcesses | ForEach-Object { [int]$_.ProcessId })
    $report.workerProcessIds = $workerPids
    $report.workerProcessNames = @($workerProcesses | ForEach-Object { $_.Name })
    if ($workerPids.Count -lt 1) { throw 'No dedicated worker child process was observed.' }

    $report.stage = 'close'
    if (-not $app.CloseMainWindow()) { throw 'CloseMainWindow failed.' }
    if (-not $app.WaitForExit(15000)) { throw 'App did not exit after window close.' }
    $report.appExitCode = $app.ExitCode
    Wait-Until { @($workerPids | Where-Object { Test-Alive $_ }).Count -eq 0 } 10 'worker process reaping'
    $report.workerReaped = $true
    $report.stage = 'done'
    $report.ok = $true
    $report | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $outputDirectory 'report.json') -Encoding UTF8
    $report | ConvertTo-Json -Depth 6
} catch {
    $report.ok = $false
    $report.error = $_.Exception.Message
    $report.errorLocation = $_.InvocationInfo.PositionMessage
    $report.errorStack = $_.ScriptStackTrace
    try { Save-Shot '99-failure.png' } catch { }
    $report | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $outputDirectory 'report.json') -Encoding UTF8
    throw
} finally {
    if ($null -ne $app) {
        try {
            $remainingChildren = @(Get-DirectChildren $app.Id | Where-Object { $_.Name -match 'python|WechatMemory\.Worker' } | ForEach-Object { [int]$_.ProcessId })
            $workerPids = @($workerPids + $remainingChildren | Select-Object -Unique)
        } catch { }
        try { if (-not $app.HasExited) { $app.Kill(); $app.WaitForExit() } } catch { }
    }
    foreach ($pidValue in $workerPids) {
        try { $process = Get-Process -Id $pidValue -ErrorAction SilentlyContinue; if ($process) { $process.Kill() } } catch { }
    }
    if ($null -ne $previousClipboard) {
        try { [System.Windows.Forms.Clipboard]::SetDataObject($previousClipboard, $true) } catch { }
    }
}
