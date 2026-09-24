$ErrorActionPreference = "Stop"

$EnvRoot = $env:AI_ENVS_ROOT
if ([string]::IsNullOrWhiteSpace($EnvRoot)) {
    if (Test-Path "D:\") {
        $EnvRoot = "D:\AIenvs"
    } else {
        $EnvRoot = Join-Path $env:LOCALAPPDATA "AIenvs"
    }
}

$Python = Join-Path (Join-Path $EnvRoot "AnySirchmunk") "Scripts\python.exe"
if (-not (Test-Path $Python)) {
    throw "AnySirchmunk environment not found. Run .\scripts\setup_dev.ps1 first."
}

& $Python @args
exit $LASTEXITCODE
