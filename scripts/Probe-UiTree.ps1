﻿param([string]$Executable, [string]$OutputFile)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient,UIAutomationTypes
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $scriptDir
if (-not $Executable) { $Executable = Join-Path $root 'src/NativeApp/bin/x64/Debug/net10.0-windows10.0.19041.0/win-x64/NativeApp.exe' }
if (-not $OutputFile) { $OutputFile = Join-Path $root 'artifacts/ui-tree.json' }
$AE = [System.Windows.Automation.AutomationElement]
$TS = [System.Windows.Automation.TreeScope]
$walker = [System.Windows.Automation.TreeWalker]::RawViewWalker
function Get-ById($parent, [string]$id) {
    $condition = New-Object System.Windows.Automation.PropertyCondition($AE::AutomationIdProperty, $id)
    return $parent.FindFirst($TS::Descendants, $condition)
}
function Get-Patterns($element) {
    $supported = @()
    $pairs = @(
        @('Invoke', [System.Windows.Automation.InvokePattern]::Pattern),
        @('Value', [System.Windows.Automation.ValuePattern]::Pattern),
        @('SelectionItem', [System.Windows.Automation.SelectionItemPattern]::Pattern),
        @('ExpandCollapse', [System.Windows.Automation.ExpandCollapsePattern]::Pattern),
        @('Toggle', [System.Windows.Automation.TogglePattern]::Pattern),
        @('Text', [System.Windows.Automation.TextPattern]::Pattern)
    )
    foreach ($pair in $pairs) {
        $holder = $null
        try { if ($element.TryGetCurrentPattern($pair[1], [ref]$holder)) { $supported += $pair[0] } } catch { }
    }
    return $supported
}
function Dump-Node($element, [int]$depth) {
    if ($null -eq $element -or $depth -gt 12) { return }
    $script:nodes.Add([ordered]@{
        depth = $depth
        type = $element.Current.ControlType.ProgrammaticName
        name = $element.Current.Name
        automationId = $element.Current.AutomationId
        className = $element.Current.ClassName
        patterns = @(Get-Patterns $element)
    })
    $child = $walker.GetFirstChild($element)
    while ($null -ne $child) {
        Dump-Node $child ($depth + 1)
        $child = $walker.GetNextSibling($child)
    }
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
    $end = Get-ById $window 'EndDate'
    $apply = Get-ById $window 'ApplyFilterButton'
    $summary = Get-ById $window 'MessageSummary'
    $result = [ordered]@{
        start = $(if ($start) { [ordered]@{ name=$start.Current.Name; id=$start.Current.AutomationId; type=$start.Current.ControlType.ProgrammaticName; patterns=@(Get-Patterns $start) } } else { $null })
        end = $(if ($end) { [ordered]@{ name=$end.Current.Name; id=$end.Current.AutomationId; type=$end.Current.ControlType.ProgrammaticName; patterns=@(Get-Patterns $end) } } else { $null })
        apply = $(if ($apply) { [ordered]@{ name=$apply.Current.Name; id=$apply.Current.AutomationId } } else { $null })
        summary = $(if ($summary) { $summary.Current.Name } else { $null })
        startTree = @()
        startTreeFocused = @()
    }
    if ($start) { Dump-Node $start 0; $result.startTree = @($script:nodes) }
    if ($start) {
        $start.SetFocus()
        Start-Sleep -Milliseconds 500
        $script:nodes.Clear()
        Dump-Node $start 0
        $result.startTreeFocused = @($script:nodes)
    }
    $result | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $OutputFile -Encoding UTF8
    Write-Output "written $OutputFile"
} finally {
    if (-not $app.HasExited) {
        $app.CloseMainWindow() | Out-Null
        if (-not $app.WaitForExit(10000)) { $app.Kill(); $app.WaitForExit() }
    }
}
