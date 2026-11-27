param(
    [switch]$SkipSmokeTest
)
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot/Common.ps1"
Push-Location $ProjectRoot
try {
    $dotnet = Get-ProjectDotnet
    $python = Get-ProjectPython
    $publishRoot = Join-Path $ProjectRoot 'artifacts/publish'
    $publishDir = Join-Path $ProjectRoot '微信记忆App'
    $workerDist = Join-Path $publishRoot 'worker-dist'
    $workerWork = Join-Path $publishRoot 'worker-build'
    $workerSpec = Join-Path $publishRoot 'worker-spec'
    $project = Join-Path $ProjectRoot 'src/NativeApp/NativeApp.csproj'

    foreach ($dir in @($publishDir, $workerDist, $workerWork, $workerSpec)) {
        if (Test-Path -LiteralPath $dir) { Remove-Item -LiteralPath $dir -Recurse -Force }
    }
    New-Item -ItemType Directory -Force -Path $publishDir, $workerDist, $workerWork, $workerSpec | Out-Null

    $buildRequirements = Join-Path $ProjectRoot 'src/PythonWorker/requirements-build.txt'
    if (-not (Test-Path -LiteralPath $buildRequirements)) { throw 'src/PythonWorker/requirements-build.txt is missing.' }
    Invoke-Checked $python @('-I','-m','pip','install','--disable-pip-version-check','-r',$buildRequirements)

    Write-Output 'Publishing native app (self-contained win-x64)...'
    Invoke-Checked $dotnet @(
        'publish', $project, '-c', 'Release', '-r', 'win-x64',
        '--self-contained', 'true', '-p:Platform=x64', '-o', $publishDir, '--nologo'
    )

    $appExe = Join-Path $publishDir 'NativeApp.exe'
    if (-not (Test-Path -LiteralPath $appExe)) { throw 'dotnet publish did not produce NativeApp.exe.' }

    $pyinstaller = Join-Path $ProjectRoot '.venv/Scripts/pyinstaller.exe'
    if (-not (Test-Path -LiteralPath $pyinstaller)) { throw 'PyInstaller is not installed in the project venv.' }
    Write-Output 'Building the standalone worker with PyInstaller (onedir)...'
    # onedir avoids a 200+ MiB one-file extraction on every launch and is much
    # friendlier to antivirus software once frida and the speech stack are included.
    Invoke-Checked $pyinstaller @(
        '--noconfirm', '--clean', '--onedir',
        '--name', 'WechatMemory.Worker',
        '--distpath', $workerDist,
        '--workpath', $workerWork,
        '--specpath', $workerSpec,
        '--paths', (Join-Path $ProjectRoot 'src/PythonWorker/vendor'),
        '--hidden-import', 'frida',
        '--hidden-import', 'pysilk',
        '--hidden-import', 'faster_whisper',
        '--hidden-import', 'imageio_ffmpeg',
        '--hidden-import', 'pywinauto',
        '--hidden-import', 'comtypes',
        '--hidden-import', 'reportlab.pdfbase._fontdata',
        '--collect-submodules', 'wechat_memory',
        '--collect-submodules', 'wechat_context_exporter',
        '--collect-submodules', 'faster_whisper',
        '--collect-submodules', 'ctranslate2',
        '--collect-binaries', 'ctranslate2',
        '--collect-submodules', 'pywinauto',
        '--collect-submodules', 'comtypes',
        '--collect-all', 'imageio_ffmpeg',
        '--collect-all', 'pysilk',
        '--collect-data', 'reportlab',
        '--exclude-module', 'tkinter',
        '--exclude-module', 'matplotlib',
        '--exclude-module', 'IPython',
        '--exclude-module', 'pytest',
        (Join-Path $ProjectRoot 'src/PythonWorker/worker.py')
    )
    $builtWorker = Join-Path $workerDist 'WechatMemory.Worker/WechatMemory.Worker.exe'
    if (-not (Test-Path -LiteralPath $builtWorker)) { throw 'PyInstaller did not produce WechatMemory.Worker.exe.' }
    $workerTarget = Join-Path $publishDir 'worker'
    if (Test-Path -LiteralPath $workerTarget) { Remove-Item -LiteralPath $workerTarget -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $workerTarget | Out-Null
    Copy-Item -Path (Join-Path $workerDist 'WechatMemory.Worker/*') -Destination $workerTarget -Recurse -Force

    $workerExe = Join-Path $publishDir 'worker/WechatMemory.Worker.exe'
    if (-not (Test-Path -LiteralPath $workerExe)) { throw 'Published worker is missing.' }
    # A stale copy here silently ships old worker code; fail loudly instead.
    $builtHash = (Get-FileHash -LiteralPath $builtWorker -Algorithm SHA256).Hash
    $publishedHash = (Get-FileHash -LiteralPath $workerExe -Algorithm SHA256).Hash
    if ($builtHash -ne $publishedHash) { throw 'Published worker does not match the freshly built worker.' }

    # Demo JSON is a test fixture, not part of the product; the worker is
    # smoke-tested from the project fixture without shipping it.
    $demoFile = Join-Path $ProjectRoot 'demo/conversations.json'

    foreach ($name in @('THIRD_PARTY_NOTICES.md', 'README.md')) {
        $source = Join-Path $ProjectRoot $name
        if (Test-Path -LiteralPath $source) { Copy-Item -LiteralPath $source -Destination $publishDir -Force }
    }
    $licenseSource = Join-Path $ProjectRoot 'LICENSES'
    if (Test-Path -LiteralPath $licenseSource) {
        Copy-Item -LiteralPath $licenseSource -Destination $publishDir -Recurse -Force
    }
    # Preserve the license texts that ship inside Python package metadata and the
    # CPython/PyInstaller notices used by the bundled worker.
    $pythonLicenseTarget = Join-Path $publishDir 'LICENSES/python-runtime'
    New-Item -ItemType Directory -Force -Path $pythonLicenseTarget | Out-Null
    $basePrefix = (& $python -I -c "import sys; print(sys.base_prefix)").Trim()
    if (Test-Path -LiteralPath (Join-Path $basePrefix 'LICENSE.txt')) {
        Copy-Item -LiteralPath (Join-Path $basePrefix 'LICENSE.txt') -Destination $pythonLicenseTarget -Force
    }
    $sitePackages = Join-Path $ProjectRoot '.venv/Lib/site-packages'
    foreach ($info in @(Get-ChildItem -LiteralPath $sitePackages -Directory -Filter '*.dist-info' -ErrorAction SilentlyContinue)) {
        $licenses = Join-Path $info.FullName 'licenses'
        if (Test-Path -LiteralPath $licenses) {
            $target = Join-Path $pythonLicenseTarget $info.BaseName
            Copy-Item -LiteralPath $licenses -Destination $target -Recurse -Force
        }
    }
    $pyInstallerCopying = Join-Path $sitePackages 'pyinstaller-6.22.3.dist-info/licenses/COPYING.txt'
    if (Test-Path -LiteralPath $pyInstallerCopying) {
        Copy-Item -LiteralPath $pyInstallerCopying -Destination $pythonLicenseTarget -Force
    }
    $docsSource = Join-Path $ProjectRoot 'docs'
    if (Test-Path -LiteralPath $docsSource) {
        Copy-Item -LiteralPath $docsSource -Destination $publishDir -Recurse -Force
    }

    if (-not $SkipSmokeTest) {
        Write-Output 'Smoke-testing the packaged worker over its real stdio protocol...'
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = $workerExe
        $psi.Arguments = '--demo "' + $demoFile + '"'
        $psi.WorkingDirectory = $publishDir
        $psi.UseShellExecute = $false
        $psi.CreateNoWindow = $true
        $psi.RedirectStandardInput = $true
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
        $psi.StandardErrorEncoding = [System.Text.Encoding]::UTF8
        $process = [System.Diagnostics.Process]::Start($psi)

        function Send-Request([string]$line) {
            $process.StandardInput.WriteLine($line)
            $process.StandardInput.Flush()
        }
        function Read-ResponseLine([int]$seconds = 30) {
            $task = $process.StandardOutput.ReadLineAsync()
            if (-not $task.Wait([TimeSpan]::FromSeconds($seconds))) { throw 'Timed out waiting for a worker response.' }
            return $task.Result
        }

        try {
            $report = [ordered]@{ worker = $workerExe; demo = $demoFile }

            Send-Request '{"protocolVersion":1,"requestId":"smoke-hello","type":"request","command":"hello","payload":{}}'
            $hello = (Read-ResponseLine) | ConvertFrom-Json
            if (-not $hello.ok -or $hello.result.protocolVersion -ne 1) { throw 'Packaged worker hello failed.' }
            $report.hello = $hello.result

            Send-Request '{"protocolVersion":1,"requestId":"smoke-list","type":"request","command":"list_conversations","payload":{"source":"demo"}}'
            $list = (Read-ResponseLine) | ConvertFrom-Json
            if (-not $list.ok -or $list.result.items.Count -lt 3) { throw 'Packaged worker list_conversations failed.' }
            $report.conversationCount = $list.result.items.Count

            Send-Request '{"protocolVersion":1,"requestId":"smoke-messages","type":"request","command":"get_messages","payload":{"conversationId":"demo-chat-001","startDate":"2026-11-24","endDate":"2026-11-24","offset":0,"limit":50}}'
            $messages = (Read-ResponseLine) | ConvertFrom-Json
            if (-not $messages.ok -or $messages.result.total -ne 4) { throw 'Packaged worker date filter returned an unexpected total.' }
            $report.filteredMessages = $messages.result.total

            # The UI previews newest-first; this also catches a stale worker copy.
            Send-Request '{"protocolVersion":1,"requestId":"smoke-desc","type":"request","command":"get_messages","payload":{"conversationId":"demo-chat-001","order":"desc","offset":0,"limit":50}}'
            $desc = (Read-ResponseLine) | ConvertFrom-Json
            Send-Request '{"protocolVersion":1,"requestId":"smoke-asc","type":"request","command":"get_messages","payload":{"conversationId":"demo-chat-001","offset":0,"limit":50}}'
            $asc = (Read-ResponseLine) | ConvertFrom-Json
            if (-not $desc.ok -or -not $asc.ok -or $desc.result.total -ne $asc.result.total) { throw 'Packaged worker message ordering failed.' }
            if ($desc.result.items[0].id -eq $asc.result.items[0].id) { throw 'Packaged worker ignored the desc order parameter.' }
            if ([datetime]$desc.result.items[0].timestamp -lt [datetime]$desc.result.items[-1].timestamp) { throw 'Packaged worker desc order is not newest-first.' }
            $report.orderCheck = $true

            Send-Request '{"protocolVersion":1,"requestId":"smoke-shutdown","type":"request","command":"shutdown","payload":{}}'
            $shutdown = (Read-ResponseLine) | ConvertFrom-Json
            if (-not $shutdown.ok -or -not $shutdown.result.shutdown) { throw 'Packaged worker shutdown failed.' }

            $process.StandardInput.Close()
            if (-not $process.WaitForExit(15000)) { throw 'Packaged worker did not exit after shutdown.' }
            if ($process.ExitCode -ne 0) { throw "Packaged worker exited with code $($process.ExitCode)." }
            $report.exitCode = $process.ExitCode
            $stderrText = $process.StandardError.ReadToEnd()
            $report.stderrLineCount = @($stderrText -split "`r?`n" | Where-Object { $_.Trim().Length -gt 0 }).Count

            $report | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $publishRoot 'publish-report.json') -Encoding UTF8
            $report | ConvertTo-Json -Depth 6
        } finally {
            try { if (-not $process.HasExited) { $process.Kill(); $process.WaitForExit() } } catch { }
        }
    }

    $files = @(Get-ChildItem -LiteralPath $publishDir -Recurse -File)
    $size = ($files | Measure-Object -Property Length -Sum).Sum
    Write-Output ("Published to: " + $publishDir)
    Write-Output ("Files: " + $files.Count + ", Bytes: " + $size)
} finally { Pop-Location }
