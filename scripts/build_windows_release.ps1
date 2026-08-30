param(
    [string]$ReleaseName = "stt_app-win-x64",
    [switch]$SkipZip
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

if ([System.Environment]::OSVersion.Platform -ne [System.PlatformID]::Win32NT) {
    throw "This build script is intended to run on Windows."
}

# $ErrorActionPreference governs cmdlet errors only. A native command that
# fails just sets $LASTEXITCODE: Windows PowerShell 5.1 -- which is what
# `powershell -File` in windows-release.yml runs -- has no
# $PSNativeCommandUseErrorActionPreference at all, and on pwsh 7.6.5 it
# defaults to $false. Measured with `cmd /c "exit 7"`: the script ran on and
# the step exited 0. Unchecked, a failed `npm ci` (which deletes node_modules
# before installing) left an emptied tree, the spec below silently dropped the
# Node runtime from the bundle, and the release published green with Cohere
# and Granite Speech broken at runtime.
function Assert-LastExitCode {
    param([string]$Step)
    if ($LASTEXITCODE -ne 0) {
        throw "$Step failed with exit code $LASTEXITCODE."
    }
}

Write-Host "==> Syncing project environment"
uv sync --group dev --locked
Assert-LastExitCode "uv sync"

Write-Host "==> Installing locked JavaScript runtime dependencies"
npm ci --omit=dev
Assert-LastExitCode "npm ci"

Write-Host "==> Cleaning old build outputs"
# Not -ErrorAction SilentlyContinue: a delete refused because a previous
# stt_app.exe or an AV scan still holds a file inside dist\stt_app leaves the
# directory in place, which then satisfies the Test-Path check below and lets
# the previous build's files travel into the zip and the installer. Measured
# in a scratch tree with one file held open: the directory survived and the
# script did not notice.
foreach ($stale in @("build", "dist", "release")) {
    if (Test-Path $stale) {
        Remove-Item -Recurse -Force $stale -ErrorAction Stop
    }
}

Write-Host "==> Building PyInstaller bundle"
uv run pyinstaller --noconfirm --clean stt_app.spec
Assert-LastExitCode "pyinstaller"

$distRoot = Join-Path $repoRoot "dist\stt_app"
if (-not (Test-Path $distRoot)) {
    throw "Expected PyInstaller output folder not found: $distRoot"
}

$releaseRoot = Join-Path $repoRoot ("release\" + $ReleaseName)
New-Item -ItemType Directory -Force -Path $releaseRoot | Out-Null
Copy-Item -Recurse -Force (Join-Path $distRoot "*") $releaseRoot

$notes = @"
stt_app Windows release bundle

- Run stt_app.exe
- No terminal is required for normal use
- Models are downloaded on first use unless you pre-seed the cache
- Code-sign the executable before broad distribution
"@
$notes | Set-Content -Encoding UTF8 (Join-Path $releaseRoot "README.txt")

if (-not $SkipZip) {
    $zipPath = Join-Path $repoRoot ("release\" + $ReleaseName + ".zip")
    if (Test-Path $zipPath) {
        Remove-Item -Force $zipPath
    }
    Write-Host "==> Creating zip archive"
    Compress-Archive -Path (Join-Path $releaseRoot "*") -DestinationPath $zipPath
}

Write-Host ""
Write-Host "Build complete:"
Write-Host "  Folder: $releaseRoot"
if (-not $SkipZip) {
    Write-Host "  Zip:    $zipPath"
}
