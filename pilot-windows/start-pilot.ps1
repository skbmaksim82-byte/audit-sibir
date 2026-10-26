# Аудит магазина — сервер пилота на этом компьютере + туннель tuna.am.
# Запускается из start-pilot.bat. Пока окно открыто, приложение на GitHub Pages
# работает с общими данными: визиты, замечания ДП, фото, фотоотчёт.
# Закрыли окно — сервер остановлен, данные остаются в папке audit-sibir-pilot\data.

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"
$ProgressPreference = "SilentlyContinue"   # иначе Invoke-WebRequest качает в разы медленнее

$Repo    = "skbmaksim82-byte/audit-sibir"
$Pages   = "https://skbmaksim82-byte.github.io/audit-sibir/"
$Stores  = "11472"
$Port    = 8010
$Root    = Join-Path $env:USERPROFILE "audit-sibir-pilot"
$App     = Join-Path $Root "app"
$Data    = Join-Path $Root "data"
$Bin     = Join-Path $Root "bin"
$Venv    = Join-Path $Root "venv"
$Logs    = Join-Path $Root "logs"
$SubFile = Join-Path $Root "tuna-domain.txt"
$PwFile  = Join-Path $Data "ПАРОЛИ-не-пересылать.txt"

function Step($t) { Write-Host ""; Write-Host "== $t" -ForegroundColor Cyan }
function Fail($t) { Write-Host ""; Write-Host "ОШИБКА: $t" -ForegroundColor Red; Read-Host "Нажмите Enter, чтобы закрыть"; exit 1 }

New-Item -ItemType Directory -Force -Path $Root, $Data, $Bin, $Logs | Out-Null
$Host.UI.RawUI.WindowTitle = "Аудит магазина — сервер пилота (не закрывайте)"

# --- компьютер не засыпает, пока открыто это окно ---------------------------------
Add-Type -Namespace Audit -Name Power -MemberDefinition '[DllImport("kernel32.dll")] public static extern uint SetThreadExecutionState(uint f);'
[void][Audit.Power]::SetThreadExecutionState([uint32]"0x80000001")   # ES_CONTINUOUS | ES_SYSTEM_REQUIRED

# --- остановить сервер и туннель от прошлого запуска --------------------------------
# python.exe из venv на Windows запускает дочерний python — гасим всё дерево и всё, что слушает порт.
foreach ($n in "server.pid", "tuna.pid") {
  $f = Join-Path $Root $n
  if (Test-Path $f) { $id = (Get-Content $f -Raw).Trim(); if ($id) { & taskkill.exe /PID $id /T /F 2>&1 | Out-Null }; Remove-Item $f -Force }
}
try { Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop | ForEach-Object { & taskkill.exe /PID $_.OwningProcess /T /F 2>&1 | Out-Null } } catch {}
Get-Process tuna -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
  Where-Object { $_.CommandLine -like "*uvicorn*app:app*$Port*" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 1

# --- 1. свежая версия приложения с GitHub ------------------------------------------
Step "Загружаю приложение с GitHub"
$zip = Join-Path $Root "app.zip"; $tmp = Join-Path $Root "app-new"; $old = Join-Path $Root "app-old"
try {
  Invoke-WebRequest "https://github.com/$Repo/archive/refs/heads/main.zip" -OutFile $zip -UseBasicParsing
  if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
  Expand-Archive $zip -DestinationPath $tmp -Force
  $src = Get-ChildItem $tmp -Directory | Select-Object -First 1
  if (Test-Path $old) { Remove-Item $old -Recurse -Force -ErrorAction SilentlyContinue }
  $swapped = $true
  if (Test-Path $App) { try { Rename-Item $App $old -ErrorAction Stop } catch { $swapped = $false } }
  if ($swapped) {
    Move-Item $src.FullName $App
    Remove-Item $old -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "Готово."
  } else { Write-Host "Папка приложения занята, запускаю прошлую версию." -ForegroundColor Yellow }
  Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue; Remove-Item $zip -Force -ErrorAction SilentlyContinue
} catch {
  if (-not (Test-Path (Join-Path $App "server\app.py")) -and (Test-Path (Join-Path $old "server\app.py"))) { Rename-Item $old $App }
  if (Test-Path (Join-Path $App "server\app.py")) { Write-Host "GitHub недоступен, запускаю прошлую версию." -ForegroundColor Yellow }
  else { Fail "Не удалось скачать приложение с GitHub: $($_.Exception.Message)" }
}

# --- 2. Python ---------------------------------------------------------------------
Step "Проверяю Python"
function Find-Python {
  foreach ($c in @("py -3", "python")) {
    try {
      $parts = $c.Split(" ")
      $v = & $parts[0] $parts[1..9] -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
      if ($LASTEXITCODE -eq 0 -and $v -and [version]$v -ge [version]"3.10") { return $c }
    } catch {}
  }
  foreach ($p in Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe" -ErrorAction SilentlyContinue | Sort-Object FullName -Descending) { return "`"$($p.FullName)`"" }
  return $null
}
$py = Find-Python
if (-not $py) {
  Write-Host "Python не найден, ставлю Python 3.12 (пара минут)…"
  try { winget install --id Python.Python.3.12 -e --scope user --silent --accept-package-agreements --accept-source-agreements | Out-Host } catch {}
  $py = Find-Python
  if (-not $py) { Fail "Не получилось поставить Python. Поставьте его с python.org (галочка «Add to PATH») и запустите файл снова." }
}
Write-Host "Python: $py"

if (-not (Test-Path (Join-Path $Venv "Scripts\python.exe"))) {
  Step "Готовлю окружение (один раз)"
  Invoke-Expression "& $py -m venv `"$Venv`""
}
$vpy = Join-Path $Venv "Scripts\python.exe"
& $vpy -m pip install -q --disable-pip-version-check -r (Join-Path $App "server\requirements.txt")
if ($LASTEXITCODE -ne 0) { Fail "Не установились пакеты FastAPI. Проверьте интернет и запустите снова." }

# --- 3. tuna (туннель в интернет, серверы в России) ---------------------------------
Step "Проверяю туннель tuna.am"
$tuna = (Get-Command tuna -ErrorAction SilentlyContinue).Source
if (-not $tuna) { $tuna = Join-Path $Bin "tuna.exe" }
if (-not (Test-Path $tuna)) {
  Write-Host "Скачиваю tuna…"
  $tz = Join-Path $Root "tuna.zip"
  Invoke-WebRequest "https://releases.tuna.am/tuna/latest/tuna_windows_amd64.zip" -OutFile $tz -UseBasicParsing
  Expand-Archive $tz -DestinationPath $Bin -Force; Remove-Item $tz -Force
  $tuna = (Get-ChildItem $Bin -Recurse -Filter tuna.exe | Select-Object -First 1).FullName
  if (-not $tuna) { Fail "В архиве tuna нет tuna.exe" }
}
if (-not (Test-Path $SubFile)) {
  Write-Host ""
  Write-Host "Первый запуск. Нужен бесплатный аккаунт tuna.am:" -ForegroundColor Yellow
  Write-Host "  1) Зарегистрируйтесь на https://tuna.am и откройте личный кабинет."
  Write-Host "  2) Токен — на главной странице кабинета (строка tuna config save-token …), или в разделе «Токен»."
  Write-Host "  3) Постоянный адрес — в разделе «Домены», например snowy-orca-6775.a.tuna.free"
  Write-Host ""
  $tok = Read-Host "Вставьте токен tuna (правой кнопкой мыши) и нажмите Enter"
  if (-not $tok.Trim()) { Fail "Токен пустой." }
  & $tuna config save-token $tok.Trim() | Out-Null
  if ($LASTEXITCODE -ne 0) { Fail "tuna не приняла токен. Проверьте и запустите снова." }
  $sub = (Read-Host "Вставьте домен из раздела «Домены» (например snowy-orca-6775.a.tuna.free)").Trim().ToLower() -replace '^https?://', '' -replace '/.*$', ''
  if ($sub -notmatch '^[a-z0-9][a-z0-9.-]{2,80}$') { Fail "Домен — латиница, цифры, точки и дефис." }
  Set-Content $SubFile $sub -Encoding ASCII
}
$sub = (Get-Content $SubFile -Raw).Trim()

# --- 4. пользователи пилота ---------------------------------------------------------
$env:AUDIT_DB = Join-Path $Data "audit.db"
$env:AUDIT_PHOTOS = Join-Path $Data "photos"
$env:AUDIT_PILOT_STORES = $Stores
$env:AUDIT_PHOTO_DAYS = "120"
$env:AUDIT_INDEX = Join-Path $App "index.html"
$env:AUDIT_CORS = "https://skbmaksim82-byte.github.io"
Push-Location (Join-Path $App "server")
if (-not (Test-Path $PwFile)) {
  Step "Создаю входы для пилота"
  $out = & $vpy manage.py pilot $Stores
  if ($LASTEXITCODE -ne 0) { Pop-Location; Fail "Не создались пользователи: $out" }
  Set-Content $PwFile ($out -join "`r`n") -Encoding UTF8
  Write-Host "Пароли сохранены в $PwFile — раздайте их лично, в чатах не пересылайте." -ForegroundColor Yellow
  Start-Process notepad.exe $PwFile
}

# --- 5. сервер ----------------------------------------------------------------------
Step "Запускаю сервер"
$srv = Start-Process $vpy -ArgumentList "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "$Port", "--workers", "1", "--proxy-headers", "--app-dir", "`"$(Join-Path $App 'server')`"" `
  -WorkingDirectory $Root -WindowStyle Hidden -PassThru `
  -RedirectStandardOutput (Join-Path $Logs "server.log") -RedirectStandardError (Join-Path $Logs "server-err.log")
Pop-Location
Set-Content (Join-Path $Root "server.pid") $srv.Id
$ok = $false
for ($i = 0; $i -lt 30; $i++) {
  Start-Sleep -Milliseconds 700
  try { $h = Invoke-RestMethod "http://127.0.0.1:$Port/api/health" -TimeoutSec 2; if ($h.ok) { $ok = $true; break } } catch {}
}
if (-not $ok) { Fail "Сервер не запустился. Подробности: $Logs\server-err.log" }
Write-Host "Сервер работает, версия $($h.version), магазины: $($h.stores -join ', ')"

# --- 6. туннель ---------------------------------------------------------------------
Step "Открываю туннель $sub"
$tlog = Join-Path $Logs "tuna.log"; $telog = Join-Path $Logs "tuna-err.log"
$label = $sub.Split(".")[0]
# Флаг закреплённого адреса у tuna: -d для полного домена, -s для поддомена. Пробуем по очереди.
$variants = @(@("-d", $sub), @("-s", $label), @("-s", $sub))
function Start-Tuna($v) {
  Start-Process $tuna -ArgumentList (@("http", "$Port") + $v) -WindowStyle Hidden -PassThru -RedirectStandardOutput $tlog -RedirectStandardError $telog
}
function Test-Public($u) { try { $r = Invoke-RestMethod "$u/api/health" -Headers @{ "tuna-skip-browser-warning" = "1" } -TimeoutSec 5; return $r.app -eq "audit-sibir" } catch { return $false } }
$url = $null; $tunArgs = $null
foreach ($v in $variants) {
  $tun = Start-Tuna $v
  Set-Content (Join-Path $Root "tuna.pid") $tun.Id
  for ($i = 0; $i -lt 30 -and -not $url; $i++) {
    Start-Sleep -Seconds 1
    $txt = (Get-Content $tlog, $telog -Raw -ErrorAction SilentlyContinue) -join "`n"
    $m = [regex]::Match($txt, "https://$([regex]::Escape($label))[a-z0-9.-]*")   # наш адрес, а не ссылки на сайт tuna
    if ($m.Success -and (Test-Public $m.Value)) { $url = $m.Value }
    elseif ($i -ge 4 -and $i % 3 -eq 0 -and (Test-Public "https://$sub")) { $url = "https://$sub" }
    if ($tun.HasExited) { break }
  }
  if ($url) { $tunArgs = $v; break }
  try { Stop-Process -Id $tun.Id -Force -ErrorAction SilentlyContinue } catch {}
  Write-Host "Вариант «tuna http $Port $($v -join ' ')» не сработал, пробую следующий…" -ForegroundColor Yellow
}
if (-not $url) { Fail "Туннель не поднялся. Смотрите $telog и $tlog (часто — неверный токен или домен)." }
Write-Host "Туннель: $url"

$link = "$Pages`?api=$url"
Set-Clipboard $link
Write-Host ""
Write-Host "================================================================" -ForegroundColor Green
Write-Host " Пилот работает. Ссылка для участников (уже скопирована):" -ForegroundColor Green
Write-Host ""
Write-Host "   $link" -ForegroundColor White
Write-Host ""
Write-Host " Откройте её один раз на каждом телефоне: адрес сервера запомнится,"
Write-Host " дальше можно открывать обычную ссылку $Pages"
Write-Host " Пароли: $PwFile"
Write-Host " НЕ ЗАКРЫВАЙТЕ это окно и не выключайте компьютер, пока идёт тест."
Write-Host "================================================================" -ForegroundColor Green

# --- 7. следим, чтобы сервер и туннель не упали -------------------------------------
while ($true) {
  Start-Sleep -Seconds 10
  if ($srv.HasExited) { Write-Host "$(Get-Date -f HH:mm) сервер остановился, смотрите $Logs\server-err.log" -ForegroundColor Red; break }
  if ($tun.HasExited) {
    Write-Host "$(Get-Date -f HH:mm) туннель упал, перезапускаю…" -ForegroundColor Yellow
    $tun = Start-Tuna $tunArgs
    Set-Content (Join-Path $Root "tuna.pid") $tun.Id
  }
}
Read-Host "Нажмите Enter, чтобы закрыть"
