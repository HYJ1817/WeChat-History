param()
. "$PSScriptRoot/Common.ps1"
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT -or -not [Environment]::Is64BitOperatingSystem) { throw 'Windows x64 is required.' }
Write-Output ('Windows: ' + [Environment]::OSVersion.VersionString)
Write-Output ('Interactive desktop: ' + [Environment]::UserInteractive)
Push-Location $ProjectRoot
try {
    Invoke-Checked (Get-ProjectDotnet) @('--info')
    $py = Join-Path $ProjectRoot '.venv/Scripts/python.exe'
    if (Test-Path -LiteralPath $py) { Invoke-Checked $py @('-I','-c','import sys; print(sys.version); print(sys.prefix)') }
    else { Write-Warning 'Project Python environment is not ready. Run Prepare-Python.ps1.' }
    $kits = Join-Path ${env:ProgramFiles(x86)} 'Windows Kits/10/bin'
    Write-Output ('Windows SDK tools present: ' + (Test-Path -LiteralPath $kits))
    Write-Output 'No machine installation or system setting change was performed by this check.'
} finally { Pop-Location }
