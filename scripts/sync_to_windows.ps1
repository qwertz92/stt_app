<#
.SYNOPSIS
    Sync the tts_app project to a Windows-native directory.

.DESCRIPTION
    Copies the tts_app repository to a Windows-native directory so the app
    can be run natively on Windows (e.g. when developing under WSL).

    The source directory is automatically resolved from the script's location
    (the repo root). The target defaults to %USERPROFILE%\programs\tts_app.

    This is necessary because:
    - The app uses Win32 APIs (hotkeys, SendInput, clipboard) that only
      work in native Windows.
    - Model import scripts must write to the Windows HuggingFace cache
      (C:\Users\<user>\.cache\huggingface\hub\), not the WSL Linux cache
      (~/.cache/huggingface/hub/).

    The script uses robocopy for efficient incremental sync (only copies
    changed files).

.EXAMPLE
    .\scripts\sync_to_windows.ps1

    # Custom target directory:
    .\scripts\sync_to_windows.ps1 -TargetDir "D:\my-tts-app"

    # Dry run (show what would be copied):
    .\scripts\sync_to_windows.ps1 -DryRun
#>

[CmdletBinding()]
param(
    [string]$TargetDir = "$env:USERPROFILE\programs\tts_app",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

# Resolve source directory: the repo root (parent of scripts/).
# Works regardless of where the script is called from.
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$SourceDir = (Resolve-Path (Join-Path $ScriptDir "..")).Path

# Directories and files to exclude from sync (not needed on Windows).
$ExcludeDirs = @(
    ".git"
    "__pycache__"
    ".pytest_cache"
    ".ruff_cache"
    ".mypy_cache"
    "*.egg-info"
    ".venv"
    ".uv"
)

$ExcludeFiles = @(
    "*.pyc"
    ".gitignore"
    "uv.lock"
)

# --- Validation ---

if (-not (Test-Path $SourceDir)) {
    Write-Error "Source directory not found: $SourceDir"
    exit 1
}

# --- Sync ---

Write-Host ""
Write-Host "=== tts_app WSL -> Windows Sync ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Source:  $SourceDir"
Write-Host "  Target:  $TargetDir"
Write-Host ""

if ($DryRun) {
    Write-Host "  [DRY RUN] Showing what would be copied..." -ForegroundColor Yellow
    Write-Host ""
}

# Build robocopy arguments.
$roboArgs = @(
    $SourceDir
    $TargetDir
    "/MIR"          # Mirror: sync deletions too
    "/XD"           # Exclude directories (followed by list)
) + $ExcludeDirs + @(
    "/XF"           # Exclude files (followed by list)
) + $ExcludeFiles + @(
    "/NFL"          # No file list (less verbose)
    "/NDL"          # No directory list
    "/NJH"          # No job header
    "/NJS"          # No job summary
    "/NC"           # No file class
    "/NS"           # No file size
    "/NP"           # No progress percentage
    "/R:1"          # Retry once on failure
    "/W:1"          # Wait 1 second between retries
)

if ($DryRun) {
    $roboArgs += "/L"  # List only, don't actually copy
    # Show more detail in dry run
    $roboArgs = $roboArgs | Where-Object { $_ -notin @("/NFL", "/NDL", "/NC", "/NS") }
}

$startTime = Get-Date

robocopy @roboArgs

# Robocopy exit codes: 0 = nothing copied, 1 = files copied, 2 = extra files
# removed (via /MIR). Values 0-7 are success. 8+ are errors.
$exitCode = $LASTEXITCODE

if ($exitCode -ge 8) {
    Write-Host ""
    Write-Error "Robocopy failed with exit code $exitCode"
    exit 1
}

$elapsed = (Get-Date) - $startTime

Write-Host ""
if ($DryRun) {
    Write-Host "  [DRY RUN] No files were actually copied." -ForegroundColor Yellow
} else {
    Write-Host "  Sync complete! ($([math]::Round($elapsed.TotalSeconds, 1))s)" -ForegroundColor Green
}
Write-Host ""

# --- Post-sync reminder ---

Write-Host "  Next steps on Windows:" -ForegroundColor Cyan
Write-Host "    cd $TargetDir"
Write-Host "    pip install -e . --no-deps   # if first time or deps changed"
Write-Host "    python -m tts_app            # run the app"
Write-Host ""
Write-Host "  To import a model on Windows:" -ForegroundColor Cyan
Write-Host "    python scripts\import_model.py <path-to-model-folder>"
Write-Host "    (imports into Windows HF cache: ~\.cache\huggingface\hub\)"
Write-Host ""
