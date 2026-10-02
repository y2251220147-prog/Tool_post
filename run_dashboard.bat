@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHON_EXE=python"
where python >nul 2>&1
if errorlevel 1 set "PYTHON_EXE=%LocalAppData%\Programs\Python\Python310\python.exe"
if not exist "%PYTHON_EXE%" if "%PYTHON_EXE%" NEQ "python" (
  echo Khong tim thay Python. Vui long cai Python 3.10 hoac them Python vao PATH.
  pause
  exit /b 1
)
title UCircle AI Automation - Review Dashboard
echo ============================================================
echo   UCircle AI Content Automation - Review Dashboard
echo   Mo trinh duyet tai: http://127.0.0.1:8000
echo ============================================================
echo.

start http://127.0.0.1:8000
"%PYTHON_EXE%" review_ui/app.py
pause
