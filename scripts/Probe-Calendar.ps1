param([string]$Executable, [string]$OutputFile)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient,UIAutomationTypes
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $scriptDir
if (-not $Executable) { $Executable = Join-Path $root 'src/NativeApp/bin/x64/Debug/net10.0-windows10.0.19041.0/win-x64/WeChat-History.exe' }
if (-not $OutputFile) { $OutputFile = Join-Path $root 'artifacts/calendar-tree.json' }
$AE = [System.Windows.Automation.AutomationElement]
$TS = [System.Windows.Automation.TreeScope]
$walker = [System.Windows.Automation.TreeWalker]::RawViewWalker
function Get-ById($parent, [string]$id) {
    $condition = New-Object System.Windows.Automation.PropertyCondition($AE::AutomationIdProperty, $id)
    return $parent.FindFirst($TS::Descendants, $condition)
}
function Dump($element, [int]$depth) {
    if ($null -eq $element -or $depth -gt 8) { return }
    $script:nodes.Add([ordered]@{
        depth = $depth
        type = $element.Current.ControlType.ProgrammaticName
        name = $element.Current.Name
        id = $element.Current.AutomationId
        className = $element.Current.ClassName
        patterns = @(Get-Patterns $element)
    })
    $child = $walker.GetFirstChild($element)
    while ($null -ne $child) { Dump $child ($depth + 1); $child = $walker.GetNextSibling($child) }
}
function Get-Patterns($element) {
    $supported = @()
    $pairs = @(
        @('Invoke', [System.Windows.Automation.InvokePattern]::Pattern),
        @('SelectionItem', [System.Windows.Automation.SelectionItemPattern]::Pattern),
        @('GridItem', [System.Windows.Automation.GridItemPattern]::Pattern),
        @('Value', [System.Windows.Automation.ValuePattern]::Pattern)
    )
    foreach ($pair in $pairs) {
        $holder = $null
        try { if ($element.TryGetCurrentPattern($pair[1], [ref]$holder)) { $supported += $pair[0] } } catch { }
    }
    return $supported
}
$script:nodes = New-Object System.Collections.ArrayList
$app = Start-Process -FilePath (Resolve-Path -LiteralPath $Executable).Path -PassThru
try {
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    do { Start-Sleep -Milliseconds 250; $app.Refresh() } until ($app.MainWindowHandle -ne [IntPtr]::Zero -or [DateTime]::UtcNow -gt $deadline)
    if ($app.MainWindowHandle -eq [IntPtr]::Zero) { throw 'No window.' }
    $window = $AE::FromHandle($app.MainWindowHandle)
    Start-Sleep -Seconds 4
    $start = Get-ById $window 'StartDate'
    $start.SetFocus()
    $start.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
    $calendar = $null
    $deadline = [DateTime]::UtcNow.AddSeconds(10)
    do {
        Start-Sleep -Milliseconds 250
        $calendar = Get-ById $window 'CalendarView'
    } until ($calendar -or [DateTime]::UtcNow -gt $deadline)
    if (-not $calendar) { throw 'CalendarView did not appear.' }
    Dump $calendar 0
    [ordered]@{ calendar = @($script:nodes) } | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $OutputFile -Encoding UTF8
    Write-Output "written $OutputFile"
} finally {
    if (-not $app.HasExited) {
        try { $app.CloseMainWindow() | Out-Null } catch { }
        if (-not $app.WaitForExit(10000)) { $app.Kill(); $app.WaitForExit() }
    }
}
