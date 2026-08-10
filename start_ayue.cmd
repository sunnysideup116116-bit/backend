@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_ayue.ps1" %*
set "AYUE_EXIT_CODE=%ERRORLEVEL%"
if not "%AYUE_EXIT_CODE%"=="0" (
    echo.
    echo Ayue failed to start. Review the error above.
    if "%~1"=="" pause
)
exit /b %AYUE_EXIT_CODE%
