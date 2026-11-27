param([ValidateSet('Debug','Release')][string]$Configuration = 'Debug', [switch]$NoBuild)
. "$PSScriptRoot/Common.ps1"
if (-not $NoBuild) { & "$PSScriptRoot/Build.ps1" -Configuration $Configuration }
$exe = Join-Path $ProjectRoot "src/NativeApp/bin/x64/$Configuration/net10.0-windows10.0.19041.0/win-x64/WeChat-History.exe"
if (-not (Test-Path -LiteralPath $exe)) { throw 'WeChat-History.exe not found. Build first.' }
if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot '.venv/Scripts/python.exe'))) { throw 'Run scripts/Prepare-Python.ps1 first.' }
Start-Process -FilePath $exe -WorkingDirectory $ProjectRoot
