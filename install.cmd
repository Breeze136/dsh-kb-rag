@echo off
rem dsh-kb-rag — one-click installer entry (double-click or run from cmd)
rem 完整参数见 npm-package\scripts\install.ps1，例：install.cmd -Profile myprofile -Models
setlocal
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0npm-package\scripts\install.ps1" %*
echo.
pause
endlocal
