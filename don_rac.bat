@echo off
chcp 65001 >nul
title Don Rac Bot - C:\bot_san_code

cd /d "C:\bot_san_code"

echo.
echo ╔══════════════════════════════════════════════╗
echo ║         DON RAC BOT - bot_san_code           ║
echo ║  Giu lai: *.py / .env / *.bat / *.session   ║
echo ╚══════════════════════════════════════════════╝
echo.
echo Se xoa:
echo   [1] logs\
echo   [2] data\
echo   [3] backups\
echo   [4] __pycache__\
echo   [5] HoSo_Bot_Vip\ (Chrome profile tam)
echo   [6] coccoc_cdp_profile\ (CocCoc profile tam)
echo   [7] Temp OCR trong %%TEMP%%
echo.
set /p confirm="Nhap YES de xac nhan xoa: "
if /i not "%confirm%"=="YES" (
    echo.
    echo Huy. Khong co gi bi xoa.
    timeout /t 2 >nul
    exit /b
)

echo.

echo [1/7] Xoa logs\...
if exist "logs" ( rd /s /q "logs" && echo      OK ) else ( echo      Khong co )

echo [2/7] Xoa data\...
if exist "data" ( rd /s /q "data" && echo      OK ) else ( echo      Khong co )

echo [3/7] Xoa backups\...
if exist "backups" ( rd /s /q "backups" && echo      OK ) else ( echo      Khong co )

echo [4/7] Xoa __pycache__\...
if exist "__pycache__" ( rd /s /q "__pycache__" && echo      OK ) else ( echo      Khong co )
del /f /q "*.pyc" 2>nul

echo [5/7] Xoa HoSo_Bot_Vip\...
if exist "HoSo_Bot_Vip" ( rd /s /q "HoSo_Bot_Vip" && echo      OK ) else ( echo      Khong co )

echo [6/7] Xoa coccoc_cdp_profile\...
if exist "coccoc_cdp_profile" ( rd /s /q "coccoc_cdp_profile" && echo      OK ) else ( echo      Khong co )

echo [7/7] Xoa Temp OCR trong %%TEMP%%...
del /f /q "%TEMP%\ocr_*" 2>nul
for /d %%d in ("%TEMP%\ocr_*") do rd /s /q "%%d" 2>nul
echo      OK

echo.
echo Tao lai thu muc can thiet...
mkdir "logs" 2>nul
mkdir "logs\screenshots" 2>nul
mkdir "logs\code_history" 2>nul
mkdir "data" 2>nul
echo    logs\ va data\ da duoc tao lai

echo.
echo ╔══════════════════════════════════════════════╗
echo ║   XONG! Bot san sang chay lai tu dau         ║
echo ╚══════════════════════════════════════════════╝
echo.
pause