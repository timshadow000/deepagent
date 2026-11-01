$ErrorActionPreference = "Stop"

Write-Host "DeepSeek Desktop — установка" -ForegroundColor Cyan

$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) {
    $py = Get-Command python3 -ErrorAction SilentlyContinue
}
if (-not $py) {
    Write-Host "Python не найден. Скачиваю..." -ForegroundColor Yellow
    $installer = "$env:TEMP\python_installer.exe"
    Invoke-WebRequest "https://www.python.org/ftp/python/3.12.4/python-3.12.4-amd64.exe" -OutFile $installer
    Start-Process $installer "/quiet InstallAllUsers=0 PrependPath=1 Include_pip=1" -Wait
    $env:PATH = [System.Environment]::GetEnvironmentVariable("PATH","User") + ";" + $env:PATH
    $py = Get-Command python -ErrorAction SilentlyContinue
    if (-not $py) {
        Write-Host "Python всё ещё не найден после установки. Перезапусти терминал и попробуй снова." -ForegroundColor Red
        exit 1
    }
}

$pyExe = $py.Source
Write-Host "Python: $pyExe" -ForegroundColor Green

Write-Host "Устанавливаю зависимости..." -ForegroundColor Cyan
& $pyExe -m pip install --upgrade pip --quiet
& $pyExe -m pip install pywebview openai openpyxl pypdf --quiet

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$appFile = Join-Path $scriptDir "deepagent.py"

if (-not (Test-Path $appFile)) {
    Write-Host "deepagent.py не найден рядом с install.ps1. Скачиваю..." -ForegroundColor Yellow
    Invoke-WebRequest "https://raw.githubusercontent.com/timshadow000/deepagent/refs/heads/main/install/deepagent.py" -OutFile $appFile
}

if (-not (Test-Path $appFile)) {
    Write-Host "Не удалось получить deepagent.py." -ForegroundColor Red
    exit 1
}

Write-Host "Запускаю приложение..." -ForegroundColor Green
Start-Process $pyExe $appFile
