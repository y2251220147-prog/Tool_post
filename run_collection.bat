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
title UCircle AI Automation - Phase 1: Collection
echo ============================================================
echo   UCircle AI Content Automation - Phase 1 Collection
echo   Thu thap RSS -> Loc Tier 1 -> Loai trung -> Luu Queue
echo   (KHONG tieu ton Gemini quota)
echo ============================================================
echo.

"%PYTHON_EXE%" collection_pipeline.py
echo.
pause
