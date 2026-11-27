param([string]$PythonExecutable)
. "$PSScriptRoot/Common.ps1"
Push-Location $ProjectRoot
try {
    $venv = Join-Path $ProjectRoot '.venv/Scripts/python.exe'
    if (-not (Test-Path -LiteralPath $venv)) {
        if (-not $PythonExecutable) {
            if (-not (Get-Command py -ErrorAction SilentlyContinue)) { throw 'Provide -PythonExecutable with an installed Python 3.11 or 3.13 x64 interpreter. No machine installation is performed.' }
            # Query registered runtimes; do not search user folders or invoke Store install.
            $registered = @(& py --list-paths 2>&1)
            foreach ($series in @('3.11','3.13')) {
                foreach ($line in $registered) {
                    if ("$line" -match ('^\s*-V:\S*' + [regex]::Escape($series) + '\S*\s+\*?\s*(.+python.exe)\s*$')) {
                        $PythonExecutable = $Matches[1].Trim()
                        break
                    }
                }
                if ($PythonExecutable) { break }
            }
        }
        if (-not $PythonExecutable -or -not (Test-Path -LiteralPath $PythonExecutable -PathType Leaf)) { throw 'No supported interpreter found. Provide -PythonExecutable. This script will not install Python system-wide.' }
        Invoke-Checked $PythonExecutable @('-I','-c','import sys; assert (3,11) <= sys.version_info[:2] <= (3,13) and sys.maxsize > 2**32')
        Invoke-Checked $PythonExecutable @('-I','-m','venv',(Join-Path $ProjectRoot '.venv'))
    }
    Invoke-Checked $venv @('-I','-c','import sys; print(sys.version); print(sys.prefix)')
    $requirements = Join-Path $ProjectRoot 'src/PythonWorker/requirements.txt'
    if (Test-Path -LiteralPath $requirements) { Invoke-Checked $venv @('-I','-m','pip','install','--disable-pip-version-check','-r',$requirements) }
    Write-Output 'Project Python environment is ready.'
} finally { Pop-Location }
