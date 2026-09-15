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
    $expectedCommit = "3c7ee54f93fa198db2020a3ab850356f2dacff72"
    $patchFile = Join-Path $repoRoot "patches\sirchmunk-3c7ee54-anytxt.patch"
    $inside = git -C $target rev-parse --is-inside-work-tree 2>$null
    if ($LASTEXITCODE -ne 0 -or $inside -ne "true") {
        throw "Target is not a Git working tree: $target"
    }
    $actualCommit = git -C $target rev-parse HEAD
    if ($actualCommit -ne $expectedCommit) {
        throw "Sirchmunk HEAD must be $expectedCommit; found $actualCommit"
    }
    git -C $target apply --reverse --check --whitespace=error-all $patchFile
    if ($LASTEXITCODE -ne 0) {
        throw "Target does not contain the current AnySirchmunk patch or patched files have drifted."
    }
    python -m py_compile `
        (Join-Path $target "src\sirchmunk\retrieve\anytxt_retriever.py") `
        (Join-Path $target "src\sirchmunk\agentic\tools.py") `
        (Join-Path $target "src\sirchmunk\cli\cli.py") `
        (Join-Path $target "src\sirchmunk\search.py")
    if ($LASTEXITCODE -ne 0) { throw "Patched Sirchmunk compilation failed" }
}

Write-Output "AnySirchmunk verification passed"
