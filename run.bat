@echo off
chcp 65001 >nul 2>&1
title Smart Fan Controller

:: Check venv exists
if not exist ".venv\Scripts\activate.bat" (
    echo [HIBA] Virtualis kornyezet nem talalhato!
    echo        Futtasd eloszor: setup_windows.bat
    pause
    exit /b 1
)

:: Activate venv and run
call .venv\Scripts\activate.bat
python swift_fan_controller_new_v7.py %*
pause
