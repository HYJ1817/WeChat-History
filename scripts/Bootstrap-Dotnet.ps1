# Project-local ZIP installation only; no administrator, PATH, registry or machine install.
param()
. "$PSScriptRoot/Common.ps1"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$version = (Get-Content -LiteralPath (Join-Path $ProjectRoot 'global.json') -Raw | ConvertFrom-Json).sdk.version
$dest = Join-Path $ProjectRoot '.tools/dotnet'
if (Test-Path -LiteralPath (Join-Path $dest 'dotnet.exe')) {
    Invoke-Checked (Join-Path $dest 'dotnet.exe') @('--version')
    exit 0
}
if ((Test-Path -LiteralPath $dest) -and (Get-ChildItem -LiteralPath $dest -Force | Select-Object -First 1)) { throw 'SDK destination must be empty.' }
$download = Join-Path $ProjectRoot ('.tools/downloads/sdk-' + $version + '-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory $download | Out-Null
$metadata = Join-Path $download 'releases.json'
Invoke-WebRequest -UseBasicParsing 'https://builds.dotnet.microsoft.com/dotnet/release-metadata/10.0/releases.json' -OutFile $metadata
$release = (Get-Content -LiteralPath $metadata -Raw | ConvertFrom-Json).releases | Where-Object { $_.sdk.version -eq $version } | Select-Object -First 1
if (-not $release) { throw "Official metadata has no SDK $version" }
$file = $release.sdk.files | Where-Object { $_.rid -eq 'win-x64' -and $_.name -like '*.zip' } | Select-Object -First 1
if (-not $file -or -not $file.url.StartsWith('https://builds.dotnet.microsoft.com/')) { throw 'Unexpected official download metadata.' }
$archive = Join-Path $download 'sdk.zip'
Invoke-WebRequest -UseBasicParsing $file.url -OutFile $archive
if ((Get-FileHash -LiteralPath $archive -Algorithm SHA512).Hash.ToLowerInvariant() -ne $file.hash.ToLowerInvariant()) { throw 'SDK SHA512 verification failed.' }
New-Item -ItemType Directory -Force $dest | Out-Null
Expand-Archive -LiteralPath $archive -DestinationPath $dest
Invoke-Checked (Join-Path $dest 'dotnet.exe') @('--version')
