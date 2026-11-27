param([ValidateSet('Debug','Release')][string]$Configuration = 'Debug')
. "$PSScriptRoot/Common.ps1"
Push-Location $ProjectRoot
try {
    $dotnet = Get-ProjectDotnet
    $project = Join-Path $ProjectRoot 'src/NativeApp/NativeApp.csproj'
    Invoke-Checked $dotnet @('restore',$project,'-p:Platform=x64')
    Invoke-Checked $dotnet @('build',$project,'-c',$Configuration,'-p:Platform=x64','--no-restore','--nologo')
} finally { Pop-Location }
