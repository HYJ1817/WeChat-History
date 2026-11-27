param([string]$Executable, [string]$OutputFile)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient,UIAutomationTypes
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $scriptDir
if (-not $Executable) { $Executable = Join-Path $root 'src/NativeApp/bin/x64/Debug/net10.0-windows10.0.19041.0/win-x64/WeChat-History.exe' }
if (-not $OutputFile) { $OutputFile = Join-Path $root 'artifacts/save-dialog-tree.json' }
$AE = [System.Windows.Automation.AutomationElement]
$TS = [System.Windows.Automation.TreeScope]
function Get-ById($parent, [string]$id) {
    $condition = New-Object System.Windows.Automation.PropertyCondition($AE::AutomationIdProperty, $id)
    return $parent.FindFirst($TS::Descendants, $condition)
}
function Get-Patterns($element) {
    $supported = @()
    $pairs = @(
        @('Invoke', [System.Windows.Automation.InvokePattern]::Pattern),
        @('Value', [System.Windows.Automation.ValuePattern]::Pattern),
        @('Text', [System.Windows.Automation.TextPattern]::Pattern),
        @('SelectionItem', [System.Windows.Automation.SelectionItemPattern]::Pattern),
        @('ExpandCollapse', [System.Windows.Automation.ExpandCollapsePattern]::Pattern)
    )
    foreach ($pair in $pairs) {
        $holder = $null
        try { if ($element.TryGetCurrentPattern($pair[1], [ref]$holder)) { $supported += $pair[0] } } catch { }
    }
    return $supported
}
$app = Start-Process -FilePath (Resolve-Path -LiteralPath $Executable).Path -PassThru
try {
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    do { Start-Sleep -Milliseconds 250; $app.Refresh() } until ($app.MainWindowHandle -ne [IntPtr]::Zero -or [DateTime]::UtcNow -gt $deadline)
    if ($app.MainWindowHandle -eq [IntPtr]::Zero) { throw 'No window.' }
    $window = $AE::FromHandle($app.MainWindowHandle)
    Start-Sleep -Seconds 4
    $export = Get-ById $window 'ExportButton'
    $export.SetFocus()
    $export.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
    $dialog = $null
    $deadline = [DateTime]::UtcNow.AddSeconds(20)
    do {
        Start-Sleep -Milliseconds 300
        $condition = New-Object System.Windows.Automation.PropertyCondition($AE::ControlTypeProperty, [System.Windows.Automation.ControlType]::Window)
        $found = @($window.FindAll($TS::Descendants, $condition) | Where-Object { $_.Current.ClassName -eq '#32770' })
        if ($found.Count -gt 0) { $dialog = $found[0] }
    } until ($dialog -or [DateTime]::UtcNow -gt $deadline)
    if (-not $dialog) { throw 'Dialog not found.' }
    $nodes = @()
    foreach ($element in $dialog.FindAll($TS::Descendants, [System.Windows.Automation.Condition]::TrueCondition)) {
        $nodes += [ordered]@{
            type = $element.Current.ControlType.ProgrammaticName
            name = $element.Current.Name
            id = $element.Current.AutomationId
            className = $element.Current.ClassName
            handle = $element.Current.NativeWindowHandle
            patterns = @(Get-Patterns $element)
        }
    }
    [ordered]@{ dialogName=$dialog.Current.Name; dialogHandle=$dialog.Current.NativeWindowHandle; nodes=$nodes } | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $OutputFile -Encoding UTF8
    Write-Output "written $OutputFile"
} finally {
    try { if (-not $app.HasExited) { $app.Kill(); $app.WaitForExit() } } catch { }
}
