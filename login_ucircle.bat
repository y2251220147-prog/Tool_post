@echo off
setlocal
chcp 65001 >nul

set "PROJECT_DIR=%~dp0"
set "LOGIN_SCRIPT=%~dp0scripts\login_ucircle.py"

if not exist "%LOGIN_SCRIPT%" (
  echo Khong tim thay chuong trinh dang nhap:
  echo   %LOGIN_SCRIPT%
  echo Hay dam bao login_ucircle.bat nam trong thu muc goc cua du an.
  echo.
  pause
  exit /b 1
)

set "PYTHON_EXE=%LocalAppData%\Programs\Python\Python310\python.exe"
if not exist "%PYTHON_EXE%" (
  where python >nul 2>&1
  if errorlevel 1 (
    set "PYTHON_EXE="
  ) else (
    set "PYTHON_EXE=python"
  )
)

if not defined PYTHON_EXE (
  echo Khong tim thay Python 3.10 tren may.
  echo Vui long cai Python hoac them Python vao PATH.
  echo.
  pause
  exit /b 1
)

title Dang Nhap UCircle - Luu Session Gmail OTP
echo ============================================================
echo   DANG NHAP UCIRCLE - LUU PHIEN SESSION.JSON
echo   Trinh duyet se giu mo de ban dang nhap Gmail va ma OTP.
echo   Chi nhan ENTER khi ban da vao duoc trang UCircle.
echo ============================================================
echo.

"%PYTHON_EXE%" -u "%LOGIN_SCRIPT%"
set "LOGIN_EXIT_CODE=%ERRORLEVEL%"

echo.
if not "%LOGIN_EXIT_CODE%"=="0" (
  echo ============================================================
  echo   DANG NHAP CHUA HOAN TAT - MA LOI: %LOGIN_EXIT_CODE%
  echo   Vui long doc thong bao loi ben tren.
  echo ============================================================
) else (
  echo ============================================================
  echo   DA LUU PHIEN DANG NHAP THANH CONG
  echo ============================================================
)
echo.
echo Nhan phim bat ky de dong cua so nay...
pause
exit /b %LOGIN_EXIT_CODE%
