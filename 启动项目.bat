@echo off
setlocal EnableExtensions
cd /d "%~dp0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\launcher.ps1" %*
set "exitCode=%ERRORLEVEL%"
if "%exitCode%"=="0" exit /b 0
echo [ERROR] Application startup failed with exit code %exitCode%.
pause
exit /b %exitCode%
