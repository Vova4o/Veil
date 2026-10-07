@echo off
title ComfyUI Meta Viewer
cd /d "%~dp0"
if errorlevel 1 (
    echo [ERROR] Failed to open the application directory.
    pause
    exit /b 1
)

if /I "%~1"=="--web" goto web
if /I "%~1"=="--reset-index" goto web
if /I "%~1"=="--factory-reset" goto web

set "COMFY_META_IMPORT_DIR=%~dp0.comfy_meta_uploads"
if exist "%~dp0dist\desktop\win-unpacked\Comfy Meta Viewer.exe" (
    start "" "%~dp0dist\desktop\win-unpacked\Comfy Meta Viewer.exe" %*
    exit /b 0
)

where npm >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Node.js is needed for source development. Use the desktop installer instead.
    pause
    exit /b 1
)
if not exist "node_modules\electron\package.json" (
    call npm ci
    if errorlevel 1 exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
    set "POETRY_VIRTUALENVS_IN_PROJECT=true"
    call poetry install --no-root --quiet
    if errorlevel 1 exit /b 1
)
call npm run desktop -- %*
exit /b %errorlevel%

:web
if not defined COMFY_META_DATA_DIR if exist "%APPDATA%\Comfy Meta Viewer\data\meta.db" (
    set "COMFY_META_DATA_DIR=%APPDATA%\Comfy Meta Viewer\data"
    set "COMFY_META_CACHE_DIR=%APPDATA%\Comfy Meta Viewer\cache"
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

echo Starting ComfyUI Meta Viewer...
call poetry run python -m app.main %*
if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Failed to start. Try: poetry install
    pause
)
