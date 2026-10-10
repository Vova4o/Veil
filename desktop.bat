@echo off
setlocal
title Comfy Meta Viewer Desktop
cd /d "%~dp0"
if errorlevel 1 (
    echo [ERROR] Failed to open the application directory.
    goto fail
)

if /I "%~1"=="--packaged" goto packaged

where npm >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Node.js not found. Install Node.js or use the desktop installer.
    goto fail
)
if not exist "node_modules\electron\package.json" (
    echo Installing Node.js dependencies...
    call npm ci || goto fail
)
if not defined COMFY_META_PYTHON if not exist ".venv\Scripts\python.exe" (
    where poetry >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] Python environment not found. Install Poetry: pip install poetry
        goto fail
    )
    echo Creating the project Python environment...
    set "POETRY_VIRTUALENVS_IN_PROJECT=true"
    call poetry install --no-root --quiet || goto fail
)

echo Starting Comfy Meta Viewer desktop from source...
call npm run desktop -- %* || goto fail
exit /b 0

:packaged
set "DESKTOP_EXE=%~dp0dist\desktop\win-unpacked\Comfy Meta Viewer.exe"
if not exist "%DESKTOP_EXE%" (
    echo [ERROR] Packaged app not found. Build it with: npm run desktop:package
    goto fail
)
if not defined COMFY_META_IMPORT_DIR set "COMFY_META_IMPORT_DIR=%~dp0.comfy_meta_uploads"
start "" "%DESKTOP_EXE%"
exit /b 0

:fail
echo.
pause
exit /b 1
