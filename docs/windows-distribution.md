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
- `stt_app-win-x64-setup.exe.sha256`

The workflow uploads the ZIP and installer together as one short-lived
candidate artifact. It does not upload the extracted portable folder a second
time because that duplicates the ZIP contents and consumes artifact storage.

Local build commands:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build_windows_release.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\build_windows_installer.ps1 -SkipBundleBuild
```

Output:

- `release\stt_app-win-x64\stt_app.exe`
- `release\stt_app-win-x64.zip`
- `release\installer\stt_app-win-x64-setup.exe`

### Before a release: check what no test double can stand in for

The test suite drives fakes of the clipboard, of file locks, of the provider
SDKs and of the packaged executable. Four scripts run the same code against
the real thing. Each prints one `OK` / `FAIL` / `SKIP` line per check and a
`SUMMARY` line, and exits 0 (at least one check ran and none failed), 1 (a
check failed, or the script itself crashed or was interrupted -- recorded as a
failed check, so the `SUMMARY` line and the report still exist) or 2 (nothing
was measured: this machine cannot run it, or every check was skipped, for
example because no provider key is stored). Each works in a throwaway
`APPDATA` so the real `%APPDATA%\stt_app` is unreachable, and writes its
evidence to `--report`.

| Script | What it proves on the real machine | Needs | Time |
| ------ | ---------------------------------- | ----- | ---- |
| `scripts\release_check_file_locks.py` | A settings, history or last-recording file held open by another program (share mode 0) is reported and never overwritten; nothing is moved aside; everything is back after the lock ends | Windows | ~1 s |
| `scripts\release_check_clipboard_paste.py` | Text, HTML, an image, a file list and Explorer's cut/copy marker survive a dictation byte for byte, and Explorer still moves a cut file; the transcript lands exactly once on the WM_PASTE and the SendInput road; the deferred restore; a foreground change refuses the paste | Windows, pywin32. Two parts take the keyboard focus for a few seconds; the script waits for 12 s of idle first, `--skip-focus` leaves them out | ~25 s |
| `scripts\release_check_providers.py --clip de.wav:de --clip en.wav:en` | AssemblyAI batch and realtime and Groq batch answer with a transcript through the keys stored in the Windows credential manager, and a quit during the AssemblyAI poll ends the wait. A provider without a stored key is skipped. Keys are never printed or written to the report | Network, stored keys, real speech recordings (16 kHz mono PCM16 for the realtime case). Uses paid quota: about one request per clip per provider | ~1 min per clip |
| `scripts\release_check_frozen_bundle.py --exe ...\stt_app.exe --clip de.wav` | The PyInstaller bundle's scan worker finds the cached models, its benchmark worker transcribes with one model per local runtime (onnx-asr, faster-whisper, ORT GenAI, Node/Transformers.js), and the GUI stays up for 40 s without an error in its own log. `--node-models` adds all three Node models on WebGPU and CPU | A built `onedir` bundle, the models in the local cache (nothing is downloaded), a real speech recording | ~70 s, ~1 min more with `--node-models` |

```powershell
.venv\Scripts\python.exe scripts\release_check_file_locks.py
.venv\Scripts\python.exe scripts\release_check_clipboard_paste.py
.venv\Scripts\python.exe scripts\release_check_providers.py --clip C:\clips\de_sample.wav:de
.venv\Scripts\python.exe scripts\release_check_frozen_bundle.py --exe release\stt_app-win-x64\stt_app.exe --clip C:\clips\de_sample.wav --language de
```

The repository's own `samples/benchmark_sample.wav` is synthetic sine tones,
so the two checks that transcribe need a recording of real speech.

Then start the release workflow by hand once (`gh workflow run
windows-release.yml`): it runs the identical gate the tag will run and uploads
the installer and ZIP as a three-day artifact, so a red gate costs a dry run
instead of a published tag.

What these four do **not** cover, and nothing automated does: running the
installer, and installing over an older installed version.

### Phase 2: Publish official GitHub Releases from tags

When you want a public or durable release from `main`, use the guarded release
script:

```powershell
python .\scripts\create_release.py
```

The script fetches tags, shows the latest numeric release tag, and proposes the
current project version when it is newer than the latest release tag; otherwise
it proposes the next patch version. Press Enter to accept the default, or enter
an explicit numeric version such as `0.7.0`. It then asks for an explicit `yes`
confirmation before it changes files, runs `uv lock`, runs checks, creates the
release metadata commit when one is needed, pushes `main`, creates the annotated
tag, and pushes the tag.

Release pages intentionally publish both user-facing Windows assets:

- `stt_app-win-x64-setup.exe` is the recommended installer for most users.
- `stt_app-win-x64-setup.exe.sha256` lets the app verify an installer download.
- `stt_app-win-x64.zip` is the portable bundle for users who do not want an
  installer.

GitHub also displays automatic "Source code" ZIP/TAR archives on every release.
Those are normal GitHub-generated source snapshots for developers, not installable
app builds.

The lower-level manual path is still available when you intentionally need it:

```powershell
python .\scripts\release_version.py bump 0.7.0
uv lock
git add pyproject.toml uv.lock src\stt_app\__init__.py installer\windows\stt_app.iss
git commit
git push origin main
git tag -a v0.7.0 -m "Release v0.7.0"
git push origin v0.7.0
```

The workflow `.github/workflows/windows-release.yml` is wired so that:

- manual runs build candidate artifacts only,
- `v*` tags build the same artifacts and attach them to a GitHub Release.
- tag builds fail fast unless the tag matches `pyproject.toml`,
  `stt_app.__version__`, the installer fallback version, and `uv.lock`
  (`v0.7.0` requires `version = "0.7.0"`).
- tag builds fail fast when the tag is older than an existing numeric release
  tag, so accidentally releasing `v0.3.0` after `v0.3.1` is blocked.

This gives you a clean separation:

- **manual action run** = tester build / release candidate
- **tag push** = real release

### Phase 3: Code signing

Before broader rollout, sign the built executable and installer with Windows
Authenticode. This matters for:

- SmartScreen reputation
- antivirus / EDR
- corporate deployment acceptance

A GitHub **Verified** commit and a signed Windows executable solve different
problems:

- a [verified commit or tag](https://docs.github.com/en/authentication/managing-commit-signature-verification/about-commit-signature-verification)
  proves which Git identity created that Git object;
- an [Authenticode signature](https://learn.microsoft.com/en-us/windows/win32/seccrypto/signtool)
  lets Windows verify who published the downloaded `.exe` and whether it
  changed after signing.

The former does not automatically sign release assets. The current release
workflow creates a checksum, but it does not yet own a code-signing certificate.
Until that prerequisite is configured, the app can download and checksum an
update but deliberately refuses to launch it automatically.

Recommended signing rollout:

1. Obtain a publicly trusted code-signing identity from a certificate authority
   or a managed service such as
   [Azure Artifact Signing](https://learn.microsoft.com/en-us/azure/artifact-signing/how-to-signing-integrations).
   Keep private signing keys out of the repository and ordinary GitHub secrets.
2. Add a GitHub Actions signing step after the installer build and before
   `Create installer checksum`. A managed signing action or hardware-backed key
   is preferable to exporting a reusable private key into the runner.
3. Sign at least `stt_app.exe` and `stt_app-win-x64-setup.exe`, using an RFC 3161
   timestamp so existing releases remain valid after certificate expiry.
4. Verify both signatures in CI and fail the release if Windows does not report
   `Valid`.
5. Add the exact certificate subject reported by
   `Get-AuthenticodeSignature` to
   `TRUSTED_WINDOWS_PUBLISHER_SUBJECTS` in `update_installer.py`. This pins the
   updater to the expected publisher instead of accepting any valid signer.
6. Generate the SHA-256 file only after signing, then publish the installer and
   checksum together.

### In-app update behavior

The update checker reads the latest GitHub Release asynchronously. For a
one-click-capable release, it requires the exact installer asset and its exact
`.sha256` companion. The download dialog then:

1. downloads without blocking the Qt UI,
2. keeps incomplete bytes in a `.partial` file,
3. enforces the size declared by GitHub,
4. verifies SHA-256 before publishing the local installer,
5. asks Windows to verify the Authenticode signature and pinned publisher,
6. shows **Install update** only after every check succeeds.

Installation is always a separate user click. Unsigned, unpinned, redirected,
oversized, truncated, or checksum-mismatched installers are never launched by
the app. Older releases without the checksum asset retain the safe **Open
release** fallback.

### Phase 4: Optional installer polish

The repository already wraps the portable bundle in an Inno Setup installer.
After signing is in place, continue polishing that established path instead of
introducing a second installer format without a concrete distribution need.

Recommended options:

- **Inno Setup** for a pragmatic traditional installer
- **MSIX** only if you specifically need enterprise packaging policies

For the current app, Inno Setup remains the simpler path; further work mainly
means branding, upgrade handling, and optional auto-start settings.

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
