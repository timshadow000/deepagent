$ErrorActionPreference = "Stop"

Write-Host "DeepSeek Desktop — установка" -ForegroundColor Cyan
Write-Host ""
Write-Host "ВНИМАНИЕ: При первом запуске может появиться ошибка 'maximum recursion depth exceeded'." -ForegroundColor Yellow
Write-Host "Это известная проблема с некоторыми версиями Python. Просто закройте и перезапустите приложение." -ForegroundColor Yellow
Write-Host ""

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
        Write-Host "Python всё ещё не найден. Перезапусти терминал и попробуй снова." -ForegroundColor Red
        exit 1
    }
}

$pyExe = $py.Source
Write-Host "Python: $pyExe" -ForegroundColor Green

Write-Host "Устанавливаю зависимости..." -ForegroundColor Cyan
& $pyExe -m pip install pywebview openai openpyxl pypdf --quiet 2>&1 | Out-Null

$installDir = "$env:USERPROFILE\DeepSeekDesktop"
if (-not (Test-Path $installDir)) {
    [void](New-Item -ItemType Directory -Path $installDir)
}

$appFile = Join-Path $installDir "deepagent.py"

if (-not (Test-Path $appFile)) {
    Write-Host "Скачиваю deepagent.py..." -ForegroundColor Yellow
    Invoke-WebRequest "https://raw.githubusercontent.com/timshadow000/deepagent/refs/heads/main/install/deepagent.py" -OutFile $appFile
}

if (-not (Test-Path $appFile)) {
    Write-Host "Не удалось получить deepagent.py." -ForegroundColor Red
    exit 1
}

$shortcutPath = "$env:USERPROFILE\Desktop\DeepSeek.lnk"
$wsh = New-Object -ComObject WScript.Shell
$shortcut = $wsh.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $pyExe
$shortcut.Arguments = "`"$appFile`""
$shortcut.WorkingDirectory = $installDir
$shortcut.WindowStyle = 1
$shortcut.Description = "DeepSeek Desktop"
$shortcut.Save()
Write-Host "Ярлык создан на рабочем столе." -ForegroundColor Green

$profileDir = Split-Path $PROFILE
if (-not (Test-Path $profileDir)) {
    [void](New-Item -ItemType Directory -Path $profileDir)
}
if (-not (Test-Path $PROFILE)) {
    [void](New-Item -ItemType File -Path $PROFILE)
}

$funcLine = "function ds-agent { Start-Process `"$pyExe`" `"$appFile`" }"
$profileContent = Get-Content $PROFILE -Raw -ErrorAction SilentlyContinue
if ($profileContent -notlike "*ds-agent*") {
    Add-Content $PROFILE "`n$funcLine"
    Write-Host "Команда 'ds-agent' добавлена в PowerShell профиль." -ForegroundColor Green
} else {
    Write-Host "Команда 'ds-agent' уже есть в профиле." -ForegroundColor Green
}

. $PROFILE 2>&1 | Out-Null

Write-Host ""
Write-Host "Готово! Теперь можешь запускать приложение командой: ds-agent" -ForegroundColor Cyan
