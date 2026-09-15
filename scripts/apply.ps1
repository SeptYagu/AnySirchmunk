param(
    [Parameter(Mandatory = $true)]
    [string]$SirchmunkPath
)

$ErrorActionPreference = "Stop"
$expectedCommit = "3c7ee54f93fa198db2020a3ab850356f2dacff72"
$target = (Resolve-Path -LiteralPath $SirchmunkPath).Path
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$patchFile = Join-Path $repoRoot "patches\sirchmunk-3c7ee54-anytxt.patch"

$inside = git -C $target rev-parse --is-inside-work-tree 2>$null
if ($LASTEXITCODE -ne 0 -or $inside -ne "true") {
    throw "Target is not a Git working tree: $target"
}
$actualCommit = git -C $target rev-parse HEAD
if ($actualCommit -ne $expectedCommit) {
    throw "Sirchmunk HEAD must be $expectedCommit; found $actualCommit"
}
if (-not (Test-Path -LiteralPath $patchFile -PathType Leaf)) {
    throw "Patch file is missing: $patchFile"
}

git -C $target apply --check --whitespace=error-all $patchFile
if ($LASTEXITCODE -ne 0) {
    throw "Patch preflight failed. The target may already be patched or modified."
}
git -C $target apply --whitespace=error-all $patchFile
if ($LASTEXITCODE -ne 0) {
    throw "Patch application failed."
}

Write-Output "AnySirchmunk patch applied to $target"
Write-Output "Set SIRCHMUNK_SEARCH_BACKEND=anytxt to opt in."
