param(
    [string]$ReleaseName = "stt_app-win-x64",
    [string]$InstallerName = "stt_app-win-x64-setup",
    [switch]$SkipBundleBuild,
    [string]$CompilerPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

if ([System.Environment]::OSVersion.Platform -ne [System.PlatformID]::Win32NT) {
    throw "This installer build script is intended to run on Windows."
}

function Get-ProjectVersion {
    $pyprojectPath = Join-Path $repoRoot "pyproject.toml"
    $pyprojectText = Get-Content -Raw -Path $pyprojectPath
    $match = [regex]::Match($pyprojectText, '(?m)^version\s*=\s*"([^"]+)"')
    if (-not $match.Success) {
        throw "Unable to determine project version from $pyprojectPath"
    }
    return $match.Groups[1].Value
}

function Resolve-InnoSetupCompiler {
    param(
        [string]$OverridePath
    )

    if ($OverridePath) {
        if (-not (Test-Path $OverridePath)) {
            throw "Specified Inno Setup compiler was not found: $OverridePath"
        }
        return (Resolve-Path $OverridePath).Path
    }

    $command = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    $candidates = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
        "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        "C:\Program Files\Inno Setup 6\ISCC.exe"
    )
    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) {
            return $candidate
        }
    }

    throw @"
Inno Setup compiler not found.

Install Inno Setup 6 and rerun this script, or pass -CompilerPath explicitly.
For example:
  choco install innosetup --no-progress -y
"@
}

$releaseRoot = Join-Path $repoRoot ("release\" + $ReleaseName)
if (-not $SkipBundleBuild) {
    Write-Host "==> Building Windows release bundle"
    & powershell.exe -ExecutionPolicy Bypass `
        -File (Join-Path $repoRoot "scripts\build_windows_release.ps1") `
        -ReleaseName $ReleaseName
    if ($LASTEXITCODE -ne 0) {
        throw "Release bundle build failed."
    }
}

$appExe = Join-Path $releaseRoot "stt_app.exe"
if (-not (Test-Path $appExe)) {
    throw "Expected release bundle not found: $appExe"
}

$compiler = Resolve-InnoSetupCompiler -OverridePath $CompilerPath
$version = Get-ProjectVersion
$scriptPath = Join-Path $repoRoot "installer\windows\stt_app.iss"
$outputDir = Join-Path $repoRoot "release\installer"
New-Item -ItemType Directory -Force -Path $outputDir | Out-Null

Write-Host "==> Compiling Inno Setup installer"
& $compiler `
    "/DMyAppVersion=$version" `
    "/DMyReleaseDir=$releaseRoot" `
    "/DMyOutputDir=$outputDir" `
    "/DMyOutputBaseFilename=$InstallerName" `
    $scriptPath
if ($LASTEXITCODE -ne 0) {
    throw "Inno Setup compiler failed."
}

$installerPath = Join-Path $outputDir ($InstallerName + ".exe")
if (-not (Test-Path $installerPath)) {
    throw "Expected installer output not found: $installerPath"
}

Write-Host ""
Write-Host "Installer build complete:"
Write-Host "  Path: $installerPath"
