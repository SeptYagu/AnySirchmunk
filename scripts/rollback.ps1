param(
    [Parameter(Mandatory = $true)]
    [string]$SirchmunkPath
)

$ErrorActionPreference = "Stop"
$target = (Resolve-Path -LiteralPath $SirchmunkPath).Path
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$patchFile = Join-Path $repoRoot "patches\sirchmunk-3c7ee54-anytxt.patch"

git -C $target apply --reverse --check --whitespace=error-all $patchFile
if ($LASTEXITCODE -ne 0) {
    throw "Rollback preflight failed. The patch is absent or patched files changed."
}
git -C $target apply --reverse --whitespace=error-all $patchFile
if ($LASTEXITCODE -ne 0) {
    throw "Rollback failed."
}
Write-Output "AnySirchmunk patch removed from $target"
