@echo off
setlocal
cd /d "%~dp0"
set "UV=%USERPROFILE%\.local\bin\uv.exe"
if not exist "%UV%" set "UV=uv"
"%UV%" run --locked --project "%~dp0app" python "%~dp0scripts\control.py" login %*
set "RESULT=%ERRORLEVEL%"
echo.
pause
exit /b %RESULT%