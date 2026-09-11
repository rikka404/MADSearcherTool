$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$publishDir = Join-Path $projectRoot 'artifacts\app'
& dotnet publish 'src/MADSearcher.Desktop/MADSearcher.Desktop.csproj' -c Release -o $publishDir --no-self-contained --nologo
if ($LASTEXITCODE -ne 0) { throw 'Publish failed.' }
Write-Host "Published to $publishDir. Keep this directory within the repository; worker/runtime are resolved from the root."
