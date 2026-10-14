@echo off
title VEIL
cd /d "%~dp0"
if errorlevel 1 (
    echo [ERROR] Failed to open the application directory.
    pause
    exit /b 1
)

if /I "%~1"=="--web" goto web
if /I "%~1"=="--reset-index" goto web
if /I "%~1"=="--factory-reset" goto web

set "MEDIASTUDIO_IMPORT_DIR=%~dp0.mediastudio_data"
if exist "%~dp0dist\desktop\win-unpacked\VEIL.exe" (
    start "" "%~dp0dist\desktop\win-unpacked\VEIL.exe" %*
    exit /b 0
)

call "%~dp0desktop.bat" %*
exit /b %errorlevel%

:web
if not defined MEDIASTUDIO_DATA_DIR if exist "%APPDATA%\MediaStudio\data\meta.db" (
    set "MEDIASTUDIO_DATA_DIR=%APPDATA%\MediaStudio\data"
    set "MEDIASTUDIO_CACHE_DIR=%APPDATA%\MediaStudio\cache"
)
rem A profile from before the codename is still read until the desktop app has moved it.
if not defined MEDIASTUDIO_DATA_DIR if exist "%APPDATA%\Comfy Meta Viewer\data\meta.db" (
    set "MEDIASTUDIO_DATA_DIR=%APPDATA%\Comfy Meta Viewer\data"
    set "MEDIASTUDIO_CACHE_DIR=%APPDATA%\Comfy Meta Viewer\cache"
)
where poetry >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Poetry not found. Install: pip install poetry
    pause
    exit /b 1
)

echo Installing dependencies...
call poetry install --no-root --quiet
if errorlevel 1 (
    echo [ERROR] Failed to install dependencies. Run: poetry install --no-root
    pause
    exit /b 1
)

echo Starting VEIL...
call poetry run python -m app.main %*
if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Failed to start. Try: poetry install
    pause
)
