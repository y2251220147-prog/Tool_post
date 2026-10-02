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
title UCircle AI Automation - Scheduler (2 Jobs)
echo ============================================================
echo   UCircle AI Content Automation - Scheduler
echo   - Job 1: RSS Collection moi 20 phut (0 AI calls)
echo   - Job 2: Publish Schedule (Moi 45 phut, 6h-23h)
echo ============================================================
echo.

"%PYTHON_EXE%" scheduler.py
pause
