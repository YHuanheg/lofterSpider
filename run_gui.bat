@echo off
rem Start lofterSpider GUI. ASCII-only on purpose (Windows batch + encoding).
cd /d "%~dp0"

set "PYEXE="
if exist ".venv\Scripts\python.exe" set "PYEXE=.venv\Scripts\python.exe"
if defined PYEXE goto run

set "PYEXE=python"
where python >nul 2>nul || set "PYEXE="
if defined PYEXE goto run

rem Note: py -3.12 may not resolve if Python 3.12 was installed by uv.
set "PYEXE=py"
where py >nul 2>nul || set "PYEXE="
if defined PYEXE goto run

echo [ERROR] Python not found.
echo Install Python 3.12 from https://www.python.org/downloads/
echo Remember to check "Add python.exe to PATH" during setup.
pause
exit /b 3

:run
echo Using Python: %PYEXE%
"%PYEXE%" main.py
echo.
echo Exit code: %errorlevel%
pause
