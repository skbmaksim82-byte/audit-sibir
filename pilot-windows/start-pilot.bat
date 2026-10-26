@echo off
rem Audit-sibir pilot: server + tuna tunnel. Keep this window open during the test.
rem Always takes the latest start-pilot.ps1 from GitHub; falls back to the copy next to this file.
chcp 65001 >nul
set "PILOT_LOCAL=%~dp0start-pilot.ps1"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$d=Join-Path $env:USERPROFILE 'audit-sibir-pilot'; New-Item -ItemType Directory -Force $d | Out-Null; $p=Join-Path $d 'start-pilot.ps1'; $ProgressPreference='SilentlyContinue'; try { Invoke-WebRequest 'https://raw.githubusercontent.com/skbmaksim82-byte/audit-sibir/main/pilot-windows/start-pilot.ps1' -OutFile $p -UseBasicParsing -TimeoutSec 20 } catch { Copy-Item $env:PILOT_LOCAL $p -Force }; & $p"
pause
