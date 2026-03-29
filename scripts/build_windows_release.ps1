Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

param(
    [string]$ReleaseName = "stt_app-win-x64",
    [switch]$SkipZip
)

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

if (-not $IsWindows) {
    throw "This build script is intended to run on Windows."
}

Write-Host "==> Syncing project environment"
uv sync --group dev

Write-Host "==> Cleaning old build outputs"
Remove-Item -Recurse -Force build, dist, release -ErrorAction SilentlyContinue

Write-Host "==> Building PyInstaller bundle"
uv run pyinstaller --noconfirm --clean stt_app.spec

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
