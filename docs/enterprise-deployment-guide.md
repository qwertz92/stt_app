# Enterprise Deployment Guide (Windows / Corporate)

This guide describes how to deploy `tts_app` in heavily restricted corporate environments (Zscaler, GPO/AppLocker, no `uv.exe`, limited internet access).

## Summary

- This app must run on **native Windows** (not Linux/WSL) because it uses Win32 APIs for hotkey/clipboard/input.
- `uv` is optional. If `uv.exe` is blocked, use the standard Python + pip workflow.
- For strictly air-gapped networks, a **wheelhouse (offline package folder)** is the most robust method.

## What is a wheel?

A wheel is a pre-built Python package in `.whl` format.

- Comparable to a pre-compiled binary package.
- Advantage: no local compilation required.
- Installation is faster and more stable than source builds.
- Especially important for packages with native components (e.g. `pywin32`, `numpy`, `ctranslate2`).

Example:
- `pywin32-308-cp312-cp312-win_amd64.whl`
  - `cp312`: Python 3.12
  - `win_amd64`: Windows x64

## Option A: Standard Windows setup without uv

Prerequisites:
- Python 3.12 installed
- Access to an internal PyPI proxy/Artifactory

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements-dev-win.txt
python main.py
```

Tests:

```powershell
python -m pytest
python scripts/smoke_test.py
```

## Option B: Offline / wheelhouse setup (recommended for heavily restricted networks)

### B1) On a build machine with package access

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip download -r requirements-dev-win.txt -d wheelhouse
```

Result: folder `wheelhouse/` containing all `.whl` files (and possibly sdists).

### B2) Distribute the wheelhouse internally

- Upload as ZIP to your internal artifact repository.
- Or place on a file share.

### B3) Install on the target machine (no internet)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --no-index --find-links .\wheelhouse -r requirements-dev-win.txt
python main.py
```

## Option C: PyInstaller EXE (deployment-friendly)

The project includes `tts_app.spec`.

Build:

```powershell
python -m pip install pyinstaller
pyinstaller tts_app.spec
```

Notes:
- The EXE should ideally be code-signed, otherwise some corporate policies/EDR may block it.
- EDR may still block input injection; this requires a policy exception for the signed application.

## WSL: Does it help?

Short answer: **no**, not for running the app.

- WSL/Linux can be useful for git, editors, and build scripts.
- The app itself requires Windows-specific APIs (`pywin32`, `RegisterHotKey`, `SendInput`, foreground window).
- Therefore: always run the app in a native Windows Python environment.

## Troubleshooting checklist (corporate)

1. **`uv.exe` blocked by GPO:**
   - Use the setup without `uv` (Option A/B).

2. **`irm ... | iex` blocked by Zscaler:**
   - Do not run installer scripts from the internet.
   - Use an internal artifact source or wheelhouse instead.

3. **`pywin32` cannot be installed on WSL/Linux:**
   - Expected behavior (Windows-only package).
   - Run the app on Windows.

4. **App transcribes but does not insert text:**
   - Switch paste mode in Settings (`auto`, `wm_paste`, `send_input`).
   - If problems persist, `keep_transcript_in_clipboard` leaves the text in the clipboard for manual pasting.

5. **Target application is elevated (admin) or protected:**
   - Run the dictation app with matching privileges, or request an IT policy exception for UI interaction.

## Recommended rollout in corporate environments

1. Build and version an internal wheelhouse.
2. Standardize on Python 3.12 + venv + offline install.
3. Include the smoke test as a mandatory verification step (`python scripts/smoke_test.py`).
