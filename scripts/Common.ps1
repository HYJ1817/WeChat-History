$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$env:DOTNET_CLI_HOME = Join-Path $ProjectRoot '.tools/cli'
$env:DOTNET_CLI_TELEMETRY_OPTOUT = '1'
$env:DOTNET_SKIP_FIRST_TIME_EXPERIENCE = '1'
$env:DOTNET_GENERATE_ASPNET_CERTIFICATE = 'false'
$env:DOTNET_NOLOGO = '1'
$env:PYTHONUTF8 = '1'

function Get-ProjectDotnet {
    $local = Join-Path $ProjectRoot '.tools/dotnet/dotnet.exe'
    if (Test-Path -LiteralPath $local) { return $local }
    $system = Get-Command dotnet -ErrorAction SilentlyContinue
    if (-not $system) { throw 'No .NET SDK. Run scripts/Bootstrap-Dotnet.ps1 or install the documented SDK yourself.' }
    return $system.Source
}
function Get-ProjectPython {
    $python = Join-Path $ProjectRoot '.venv/Scripts/python.exe'
    if (-not (Test-Path -LiteralPath $python)) { throw 'Run scripts/Prepare-Python.ps1 first.' }
    return $python
}
function Invoke-Checked {
    param([string]$File, [string[]]$Arguments)
    & $File @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Command failed (exit $LASTEXITCODE): $File" }
}
