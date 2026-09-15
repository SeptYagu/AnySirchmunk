param(
    [string]$SirchmunkPath
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
Push-Location $repoRoot
try {
    python -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) { throw "Contract tests failed" }
    python -m py_compile "src\sirchmunk\retrieve\anytxt_retriever.py"
    if ($LASTEXITCODE -ne 0) { throw "Adapter compilation failed" }
} finally {
    Pop-Location
}

if ($SirchmunkPath) {
    $target = (Resolve-Path -LiteralPath $SirchmunkPath).Path
    python -m py_compile `
        (Join-Path $target "src\sirchmunk\retrieve\anytxt_retriever.py") `
        (Join-Path $target "src\sirchmunk\agentic\tools.py") `
        (Join-Path $target "src\sirchmunk\cli\cli.py") `
        (Join-Path $target "src\sirchmunk\search.py")
    if ($LASTEXITCODE -ne 0) { throw "Patched Sirchmunk compilation failed" }
}

Write-Output "AnySirchmunk verification passed"
