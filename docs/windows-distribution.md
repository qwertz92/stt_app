# Windows Distribution Strategy

This document describes the recommended way to ship `stt_app` to normal
Windows end users.

## Recommendation

**Recommended path:** publish a signed **PyInstaller `onedir` bundle** on
GitHub Releases, then add an installer or `winget` later if needed.

This is the best short-to-medium-term option for the current app because:

- users do **not** need to clone the repo,
- users do **not** need to install Python or `uv`,
- the app can run as a normal background tray application without an open
  terminal,
- `onedir` is more reliable than `onefile` for a PySide6 + native-audio +
  speech-runtime app with multiple compiled dependencies.

## Why not make end users install from the repo?

The repo + `uv` flow is appropriate for developers, not end users.

Problems for normal users:

- Python must already be installed,
- `uv` is an extra tool to explain,
- command-line startup is intimidating,
- versioned upgrades are unclear,
- corporate endpoints often block ad-hoc developer tooling.

## Why `onedir` first?

The project already includes a PyInstaller spec, and `onedir` is the safer
delivery format for this app.

Compared with `onefile`:

- startup is faster,
- fewer extraction-time surprises,
- native libraries are easier to debug,
- antivirus/EDR false positives are usually less painful.

For this app, the extra folder is an acceptable tradeoff.

## Recommended rollout

### Phase 1: GitHub Releases

Ship:

- `stt_app-win-x64.zip`
- extracted bundle containing `stt_app.exe`

Build command:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build_windows_release.ps1
```

Output:

- `release\stt_app-win-x64\stt_app.exe`
- `release\stt_app-win-x64.zip`

### Phase 2: Code signing

Before broader rollout, sign the built executable and DLLs. This matters for:

- SmartScreen reputation
- antivirus / EDR
- corporate deployment acceptance

### Phase 3: Optional installer

Once the bundle is stable, consider adding a lightweight Windows installer.

Recommended options:

- **Inno Setup** for a pragmatic traditional installer
- **MSIX** only if you specifically need enterprise packaging policies

For the current app, Inno Setup is the simpler next step.

### Phase 4: Optional `winget`

`winget` is useful after the release artifact story is already stable.

It is **not** the primary packaging mechanism by itself. It is a distribution
channel on top of a stable installer or archive URL.

## Operational notes

- The packaged app should remain **windowless** during normal use and live in
  the tray.
- Models are still downloaded separately on first use unless pre-seeded.
- Remote-provider keys remain per-user runtime configuration, not build-time
  secrets.
- If corporate environments are a target, publish signed releases and keep an
  offline model-seeding path.

## Linux later

Do not block the Windows release path on Linux packaging.

The pragmatic order is:

1. make Windows releases easy and repeatable,
2. stabilize the app bundle story,
3. design Linux packaging separately (`AppImage`, `Flatpak`, or distro-native
   packaging depending on the Linux port architecture).
