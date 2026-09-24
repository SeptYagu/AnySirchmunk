$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$EnvRoot = $env:AI_ENVS_ROOT
if ([string]::IsNullOrWhiteSpace($EnvRoot)) {
    if (Test-Path "D:\") {
        $EnvRoot = "D:\AIenvs"
    } else {
        $EnvRoot = Join-Path $env:LOCALAPPDATA "AIenvs"
    }
}

$EnvPath = Join-Path $EnvRoot "AnySirchmunk"
$PythonVersion = (Get-Content (Join-Path $RepoRoot ".python-version") -Raw).Trim()
$Python = Join-Path $EnvPath "Scripts\python.exe"

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv is required. Install uv, then rerun this script."
}

$NeedsCreate = -not (Test-Path $Python)
if (-not $NeedsCreate) {
    $CurrentVersion = (& $Python -c "import platform; print(platform.python_version())").Trim()
    $NeedsCreate = $CurrentVersion -ne $PythonVersion
}

if ($NeedsCreate) {
    if (Test-Path $EnvPath) { Remove-Item $EnvPath -Recurse -Force }
    New-Item -ItemType Directory -Force $EnvRoot | Out-Null
    uv venv $EnvPath --python $PythonVersion
}

Write-Host "AnySirchmunk environment ready: $EnvPath"
Write-Host "Python: $(& $Python --version)"
