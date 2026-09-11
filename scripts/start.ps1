$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Host 'Creating the base runtime...'
    & (Join-Path $PSScriptRoot 'setup.ps1')
    if ($LASTEXITCODE -ne 0) { throw 'Setup did not finish.' }
}
$desktopExe = Join-Path $projectRoot 'src\MADSearcher.Desktop\bin\Release\net9.0-windows\MADSearcher.exe'
& dotnet build 'src/MADSearcher.Desktop/MADSearcher.Desktop.csproj' -c Release --nologo
if ($LASTEXITCODE -ne 0) { throw 'Desktop build failed.' }
Start-Process -FilePath $desktopExe -WorkingDirectory $projectRoot
