@echo off
chcp 65001 >nul 2>&1
rem Cryo-floods launcher - self-contained runtime, any drive letter.
rem Delegates to run.ps1 so all messages stay in UTF-8.
cd /d "%~dp0"
powershell -ExecutionPolicy Bypass -NoProfile -File "%~dp0run.ps1" %*
if errorlevel 1 (
    echo.
    echo Startup failed. Run diagnose.ps1 for details.
    pause
)
