param(
    [Parameter(Mandatory=$true)][string]$Executable,
    [string]$OutputDirectory
)
$ErrorActionPreference = 'Stop'
if (-not $OutputDirectory) { $OutputDirectory = Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) '../artifacts/empty-window' }
Add-Type -AssemblyName UIAutomationClient,UIAutomationTypes,System.Drawing
Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class WindowCapture {
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hwnd, out RECT rect);
    [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr hwnd, IntPtr hdc, uint flags);
}
'@
New-Item -ItemType Directory -Force $OutputDirectory | Out-Null
$p = Start-Process -FilePath (Resolve-Path -LiteralPath $Executable).Path -PassThru
try {
    $until = [DateTime]::UtcNow.AddSeconds(30)
    do {
        Start-Sleep -Milliseconds 250
        $p.Refresh()
        if ($p.HasExited) { throw "Native application exited early: $($p.ExitCode)" }
    } until ($p.MainWindowHandle -ne [IntPtr]::Zero -or [DateTime]::UtcNow -gt $until)
    if ($p.MainWindowHandle -eq [IntPtr]::Zero) { throw 'No native window appeared within 30 seconds.' }
    Start-Sleep -Seconds 2
    $window = [System.Windows.Automation.AutomationElement]::FromHandle($p.MainWindowHandle)
    $elements = $window.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
    $names = @($elements | ForEach-Object { $_.Current.Name } | Where-Object { $_ })
    $rect = New-Object WindowCapture+RECT
    if (-not [WindowCapture]::GetWindowRect($p.MainWindowHandle, [ref]$rect)) { throw 'GetWindowRect failed.' }
    $bitmap = New-Object System.Drawing.Bitmap(($rect.Right-$rect.Left),($rect.Bottom-$rect.Top))
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    $dc = $graphics.GetHdc()
    try { if (-not [WindowCapture]::PrintWindow($p.MainWindowHandle,$dc,2)) { throw 'PrintWindow failed.' } }
    finally { $graphics.ReleaseHdc($dc); $graphics.Dispose() }
    try { $bitmap.Save((Join-Path $OutputDirectory 'window.png'), [System.Drawing.Imaging.ImageFormat]::Png) }
    finally { $bitmap.Dispose() }
    $result = @{ processId=$p.Id; title=$p.MainWindowTitle; handle=$p.MainWindowHandle.ToInt64(); automationNames=$names; utc=[DateTime]::UtcNow.ToString('o') }
    $result | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $OutputDirectory 'probe.json') -Encoding UTF8
    $result | ConvertTo-Json -Depth 5
    if (-not $p.CloseMainWindow()) { throw 'Native WM_CLOSE could not be sent.' }
    if (-not $p.WaitForExit(10000)) { throw 'Application did not close.' }
    if ($p.ExitCode -ne 0) { throw "Native application closed with $($p.ExitCode)" }
} finally {
    if (-not $p.HasExited) { $p.Kill(); $p.WaitForExit() }
    $p.Dispose()
}
