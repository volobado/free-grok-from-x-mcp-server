@echo off
rem GrokX setup: dependencies, grokx commands in PATH, self-check.
where pwsh >nul 2>nul
if %errorlevel%==0 (
  pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
)
pause
