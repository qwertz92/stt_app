# Windows Distribution Strategy

This document describes the recommended way to ship `stt_app` to normal
Windows end users.

## Recommendation

**Recommended path:** publish a signed **PyInstaller `onedir` bundle** on
GitHub Releases, and pair it with an **Inno Setup installer** for users who
expect a normal Windows install flow.

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

## What `onedir` means

A PyInstaller **`onedir`** build is a self-contained application folder.

Instead of shipping only one `.py` file or expecting users to install Python,
the build process collects the app into a directory such as:

```text
stt_app-win-x64/
  stt_app.exe
  python312.dll
  PySide6\...
  Qt6\...
  *.pyd / *.dll
```

The important part is:

- `stt_app.exe` is the program the end user launches,
- the surrounding DLLs, Qt plugins, and Python runtime stay beside it,
- the user does not need Python, `uv`, or the repo.

That is why this is called **one-directory** or **`onedir`** packaging: the
application is delivered as one folder that already contains everything needed
to run.

## Why `onedir` first?

The project already includes a PyInstaller spec, and `onedir` is the safer
delivery format for this app.

Compared with `onefile`:

- startup is faster,
- fewer extraction-time surprises,
- native libraries are easier to debug,
- antivirus/EDR false positives are usually less painful.

For this app, the extra folder is an acceptable tradeoff.

## Portable bundle vs installer

There are now two end-user packaging layers:

- **Portable bundle (`stt_app-win-x64.zip`)**: the user downloads the ZIP,
  extracts it anywhere, and launches `stt_app.exe`.
- **Installer (`stt_app-win-x64-setup.exe`)**: the user runs a standard
  Windows setup wizard, the app is copied into
  `%LOCALAPPDATA%\Programs\Voice Dictation App`, and Start-menu shortcuts are
  created.

The installer does **not** replace the portable build. It wraps the already
built `onedir` folder. This keeps the packaging pipeline simple and avoids two
different runtime layouts.

For local maintainer builds, `scripts/build_windows_installer.ps1` auto-detects
typical Inno Setup installations in both machine-wide and current-user
locations.

## Recommended rollout

### Phase 1: Build candidate artifacts on demand

Use GitHub Actions `workflow_dispatch` to build artifacts manually when you
decide a commit or branch is stable enough to hand to testers.

This should **not** run on every commit. Reasons:

- not every commit is release-worthy,
- Windows packaging takes longer than normal test CI,
- release artifacts create noise if generated continuously,
- maintainers usually want to choose when a build becomes shareable.

Candidate outputs:

- `stt_app-win-x64.zip`
- `stt_app-win-x64-setup.exe`
- workflow artifact containing the extracted `stt_app-win-x64\` folder

Build command:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build_windows_release.ps1
```

Output:

- `release\stt_app-win-x64\stt_app.exe`
- `release\stt_app-win-x64.zip`
- `release\installer\stt_app-win-x64-setup.exe`

### Phase 2: Publish official GitHub Releases from tags

When you want a public or durable release, create and push a version tag such
as `v0.2.0`.

The workflow `.github/workflows/windows-release.yml` is wired so that:

- manual runs build candidate artifacts only,
- `v*` tags build the same artifacts and attach them to a GitHub Release.

This gives you a clean separation:

- **manual action run** = tester build / release candidate
- **tag push** = real release

### Phase 3: Code signing

Before broader rollout, sign the built executable and DLLs. This matters for:

- SmartScreen reputation
- antivirus / EDR
- corporate deployment acceptance

### Phase 4: Optional installer polish

Once the bundle is stable, consider adding a lightweight Windows installer.

Recommended options:

- **Inno Setup** for a pragmatic traditional installer
- **MSIX** only if you specifically need enterprise packaging policies

For the current app, Inno Setup is the simpler next step. The repository now
contains that installer path already; further work here mainly means polish
(branding, code signing, upgrade handling, and optional auto-start settings).

### Phase 5: Optional `winget`

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

## What an end user can do today

After a maintainer runs the build locally or publishes a GitHub Release, an end
user can:

1. download the ZIP or installer,
2. avoid installing Python and `uv`,
3. launch the app like a normal Windows program,
4. keep it running in the tray without an open terminal.

Without those generated artifacts, the GitHub source repo is still only the
developer distribution path.

## Linux later

Do not block the Windows release path on Linux packaging.

The pragmatic order is:

1. make Windows releases easy and repeatable,
2. stabilize the app bundle story,
3. design Linux packaging separately (`AppImage`, `Flatpak`, or distro-native
   packaging depending on the Linux port architecture).
