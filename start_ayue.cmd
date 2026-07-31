@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_ayue.ps1" %*
exit /b %ERRORLEVEL%
