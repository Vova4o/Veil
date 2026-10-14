<#
.SYNOPSIS
    Единый гейт качества mediastudio: ESLint, Node-тесты, Ruff, Pyrefly, Pydantic, Контракты OpenAPI, pytest.

.DESCRIPTION
    Прогоняет все проверки, которые раньше делались вручную по отдельности:
      1. Web: eslint (npm run lint — app/static/js).
      2. Web: node-тесты (npm test — tests/test_*.mjs).
      3. Python Lint: ruff check (app/, tests/).
      4. Python Type Check: pyrefly (если установлена системно или в venv).
      5. Pydantic & Контракты: sync_contracts.ps1 -CheckOnly (OpenAPI) и pydantic-валидация схем.
      6. Полный pytest (tests/ через poetry).

    Гейт только проверяет и ничего не правит.

    Совместим с Windows PowerShell 5.1 и PowerShell 7+.
    Нативные команды вызываются напрямую в теле скрипта (не в функциях):
    иначе их stdout попадет в возвращаемое значение функции и испортит
    код выхода. $LASTEXITCODE считывается сразу после вызова, без пайпов.

.EXAMPLE
    .\lint.ps1                    # полная проверка
    .\lint.ps1 -SkipTests         # только линтеры, node-тесты и контракты
    .\lint.ps1 -SkipWeb           # только Python-контур (ruff, pyrefly, pydantic/контракты + pytest)
#>

param(
    [switch]$SkipTests,
    [switch]$SkipWeb
)

$ErrorActionPreference = "Stop"

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

$ProjectRoot = $PSScriptRoot
if (-not $ProjectRoot) {
    $ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
}

$script:Results = @()

function Write-StepHeader($text) {
    Write-Host ""
    Write-Host ("=" * 60) -ForegroundColor Cyan
    Write-Host "  $text" -ForegroundColor Cyan
    Write-Host ("=" * 60) -ForegroundColor Cyan
}

function Register-Result($name, $status, $detail) {
    $script:Results += [PSCustomObject]@{
        Step   = $name
        Status = $status
        Detail = $detail
    }
    if ($status -eq "OK") {
        Write-Host "  [OK] $name" -ForegroundColor Green
    } elseif ($status -eq "SKIPPED") {
        Write-Host "  [SKIP] $name — $detail" -ForegroundColor Yellow
    } else {
        Write-Host "  [FAIL] $name — $detail" -ForegroundColor Red
    }
}

$hasNodeModules = Test-Path (Join-Path $ProjectRoot "node_modules")
$poetryCmd = Get-Command "poetry" -ErrorAction SilentlyContinue

# ── 1-2. Web: eslint + node-тесты ──────────────────────────────────
if ($SkipWeb) {
    Register-Result "eslint" "SKIPPED" "-SkipWeb"
    Register-Result "node tests" "SKIPPED" "-SkipWeb"
} elseif (-not $hasNodeModules) {
    Register-Result "eslint" "SKIPPED" "нет node_modules — выполнить npm ci"
    Register-Result "node tests" "SKIPPED" "нет node_modules — выполнить npm ci"
} else {
    Write-StepHeader "[1/6] ESLint (npm run lint)"
    Push-Location $ProjectRoot
    try {
        npm run lint
        $code = $LASTEXITCODE
        if ($null -eq $code) { $code = 0 }
    } finally {
        Pop-Location
    }
    if ($code -eq 0) {
        Register-Result "eslint" "OK" ""
    } else {
        Register-Result "eslint" "FAIL" "exit $code — чинить errors; warnings по мере чистки (см. eslint.config.js)"
    }

    Write-StepHeader "[2/6] Node-тесты (npm test)"
    Push-Location $ProjectRoot
    try {
        npm test
        $code = $LASTEXITCODE
        if ($null -eq $code) { $code = 0 }
    } finally {
        Pop-Location
    }
    if ($code -eq 0) {
        Register-Result "node tests" "OK" ""
    } else {
        Register-Result "node tests" "FAIL" "exit $code"
    }
}

# ── 3. Python Lint: Ruff ───────────────────────────────────────────
Write-StepHeader "[3/6] Python Lint (Ruff)"
if (-not $poetryCmd) {
    Register-Result "ruff" "SKIPPED" "poetry не найден в PATH"
} else {
    Push-Location $ProjectRoot
    try {
        poetry run ruff check app tests
        $code = $LASTEXITCODE
        if ($null -eq $code) { $code = 0 }
    } finally {
        Pop-Location
    }
    if ($code -eq 0) {
        Register-Result "ruff" "OK" ""
    } else {
        Register-Result "ruff" "FAIL" "exit $code — запустите 'poetry run ruff check app tests --fix' или исправьте ошибки ruff вручную"
    }
}

# ── 4. Python Type Check: Pyrefly ──────────────────────────────────
Write-StepHeader "[4/6] Python Type Check (Pyrefly)"
$pyreflyCmd = Get-Command "pyrefly" -ErrorAction SilentlyContinue
if (-not $pyreflyCmd -and $poetryCmd) {
    # Пробуем вызвать через poetry run pyrefly
    Push-Location $ProjectRoot
    try {
        poetry run pyrefly --version *>$null
        if ($LASTEXITCODE -eq 0) {
            $pyreflyCmd = "poetry run pyrefly"
        }
    } finally {
        Pop-Location
    }
}

if ($pyreflyCmd) {
    Push-Location $ProjectRoot
    try {
        if ($pyreflyCmd -is [string]) {
            poetry run pyrefly check app
        } else {
            pyrefly check app
        }
        $code = $LASTEXITCODE
        if ($null -eq $code) { $code = 0 }
    } finally {
        Pop-Location
    }
    if ($code -eq 0) {
        Register-Result "pyrefly" "OK" ""
    } else {
        Register-Result "pyrefly" "FAIL" "exit $code — найдена ошибка типов в pyrefly"
    }
} else {
    Register-Result "pyrefly" "SKIPPED" "pyrefly не установлен в системе/ poetry venv"
}

# ── 5. Pydantic & OpenAPI Контракты ────────────────────────────────
Write-StepHeader "[5/6] Pydantic и OpenAPI контракты"
if (-not $poetryCmd) {
    Register-Result "contracts & pydantic" "SKIPPED" "poetry не найден в PATH"
} else {
    Push-Location $ProjectRoot
    try {
        # Сначала прогоняем sync_contracts.ps1
        & (Join-Path $ProjectRoot "sync_contracts.ps1") -CheckOnly
        $contractCode = $LASTEXITCODE
        if ($null -eq $contractCode) { $contractCode = 0 }

        # Прогоняем тесты Pydantic и контрактов
        poetry run python -m pytest tests/test_openapi_contract.py tests/test_social_publish_contract.py -q
        $pydanticCode = $LASTEXITCODE
        if ($null -eq $pydanticCode) { $pydanticCode = 0 }
    } finally {
        Pop-Location
    }

    if ($contractCode -eq 0 -and $pydanticCode -eq 0) {
        Register-Result "contracts & pydantic" "OK" ""
    } else {
        Register-Result "contracts & pydantic" "FAIL" "ошибка проверки OpenAPI контрактов или Pydantic схем"
    }
}

# ── 6. Полный pytest ───────────────────────────────────────────────
if ($SkipTests) {
    Register-Result "pytest" "SKIPPED" "-SkipTests"
} elseif (-not $poetryCmd) {
    Register-Result "pytest" "SKIPPED" "poetry не найден в PATH — установить poetry и выполнить poetry install --no-root --with dev"
} else {
    Write-StepHeader "[6/6] Полный pytest (tests/)"
    Push-Location $ProjectRoot
    try {
        poetry run python -m pytest tests -q
        $code = $LASTEXITCODE
        if ($null -eq $code) { $code = 0 }
    } finally {
        Pop-Location
    }
    if ($code -eq 0) {
        Register-Result "pytest" "OK" ""
    } else {
        Register-Result "pytest" "FAIL" "exit $code"
    }
}

# ── Итог ───────────────────────────────────────────────────────────
Write-StepHeader "Итог проверки"
$script:Results | Format-Table -AutoSize | Out-Host
$failed = @($script:Results | Where-Object { $_.Status -eq "FAIL" }).Count
if ($failed -gt 0) {
    Write-Host "  Гейт НЕ пройден: FAIL шагов: $failed." -ForegroundColor Red
    exit 1
}
Write-Host "  Гейт пройден: все проверки OK (SKIP — только явно пропущенные)." -ForegroundColor Green
exit 0

