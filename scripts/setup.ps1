param(
    [switch]$WithSam,
    [switch]$WithWhisper,
    [switch]$Cuda,
    [switch]$Direct,
    [string]$Python = 'python'
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$env:PIP_CACHE_DIR = Join-Path $projectRoot 'models\pip-cache'
$setupTemp = Join-Path $projectRoot 'workspace\setup-temp'
New-Item -ItemType Directory -Path $setupTemp -Force | Out-Null
$env:TEMP = $setupTemp
$env:TMP = $setupTemp
if ($Direct) {
    $env:HTTP_PROXY = ''
    $env:HTTPS_PROXY = ''
    $env:ALL_PROXY = ''
}
function Invoke-Checked {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Command failed: $Program (exit $LASTEXITCODE)" }
}
$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) {
    Invoke-Checked $Python @('-m', 'venv', '.venv')
}
Invoke-Checked $venvPython @('-m','pip','install','--disable-pip-version-check','--upgrade','pip')
Invoke-Checked $venvPython @('-m','pip','install','--disable-pip-version-check','-r','worker/requirements.txt')
if ($WithSam) {
    $torchIndex = if ($Cuda) { 'https://download.pytorch.org/whl/cu124' } else { 'https://download.pytorch.org/whl/cpu' }
    Invoke-Checked $venvPython @('-m','pip','install','--disable-pip-version-check','torch==2.5.1','torchvision==0.20.1','--index-url',$torchIndex)
    $downloadArgs = @('scripts/download_models.py')
    if ($Direct) { $downloadArgs += '--direct' }
    Invoke-Checked $venvPython $downloadArgs
    Invoke-Checked $venvPython @('-m','pip','install','--disable-pip-version-check','setuptools>=69','wheel','hydra-core==1.3.2','iopath==0.1.10','tqdm>=4.66')
    $env:SAM2_BUILD_CUDA = '0'
    Invoke-Checked $venvPython @('-m','pip','install','--disable-pip-version-check','--no-build-isolation','--no-deps','models/sam2-source.zip')
}
if ($WithWhisper) {
    Invoke-Checked $venvPython @('-m','pip','install','--disable-pip-version-check','faster-whisper==1.2.1')
}
Invoke-Checked 'dotnet' @('build','src/MADSearcher.Desktop/MADSearcher.Desktop.csproj','-c','Release','--nologo')
Write-Host 'Setup complete. Start with scripts/start.ps1 or MADSearcher.cmd.'
