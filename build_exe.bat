@echo off
chcp 65001 >nul 2>&1
title Smart Fan Controller - Build EXE

echo ========================================
echo  Smart Fan Controller - Build EXE
echo ========================================
echo.

:: Check venv
if not exist ".venv\Scripts\activate.bat" (
    echo [HIBA] Virtualis kornyezet nem talalhato!
    echo        Futtasd eloszor: setup_windows.bat
    pause
    exit /b 1
)

call .venv\Scripts\activate.bat

:: Install PyInstaller if needed
pip show pyinstaller >nul 2>&1
if errorlevel 1 (
    echo PyInstaller telepitese...
    pip install pyinstaller
    echo.
)

echo Build inditas...
echo.
pyinstaller smart_fan_controller.spec --noconfirm

if errorlevel 1 (
    echo.
    echo [HIBA] Build sikertelen!
    pause
    exit /b 1
)

:: Copy settings files to dist
echo.
echo Settings fajlok masolasa...
if not exist "dist\SmartFanController\settings.json" (
    if exist "settings.json" (
        copy settings.json "dist\SmartFanController\settings.json" >nul
        echo [OK] settings.json masolva
    ) else (
        copy settings.example.json "dist\SmartFanController\settings.json" >nul
        echo [OK] settings.example.json masolva mint settings.json
    )
)
if exist "zwift_api_settings.json" (
    copy zwift_api_settings.json "dist\SmartFanController\zwift_api_settings.json" >nul
    echo [OK] zwift_api_settings.json masolva
)

echo.
echo ========================================
echo  Build kesz!
echo ========================================
echo.
echo Az exe itt talalhato:
echo   dist\SmartFanController\SmartFanController.exe
echo.
echo A teljes dist\SmartFanController mappat masold oda,
echo ahol hasznalni szeretned. A settings.json-t szerkeszd
echo a sajat beallitasaiddal.
echo.
pause
