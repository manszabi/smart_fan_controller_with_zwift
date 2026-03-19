@echo off
chcp 65001 >nul 2>&1
title Smart Fan Controller - Windows Setup

echo ========================================
echo  Smart Fan Controller - Windows Setup
echo ========================================
echo.

:: Check Python
where python >nul 2>&1
if errorlevel 1 (
    echo [HIBA] Python nem talalhato! Telepitsd a Python 3.10+-t:
    echo        https://www.python.org/downloads/
    echo        FONTOS: Jelold be az "Add Python to PATH" opciót!
    pause
    exit /b 1
)

:: Check Python version
for /f "tokens=2 delims= " %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo [OK] Python verzio: %PYVER%

:: Create virtual environment
if not exist ".venv" (
    echo.
    echo Virtualis kornyezet letrehozasa...
    python -m venv .venv
    if errorlevel 1 (
        echo [HIBA] Virtualis kornyezet letrehozasa sikertelen!
        pause
        exit /b 1
    )
    echo [OK] .venv letrehozva
) else (
    echo [OK] .venv mar letezik
)

:: Activate venv and install dependencies
echo.
echo Fuggosegek telepitese...
call .venv\Scripts\activate.bat

python -m pip install --upgrade pip >nul 2>&1
pip install -r requirements.txt
if errorlevel 1 (
    echo [HIBA] Fuggosegek telepitese sikertelen!
    pause
    exit /b 1
)
echo [OK] Fuggosegek telepitve

echo.
echo PyInstaller telepitese (exe buildhez)...
pip install pyinstaller >nul 2>&1
if errorlevel 1 (
    echo [FIGYELEM] PyInstaller telepitese sikertelen - exe build nem lesz elerheto
) else (
    echo [OK] PyInstaller telepitve
)

:: Check for settings.json
if not exist "settings.json" (
    echo.
    echo [INFO] settings.json nem talalhato.
    echo        Masolom a settings.example.json-t...
    copy settings.example.json settings.json >nul 2>&1
    if exist "settings.json" (
        echo [OK] settings.json letrehozva - szerkeszd a sajat beallitasaiddal!
        echo      Zwift UDP forrashoz allitsd be a zwiftudp_sources mezot:
        echo        "zwift_api_polling"  - Zwift HTTPS API (bejelentkezes szukseges)
        echo        "zwift_udp_monitor"  - Zwift Companion App UDP (automatikus
        echo                              fallback zwift_api_polling-ra, ha nem
        echo                              erkezik adat zwiftudp_sources_timeout mp-ig)
    ) else (
        echo [FIGYELEM] Nem sikerult masolni. Hozd letre manuálisan a settings.json-t!
    )
) else (
    echo [OK] settings.json mar letezik
)

echo.
echo ========================================
echo  Telepites kesz!
echo ========================================
echo.
echo Inditas:        run.bat
echo EXE buildelese: build_exe.bat
echo.
pause
