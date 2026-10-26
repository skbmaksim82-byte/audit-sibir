@echo off
rem Audit-sibir pilot: server + tuna tunnel. Keep this window open during the test.
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-pilot.ps1"
