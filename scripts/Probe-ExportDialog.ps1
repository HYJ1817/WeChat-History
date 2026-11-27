param([string]$Executable, [string]$OutputDirectory)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient,UIAutomationTypes,System.Drawing,System.Windows.Forms
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $scriptDir
if (-not $Executable) { $Executable = Join-Path $root 'src/NativeApp/bin/x64/Debug/net10.0-windows10.0.19041.0/win-x64/NativeApp.exe' }
if (-not $OutputDirectory) { $OutputDirectory = Join-Path $root 'artifacts/export-dialog-probe' }
New-Item -ItemType Directory -Force $OutputDirectory | Out-Null
$AE = [System.Windows.Automation.AutomationElement]
$TS = [System.Windows.Automation.TreeScope]
$walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
function Get-ById($parent, [string]$id) {
    $condition = New-Object System.Windows.Automation.PropertyCondition($AE::AutomationIdProperty, $id)
    return $parent.FindFirst($TS::Descendants, $condition)
}
function Dump($element, [int]$depth) {
    if ($null -eq $element -or $depth -gt 5) { return }
    $script:nodes.Add([ordered]@{ depth=$depth; type=$element.Current.ControlType.ProgrammaticName; name=$element.Current.Name; id=$element.Current.AutomationId; className=$element.Current.ClassName })
    $child = $walker.GetFirstChild($element)
    while ($null -ne $child) { Dump $child ($depth + 1); $child = $walker.GetNextSibling($child) }
}
$app = Start-Process -FilePath (Resolve-Path -LiteralPath $Executable).Path -PassThru
try {
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    do { Start-Sleep -Milliseconds 250; $app.Refresh() } until ($app.MainWindowHandle -ne [IntPtr]::Zero -or [DateTime]::UtcNow -gt $deadline)
    if ($app.MainWindowHandle -eq [IntPtr]::Zero) { throw 'No window.' }
    $window = $AE::FromHandle($app.MainWindowHandle)
    Start-Sleep -Seconds 4
    $export = Get-ById $window 'ExportButton'
    if (-not $export) { throw 'ExportButton not found.' }
    $export.SetFocus()
    $export.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
    Start-Sleep -Seconds 3
    $screen = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
    $bitmap = New-Object System.Drawing.Bitmap($screen.Width, $screen.Height)
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    try { $graphics.CopyFromScreen($screen.Left, $screen.Top, 0, 0, $screen.Size) }
    finally { $graphics.Dispose() }
    try { $bitmap.Save((Join-Path $OutputDirectory 'desktop.png'), [System.Drawing.Imaging.ImageFormat]::Png) }
    finally { $bitmap.Dispose() }
    $topWindows = @()
    foreach ($top in $AE::RootElement.FindAll($TS::Children, [System.Windows.Automation.Condition]::TrueCondition)) {
        $topWindows += [ordered]@{ name=$top.Current.Name; className=$top.Current.ClassName; type=$top.Current.ControlType.ProgrammaticName; pid=$top.Current.ProcessId }
    }
    $script:nodes = New-Object System.Collections.ArrayList
    Dump $window 0
    [ordered]@{ appPid=$app.Id; topWindows=$topWindows; appTree=@($script:nodes) } | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $OutputDirectory 'probe.json') -Encoding UTF8
    Write-Output "written $(Join-Path $OutputDirectory 'probe.json')"
} finally {
    try { if (-not $app.HasExited) { $app.Kill(); $app.WaitForExit() } } catch { }
}
