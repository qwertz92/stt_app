# Linux port: roadmap

Written 2026-10-03. This is a plan, not a design that binds the code: nothing here is
implemented, and `docs/agents/*.md` stay the binding rules for the Windows app. Entered in
`docs/ROADMAP.md`.

Target (the owner's): a NixOS notebook running the newest Hyprland release with its Lua
configuration, a Wayland compositor with no desktop environment around it. **Also required, not
optional** (owner, 2026-10-03): GNOME and KDE on Wayland, and X11 sessions -- the port is not done
until all of them work; Hyprland comes first because it is the owner's own machine. The owner used
Sway (i3 for Wayland) before, so a wlroots compositor other than Hyprland is a realistic second
target. The notebook is configured but not running yet; the port starts once it runs, and is then
tested on that machine directly.

How to read the evidence tags. **[V]** = read in a primary source on 2026-10-03 (source named).
**[M]** = measured on 2026-10-03 on the owner's HomeBase (WSL Arch, `uv`, Nix 2.35.2). **[U]** =
unverified: from memory or inference; the sentence says what would settle it. Versions are those of
2026-10-03 (Hyprland 0.56.2, xdg-desktop-portal-hyprland 1.4.1, nixpkgs-unstable).

## 1. Verdict

**Yes, a Hyprland-first port is worth doing, and it is smaller than it looks, because the hard half
is already portable.** The top-level modules of `src/stt_app` hold about 45,000 lines; the
Windows-only code sits in about ten of them (section 2), and the recording, transcription, provider,
history and settings code is plain Python and Qt. The sound way to start is phase 0 plus the Hyprland
MVP of phase 1 (about 63-87 agent-hours); the full list below, including portals for GNOME/KDE and
packaging, is about 135-205.

The honest limits, stated up front because they decide whether the result feels like the Windows app:

- **No Linux window system lets an app grab a key globally by itself.** On Hyprland the hotkey has to
  be a line in the user's Hyprland config that runs a command or fires a portal shortcut. The app
  cannot pick or change its own hotkey (section 4.1). The Settings "Hotkeys" tab turns into a help
  page that prints the lines to paste.
- **A Wayland app cannot choose where its window sits, nor stay on top.** The overlay needs either
  Hyprland window rules (written by the user or by a one-time helper) or the layer-shell protocol,
  which has no Python binding (section 4.6).
- **Only the owner's notebook can prove it works.** HomeBase is Windows; its WSL runs Arch under
  WSLg, which is not Hyprland. Everything that touches the compositor (hotkey, typing into another
  window, overlay rules) is verifiable only on real Hyprland (section 6).

## 2. Inventory: what is Windows-specific today

Found by grepping `src` for `ctypes.windll/WinDLL`, `win32`, `comtypes`, `msvcrt`, `sys.platform`,
`os.name`, `powershell`, `%APPDATA%`, `creationflags`, `winsound`, `explorer`. **[M]** Test run on
Linux (section 6) confirms the list is complete for import time: only `win_tray_icon` breaks import.

| Feature | Module | Windows mechanism | Linux status |
| --- | --- | --- | --- |
| Global hotkeys (record, cancel, show overlay, re-paste) | `hotkey.py`, `main.py` (native event filters), `controller.py` (fallback and reclaim logic) | `RegisterHotKey`, `WM_HOTKEY`, `GetAsyncKeyState` | **Replace** (4.1) |
| Insert text at the caret | `text_inserter.py` (2,467 lines) | clipboard transaction + `SendInput` Ctrl+V or `WM_PASTE` | **Replace** (4.2) |
| Clipboard save/restore | `text_inserter.py` (`Win32ClipboardBackend`, pywin32) | `OpenClipboard`, all formats, sequence number, history-exclusion formats | **Replace**, much simpler (4.3) |
| Target-window tracking | `window_focus.py` | `GetForegroundWindow`, `GUITHREADINFO`, `SetForegroundWindow` | **Replace** (4.4) |
| Tray icon and menu | `win_tray_icon.py` | `Shell_NotifyIcon`, native `TrackPopupMenu` | **Falls back already** to `QSystemTrayIcon`; module breaks on import (4.5) |
| Overlay no-activate, topmost | `overlay_ui.py` | `WS_EX_NOACTIVATE`, `WM_MOUSEACTIVATE`, `SetWindowPos` | **Replace** (4.6) |
| Taskbar identity | `main.py` | `SetCurrentProcessExplicitAppUserModelID` | Already skipped off Windows |
| Audio-device change events | `audio_device_listener.py` | MMDevice `IMMNotificationClient` via comtypes | **Replace** (4.7); inert today, so a Linux run works without it |
| Input-device list | `audio_devices.py` | prefers the WASAPI host API | Portable, needs a PipeWire rule (4.7) |
| Start/completion tones | `controller.py` | `winsound.Beep`, falls back to `QGuiApplication.beep` | Replace (4.7) |
| App data paths | `app_paths.py` | `%APPDATA%\stt_app` | **Replace** with XDG dirs (4.9) |
| Single instance | `main.py` | `QLockFile` | Portable as is |
| Cross-process download lock | `file_lock.py` | `msvcrt.locking` / `fcntl.flock` | Already has the `fcntl` branch |
| Process-tree kill | `process_tree.py`, `benchmark_process.py`, `local_model_*.py` | job object, `NtResumeProcess`, `taskkill` | POSIX branch exists (`start_new_session`, `killpg`) |
| Reveal file in manager | `history_audio.py` | `explorer.exe /select,` | Falls back to `QDesktopServices.openUrl` on failure |
| Update installer | `update_installer.py`, `update_checker.py` | downloads `stt_app-win-x64-setup.exe`, PowerShell Authenticode check | **Not applicable**: notify only (4.9) |
| Benchmark hardware info | `benchmark_environment.py` | `powershell`, `GlobalMemoryStatusEx` | Replace with `/proc` reads (small) |
| Local runtimes | `transcriber/local_*.py`, `config.py` | DirectML preference; `onnxruntime-genai` is gated `platform_system == 'Windows'` in `pyproject.toml`; Node path looks for `node.exe` | CPU works; GPU and Node need work (4.8) |
| Key command for custom endpoint | `custom_endpoint_provider.py` | `shlex.split(posix=False)`, `CREATE_NO_WINDOW` | Has POSIX branch |
| Packaging and release | `stt_app.spec`, `installer/`, `scripts/*windows*`, `.github/workflows/windows-release.yml` | PyInstaller, Inno Setup | **New** (4.10) |
| Dependencies | `pyproject.toml`, `requirements-win.txt` | `pywin32`, `comtypes` already marked Windows-only | Lock already resolves for Linux **[M]** |

Two facts that shrink the work **[M]**: (a) `uv sync --group dev --locked` on Linux succeeds with
the existing `uv.lock` (PySide6 6.11.2, onnxruntime 1.30.0, faster-whisper, keyring with
`secretstorage` all install from Linux wheels); (b) pytest collected every test module except two
(`test_main_signals.py`, `test_win_tray_icon.py`, both because `main.py` imports `win_tray_icon`,
whose import-time `ctypes.WINFUNCTYPE` does not exist on Linux). Test results are in section 6.

The controller already receives its platform pieces by injection (hotkey managers, `text_inserter`,
`window_focus_helper`, audio listener, tray created in `main.py`), and what it calls on the inserter
is narrow: `insert_text_with_options(text, target, paste_mode)` plus three optional methods it looks
up with `getattr` (`flush_pending_restore`, `paste_pace_remaining_s`, `set_restore_failure_handler`).
The seams exist; most of the port is writing implementations behind them.

## 3. Plain-words background: why Linux is different

- **Wayland and global keys.** On Windows any program may ask the OS "tell me when Ctrl+Alt+Space is
  pressed anywhere". On Wayland an app only receives key events while one of its windows has
  focus; no request exists for "anywhere", on purpose (it would let any app log keystrokes). The
  compositor (Hyprland) sees every key, so the compositor is where a global hotkey lives.
- **Typing into another app.** Likewise no app may send keystrokes to another window by default.
  Three sanctioned doors exist: the compositor's *virtual-keyboard* protocol (a small tool such as
  `wtype` uses it), a kernel-level fake keyboard (`/dev/uinput`, what `ydotool` uses), or the
  *RemoteDesktop portal* (the desktop asks the user once, then allows the app to inject input).
- **Portals.** A portal is a D-Bus service that a desktop provides so that apps can ask for such
  things politely ("global shortcut", "inject keys", "share screen"). The shared front door is
  `xdg-desktop-portal`; a backend per desktop does the work (`-hyprland`, `-gnome`, `-kde`). Which
  backend implements which request differs per desktop; table in 4.1.
- **Clipboard.** On Wayland an app learns what is on the clipboard only when it has keyboard focus
  **[V]** (wayland.xml: the `selection` event is sent "immediately before receiving keyboard focus"
  and while focused). The overlay is a window that must never take focus, so the Qt clipboard
  (which the Windows code never needed to care about) is the wrong tool; `wl-clipboard` uses the
  *data-control* protocol, which lets a privileged client touch the clipboard without focus.
- **Window placement.** The Wayland window protocol (`xdg-shell`) has no call to say "put me at x,y"
  **[V]** (wayland.app/protocols/xdg-shell: positioning is the compositor's job). The overlay code
  that calls `move()` to reach a screen corner does nothing there.
- **Tray.** The Linux tray is a protocol called StatusNotifierItem (SNI) over D-Bus. Qt's
  `QSystemTrayIcon` speaks it **[V]** (doc.qt.io/qt-6/qsystemtrayicon.html names KDE, GNOME, Xfce,
  LXQt and DDE). It needs a *host* that draws the tray: Waybar's `tray` module on Hyprland.

## 4. Feature by feature: options and recommendation

### 4.1 Global hotkeys

The recording hotkey, cancel, show-overlay and re-paste hotkeys become four named **actions** that
something outside the app triggers.

| Option | Works on | Verdict |
| --- | --- | --- |
| **A. Compositor bind runs a command; the command talks to the running app** | Every compositor with its own bind config (Hyprland, sway, river...), also X11 WMs | **MVP.** No portal, no permissions. |
| **B. Portal `GlobalShortcuts`** | GNOME, KDE, Hyprland (below) | **Phase 3.** Same app code serves three desktops. |
| C. X11 `XGrabKey` | X11 sessions only | Phase 5, optional. |
| D. Read `/dev/input` directly (evdev) | everything, needs the `input` group | **Rejected:** it is a keylogger permission and bypasses the compositor's own binds. |

*Option A.* In Hyprland the config line is, per the current wiki (`hl.bind("keys", dispatcher)`,
`hl.dsp.exec_cmd`) **[V]**:

```lua
hl.bind("SUPER + F9", hl.dsp.exec_cmd("stt-app ctl toggle"))
```

The wiki main branch documents a Lua config; release notes for 0.55 and 0.56 mention both
`config/lua` and `config/legacy` fixes **[V]** (github.com/hyprwm/Hyprland releases), so the older
hyprlang syntax (`bind = SUPER, F9, exec, ...`) probably still loads **[U]**. The doc the app
prints must follow the user's version; check with `hyprctl version`. Binds may also fire on key
release (`release = true`), which allows push-to-talk later **[V]** (wiki, bind flags).

The command `stt-app ctl toggle` must be fast, because it starts a fresh process on every press.
Importing PySide6 costs hundreds of milliseconds **[U]**, so the `ctl` path must send its message
before importing Qt (a plain Unix socket in `$XDG_RUNTIME_DIR`, served by `QLocalServer` in the
app). A shell one-liner with `socat` is the fallback. Startup time of the `ctl` path is to be
measured in phase 1.

*Option B, what is verified.* The portal API (version 2) has `CreateSession`, `BindShortcuts`,
`ListShortcuts`, `ConfigureShortcuts` and the signals `Activated` and `Deactivated` with a
timestamp **[V]** (flatpak.github.io, GlobalShortcuts). The app passes a *preferred* trigger but the
backend decides. For Hyprland the backend is xdg-desktop-portal-hyprland (XDPH), and its source
shows what that means **[V]** (`src/portals/GlobalShortcuts.cpp`, master): it registers the
shortcut under the app id, returns an **empty `trigger_description`**, and never reads the preferred
trigger. The user then binds it himself with the `global` dispatcher, whose argument is
`appid:name`, and `hyprctl globalshortcuts` lists what is registered **[V]** (wiki, "Global hotkeys
and binds"):

```lua
hl.bind("SUPER + F9", hl.dsp.global("stt_app:toggle"))
```

So on Hyprland the portal is not easier for the user than option A (still one config line); its gain
is on GNOME and KDE, where the desktop shows a dialog and the user picks the key in a GUI.

Two more catches for B. An unsandboxed app needs an application id that matches a `.desktop` file
name and must register it with the portal's host Registry before the first portal call **[V]**
(flatpak.github.io, `org.freedesktop.host.portal.Registry`); whether Qt does this for the app is
**[U]**. And portal presence differs per desktop **[V]** (directory listings of each backend's
`src`, 2026-10-03):

| Backend | GlobalShortcuts | RemoteDesktop (key injection) |
| --- | --- | --- |
| xdg-desktop-portal-hyprland | yes | **no** (it has GlobalShortcuts, InputCapture, Screencopy, Screenshot) |
| xdg-desktop-portal-gnome | yes | yes |
| xdg-desktop-portal-kde | yes | yes |
| xdg-desktop-portal-wlr (sway etc.) | **no** | **no** (screencast, screenshot only) |

Consequence: on sway and other wlroots compositors only option A works; on Hyprland, option B gives
the hotkey but typing still needs 4.2.

**Recommendation.** Build an *action layer* first (`ctl toggle|cancel|overlay|repaste`, one
socket), because option B and C then just call the same actions. Keep the Windows
`HotkeyManager` untouched behind the same interface.

### 4.2 Typing the transcript into the focused app

| Option | Works on | Notes |
| --- | --- | --- |
| **`wtype`** (virtual-keyboard protocol) | Hyprland, sway and other wlroots compositors; **not GNOME, not KDE** | Types Unicode directly, no clipboard needed. **[V]** wayland.app support table: Hyprland 0.52.1 yes, Sway 1.11 yes, Mutter 51 no, KWin 6.7 no, Weston no. Upstream's last commit is January 2022 **[V]** (GitHub API); nixpkgs ships 0.4 **[M]**. |
| `ydotool` (uinput) | everything, X11 and Wayland | Needs a root-owned daemon `ydotoold` with access to `/dev/uinput` **[V]** (README). `type` is described as keycode-based and layout-limited **[V]** (README changelog); whether it types German umlauts correctly on a German layout is **[U]**, to be tested. Last release January 2023 **[V]**. Fallback only. |
| RemoteDesktop portal (`NotifyKeyboardKeysym`) | GNOME, KDE | The user approves once; a restore token can persist the permission **[V]** (flatpak.github.io, RemoteDesktop). Phase 3. |
| `xdotool` (XTest) | X11 only | Phase 5 **[U]**. |

**Recommendation for the MVP: `wtype`, direct typing, with clipboard+Ctrl+V as the second mode.**
Why direct typing first: it needs no clipboard (so no clipboard save/restore race, the part of the
Windows code that took most of its 2,467 lines), it works in terminals (a terminal needs
Ctrl+Shift+V rather than Ctrl+V, and the right key depends on the program), and the existing
*streaming* mode (live partial text, append-only insertion) maps naturally onto "type this delta".
Risks to measure on the notebook: typing speed for a 500-character dictation with `-d` delay
**[U]**, apps that react to every keystroke (autocomplete, chat clients), and how `wtype` handles
dead keys and umlauts under a German layout **[U]**.

Paste mode (second): `wl-copy` the text, send Ctrl+V or Ctrl+Shift+V with `wtype -M ctrl v -m ctrl`
(syntax **[V]**, wtype README), chosen by the focused window's class (list of terminal classes is a
setting; defaults **[U]**), then put the old clipboard back after a delay. This is a *new, small*
implementation of the `insert_text_with_options` surface; the Windows `TextInserter` is not reused
(its ctypes clipboard code has nothing to share).

### 4.3 Clipboard save and restore

Windows saves every clipboard format, marks the transcript so clipboard-history tools ignore it, and
restores later. Linux equivalent, MVP scope: save and restore **text and image only**, via
`wl-paste` / `wl-copy` (wl-clipboard 2.3.0 in nixpkgs **[M]**; the program needs the data-control
protocol, which Hyprland 0.52.1, Sway 1.11 and KWin 6.7 implement and Mutter 51 does not **[V]**
wayland.app ext-data-control-v1). Without data-control, `wl-clipboard` falls back to a hack that
briefly pops up a tiny transparent window and can hang if the compositor does not focus it
**[V]** (wl-clipboard man page, BUGS). `wl-copy --sensitive` exists to tell clipboard managers not
to keep the entry **[V]** (wl-copy source and man page), the analogue of the Windows history
exclusion. Managers on Hyprland such as `cliphist` watch the clipboard with `wl-paste --watch`
**[V]** (Hyprland wiki, clipboard managers), so whether they honour `--sensitive` is **[U]**.
Capturing arbitrary formats (files, rich text) stays out of scope; `wl-paste --list-types` shows what
is offered **[U]**.

The many `QGuiApplication.clipboard().setText` calls in dialogs (copy transcript, copy error) run
in windows the user just clicked, which have focus, so they keep working. The overlay's copy button
(a window that never takes focus) is **[U]**; test it, and route it through `wl-copy` if it fails.

### 4.4 Which window is the target

The Windows code remembers a window handle so it can re-focus the right window if the user clicked
the overlay. On Hyprland the overlay never takes focus (4.6), so the focused window stays the target
and no restore is needed in the normal case. For the drift check (did the user switch windows during
recording) the controller compares a `FocusSignature` tuple of three optional ints. Linux source of
truth: `hyprctl -j activewindow` (JSON; the exact field names are **[U]**, read one on the notebook)
or the event socket `.socket2.sock`, which pushes `activewindowv2>>ADDRESS` on every focus change
**[V]** (wiki, IPC). Prefer the event socket (one long-lived connection, no process per query); the
wiki warns that the *request* socket is handled synchronously and a connection left open freezes
Hyprland up to five seconds, so open it, write, close **[V]**. Re-focusing, if ever needed, is
`focus({ window })` through `hyprctl dispatch` **[V]**.
Other desktops have no equivalent API (GNOME and KDE expose none to ordinary apps **[U]**); there the
signature is always "unknown" and the controller's existing "cannot tell" path applies.

### 4.5 Tray icon

`QSystemTrayIcon` is already the fallback in `create_tray_icon` (it returns it on non-Windows). Two
changes: (a) `main.py` imports `win_tray_icon` at top level, which fails on Linux **[M]**; move the
Win32 class behind a lazy import; (b) a *host* must exist: on Hyprland add Waybar's `tray` module
**[V]** (Waybar man page `waybar-tray(5)`; items with `Passive` status are hidden by default). With no
host Qt reports the tray unavailable and the app must still run (overlay and `ctl` cover the
essential actions) **[U]**. Balloon messages (`showMessage`) need a notification daemon (mako, dunst
or swaync): Hyprland ships none **[U]**. GNOME needs an extension for the tray; Qt's docs say not
every activation reason works there **[V]**.

### 4.6 The always-on-top overlay

The overlay is a frameless `Qt.Tool` window with `WindowStaysOnTopHint` and
`WindowDoesNotAcceptFocus`. Qt's documentation promises "stay on top" only through the window
manager's cooperation and names no Wayland behaviour **[V]** (qnamespace.qdoc), and a code search of
`qt/qtwayland` finds `WindowStaysOnTopHint` only in an example, not in the plugin **[V]** (GitHub
code search, 2026-10-03; absence in a search is weaker evidence than reading the plugin), so treat
the flag as **ineffective on Wayland**.

| Option | Verdict |
| --- | --- |
| **Hyprland window rules** for the overlay (`float`, `pin`, `no_focus`, `move`, `size`) matched by window class | **MVP.** All five fields exist **[V]** (wiki, window rules); `pin` is ignored unless the window is floating. Position is a rule, so the app's own `move()` and the "remember corner" logic are bypassed on Linux. Whether a `no_focus` window still receives mouse clicks (its buttons) is **[U]**, test first. |
| **layer-shell** (`wlr-layer-shell`): a surface in the "overlay" layer, anchored to a corner, with keyboard interactivity `none` | The correct tool, but PySide6 has no binding. KDE's `LayerShellQt` is C++ only, no PyPI package exists (checked `layershellqt`, `layer-shell-qt`, and four variants **[V]**), and its `QT_WAYLAND_SHELL_INTEGRATION=layer-shell` plugin reads settings from `LayerShellQt::Window::get(QWindow*)`, a C++ call **[V]** (source). Needs a small C++ or `ctypes` shim: phase 3, fragile. Supported by Hyprland and KWin v5, Sway v4, **not by GNOME's Mutter** **[V]** (wayland.app). |
| Normal window, no guarantees | Fallback on GNOME. |

The Windows-specific bits (`WS_EX_NOACTIVATE`, `WM_MOUSEACTIVATE`, `SetWindowPos`) are already
guarded by `sys.platform == "win32"`, so Linux simply skips them. The app must set a stable Wayland
app id (`QGuiApplication.setDesktopFileName`) so that rules and portals can name it; the protocol
tells clients to use the `.desktop` file basename **[V]** (xdg-shell `set_app_id`).

Example rule, in the Lua form the wiki uses (field names from the wiki tables **[V]**, the exact
call shape `hl.window_rule({ match = ..., ... })` from its examples **[V]**; the class string is
**[U]** until the app sets it):

```lua
hl.window_rule({ match = { class = "stt_app" }, float = true, pin = true, no_focus = true })
```

### 4.7 Audio

`sounddevice` needs the system PortAudio library on Linux; the wheels do not bundle it **[V]**
(python-sounddevice docs, installation). nixpkgs has `sounddevice` 0.5.5 and `portaudio` **[M]**.
PipeWire answers ALSA and PulseAudio clients through `pipewire-pulse`, a drop-in PulseAudio server
**[V]** (docs.pipewire.org). Open questions to measure on the notebook: which PortAudio host API
shows the PipeWire default device (ALSA via `pipewire-alsa` or JACK), and whether `_input_host_api_index`
(currently "prefer WASAPI, else the default") picks sensibly **[U]**.

Device-change events replace MMDevice. Option: run `pactl subscribe`, which "keeps waiting for new
events" **[V]** (pactl man page); its output format is not documented there, so the parser is
**[U]** and is checked against real output first. This only needs to nudge the existing
"re-enumerate" path, which the controller already has through its watchdog.

Tones: replace `winsound.Beep` by playing a short generated sine wave through `sounddevice` itself
(no new dependency); `QGuiApplication.beep()` is the current fallback and may be silent **[U]**.

### 4.8 Local runtimes

| Runtime | Linux status |
| --- | --- |
| faster-whisper / CTranslate2 | Pure Python wheel over `ctranslate2`; CPU works with the lock **[M]**. NVIDIA GPU needs CUDA libraries (nixpkgs has a CUDA variant of ctranslate2 **[M]**, unfree). |
| onnx-asr (Parakeet, Canary) and Granite CTC | Pure Python + onnxruntime CPU; lock resolves onnxruntime 1.30.0 **[M]**. |
| Nemotron (`onnxruntime-genai`) | Wheels exist for Linux x86_64 and aarch64, cp312, `manylinux_2_28`, `requires onnxruntime>=1.30.0` **[V]** (PyPI JSON for 0.16.0). The `pyproject.toml` marker `platform_system == 'Windows'` must change; DirectML preference becomes CUDA or CPU. |
| Node path (Cohere, Granite 4.x via Transformers.js) | `onnxruntime-node` 1.29.0 ships `linux/x64` and `linux/arm64` binaries **[V]** (unpkg listing). Whether WebGPU works there is **[U]**; Node code looks for `node.exe` and Program Files. Treat as CPU-only until measured; phase 4. |
| Settings: device policies `dml`, `webgpu`, "ONNX Device" | Labels and defaults assume DirectML; Linux needs `cpu` (and `cuda` later). |

Which GPU the owner's notebook has is not known to this plan; it decides whether any GPU work is
worth it (section 8).

### 4.9 Secrets, paths, updates

- **Secrets.** `keyring` 25.7.0 on Linux uses the freedesktop Secret Service (a D-Bus service
  provided by GNOME Keyring, KWallet or KeePassXC) **[V]** (keyring README); the lock already pulls
  `secretstorage` and `jeepney` **[M]**. Hyprland has no such service by default: the owner enables
  one (NixOS: `services.gnome.gnome-keyring.enable`, **[U]**) or the existing "insecure plain-text
  fallback" in `secret_store.py` applies. Needed: a visible message when no backend is present.
- **Paths.** `app_paths._appdata_base_root` reads `APPDATA`; on Linux use `XDG_CONFIG_HOME` for
  settings, `XDG_DATA_HOME` for history and recordings, `XDG_STATE_HOME` for logs, `XDG_RUNTIME_DIR`
  for the control socket and lock (the XDG variable names are **[U]** from memory; one `platformdirs`
  call or ten lines). No migration needed on a fresh Linux install.
- **Single instance, file lock, process groups**: already portable (section 2).
- **Updates.** The checker queries GitHub Releases and works anywhere, but the asset name and the
  installer launch are Windows-only. On Linux: show "new version available" and link to the release;
  never self-install (the package manager owns it).

### 4.10 Packaging

| Route | For | Verdict |
| --- | --- | --- |
| **Nix flake** (dev shell first, package and NixOS/Home Manager module later) | the owner's NixOS notebook | **MVP for development, phase 4 for the package.** nixpkgs has PySide6 6.11.1 (project pins 6.11.2), faster-whisper 1.2.1, onnx-asr 0.12.0, keyring 25.7.0, groq 1.7.0, truststore 0.10.4, wtype 0.4, wl-clipboard 2.3.0, Hyprland 0.56.2, XDPH 1.4.1, Waybar 0.15.0 **[M]**. **Missing in nixpkgs:** `onnxruntime-genai`, `assemblyai` (pure Python, easy to package). `onnxruntime` there is 1.27.1 **[M]**, below the 1.30.0 that `onnxruntime-genai` 0.16.0 requires **[V]**. |
| uv-managed environment on NixOS | fastest way to run the exact locked wheels | Needs `programs.nix-ld.enable` (exists in nixpkgs **[V]**) and a library list, because prebuilt wheels expect a system loader NixOS does not provide **[U]**; workable for development, not for a package. |
| AppImage | other distributions | Phase 4, bundles Python and Qt; **[U]** whether PySide6's Wayland plugin loads from it. |
| Flatpak | other distributions | Portals are native there (global shortcuts, input injection), but the sandbox blocks `wl-copy`, `hyprctl`, `wtype`, and reading `$XDG_RUNTIME_DIR/hypr`; **poor fit for Hyprland**, reasonable on GNOME/KDE. Last priority. |
| `pipx` / `uv tool install` | anyone with system Qt libraries | Cheap to document once the code runs. |

Cache check, 2026-10-03: `pyside6`, `sounddevice`, `wtype`, `wl-clipboard` and `hyprland` are in
cache.nixos.org; `onnxruntime`, `ctranslate2`, `faster-whisper` and `onnx-asr` from the same
nixpkgs revision were **not** **[M]**. Cause unknown (the channel may simply be newer than the last
Hydra build, or the build is long or failing). If they stay uncached, building onnxruntime from
source on the notebook is a large cost (**[U]**, size and time unmeasured) and the uv + nix-ld
route becomes the practical one.

## 5. Proposed architecture

**Principle: a thin platform layer with one implementation per OS, selected once at startup;
Windows code moves nowhere and changes only where it must be made importable on Linux.**

New package `stt_app/platform/` (the existing `WindowFocusHelper` and `SecretStore` protocols are
the pattern). Interfaces:

| Interface | Windows implementation (existing) | Linux implementations |
| --- | --- | --- |
| `ActionTriggers` (register actions `toggle`, `cancel`, `overlay`, `repaste`; emits `activated(action)`) | `HotkeyManager` + `Qt*EventFilter` | `ControlSocket` (always), `PortalShortcuts` (phase 3), `X11Grab` (phase 5) |
| `TextInserter` (the narrow surface in section 2) | `TextInserter` + `Win32ClipboardBackend` | `WtypeInserter`, `PortalInserter` (phase 3), `XdotoolInserter` (phase 5) |
| `ClipboardAccess` (get/set text, sensitive flag) | inside the Windows backend | `WlClipboard`, `QtClipboard` fallback |
| `WindowFocusHelper` (exists) | `Win32WindowFocusHelper` | `HyprlandFocus`, `NullFocus` |
| `TrayFactory` (`create_tray_icon` exists) | `WindowsTrayIcon` | `QSystemTrayIcon` (already there) |
| `OverlayPlatform` (`apply_no_activate`, `apply_z_order`, `reveal`) | the `sys.platform == "win32"` branches in `overlay_ui.py` | `HyprlandRulesOverlay`, `LayerShellOverlay` (phase 3), `PlainOverlay` |
| `DeviceChangeListener` (exists as `AudioDeviceChangeListener`) | MMDevice via comtypes | `PactlSubscribeListener` |
| `ProcessTree` | job object, `taskkill` | already `killpg` |

What changes in shared code, honestly listed: (1) the hotkey *settings* and the fallback/reclaim
machinery in `controller.py` (`HotkeyRegistrationError`, `_hotkey_reclaim_timer`, AltGr handling) are
Windows semantics: on Linux there is no "registration that can fail because another program holds
the key", so the Linux backend reports success and the Hotkeys tab shows the config snippet instead;
(2) `settings_store` validates hotkeys with `parse_hotkey` (virtual-key tables), which must be
skipped when the backend is not key-based; (3) paste-mode labels ("SendInput", "WM_PASTE") need a
per-platform list; (4) `main.py` wires four event filters, to move into the Windows backend's setup;
(5) strings that name Windows (`wsl.exe` hint in the key-command help, tray labels) get a platform
branch.

**Keeping Windows untouched.** Rule: no behaviour change on Windows, proven by the existing suite
(run on Windows as today) plus a Windows smoke run of each refactor before it merges.
Do the refactor mechanically first (move code behind the interface, same tests), add Linux code
second, so a regression on Windows is attributable to a pure move.

**CI.** The repository is **public** **[M]** (`gh repo view qwertz92/stt_app --json visibility`
answered `PUBLIC`), so Linux runner minutes are free; the existing `quality.yml` runs only on
`windows-latest`. Add an `ubuntu-latest` job with `uv sync --locked`, `ruff`, and a growing set of
Linux-safe tests. The local run stays the first gate (owner's rule). A Linux CI job can cover pure
logic, the control socket, the XDG paths and the `wtype`/`wl-copy` command construction with fake
executables; it cannot cover the compositor.

## 6. What Linux gives us to test today

**[M]** probe, 2026-10-03: the unmodified repository copied to a WSL Arch scratch folder,
`uv sync --group dev --locked`, then pytest with `QT_QPA_PLATFORM=offscreen` (offscreen is for this
probe only; the repository's own rule against it concerns Windows pixel tests).

The two modules that import `win_tray_icon` could not even be collected (see section 2). With those
two files ignored the run finished in 4 min 47 s: **3,596 passed, 211 failed, 23 errors, 44
skipped**, so about 93% of the suite already passes on Linux with no change. The 234 failures and
errors sit in a few files and look Windows-specific by name, not by cause: `test_text_inserter` 73
and `test_window_focus` 25 (they test the Win32 code), `test_import_model` 31,
`test_benchmark_script` 30, `test_smoke_test_script` 23, `test_setup_node_windows` 11,
`test_download_model_script` 9, `test_create_release` 9, `test_release_version` 7 (scripts and path
handling), `test_settings_dialog_general_ux` 4 (pixel layout of the tab bar and popup, which the
repository's own rule says differ outside the real Windows desktop), and 12 failures across
nine other files. The causes of the script-test failures were not read one by one **[U]**; the
phase 0 task is to classify them (Windows-only, to be skipped with a reason, or a real portability
bug) and make the Linux job green. A Linux CI job could therefore start with roughly 3,600 tests.

What cannot be tested on HomeBase: Hyprland itself, binds, `wtype` into a real window, the tray host,
overlay rules. Options: the owner's notebook (reliable), or a headless Hyprland in a NixOS VM
(whether HomeBase can run it, and whether Hyprland has a headless backend for that, is **[U]**; the
same VM would also catch a missing `programs.hyprland` module). Phase 0 therefore ends with a list
of exactly what the owner must try on the notebook and what output to bring back.

## 7. Phases, estimates, risks

Estimates are agent-hours of focused work *including* the review rounds the owner's rules demand;
they are not calendar time and carry the same wide error as any estimate made before writing the
first line. Where a phase needs the owner's notebook the waiting time is not counted.

| Phase | Content | Agent-hours |
| --- | --- | --- |
| **0. Groundwork** | `stt_app/platform/` skeleton and selection; lazy import of `win_tray_icon`; make `import stt_app.main` and the controller constructible on Linux; Linux CI job with the tests that already pass; mark Windows-only tests; XDG paths; `onnxruntime-genai` marker kept as is | 8-12 |
| **1. Hyprland MVP** | `ctl` socket and fast CLI; `WtypeInserter` (direct typing) and `WlClipboard` paste mode; `HyprlandFocus` (events); tray via SNI and notification check; overlay rules and a printed Hyprland snippet; audio check and `PactlSubscribeListener`; tones; Settings: Hotkeys tab becomes "Actions" help, paste-mode labels, ONNX device list, update notice only; Nix flake dev shell; batch transcription with local CPU and remote engines on the notebook | 55-75 |
| **2. Streaming and hardening** | streaming mode over typing (append-only deltas, failure semantics when `wtype` fails mid-stream), terminal detection list, push-to-talk via bind release, secrets "no keyring" message, XDPH `global` shortcut docs, polish from the owner's first week of use | 14-22 |
| **3. Portals for other desktops** | `PortalShortcuts` (GNOME, KDE, Hyprland) incl. host Registry and `.desktop` file; `PortalInserter` via RemoteDesktop with a persisted restore token; layer-shell shim for the overlay on Hyprland/KDE | 24-40 |
| **4. Packaging** | Nix package and NixOS/Home Manager module (assemblyai package; onnxruntime-genai through wheel or skipped); AppImage; `pipx` doc; Flatpak only on request; Node/WebGPU and CUDA measurements for local models | 24-40 |
| **5. X11** | `XdotoolInserter` and `X11Grab` | 10-16 |
| **Total** | | **135-205** (MVP alone: 63-87 with phase 0) |

Recommended order: 0, then 1, then stop and let the owner use it before spending phase 2+; only
phase 0+1 are needed to answer "does it feel right".

**The biggest risks, each with the way it would move the estimate:**

1. **Hyprland behaviour only checkable on the notebook** (does a `no_focus` overlay still accept
   clicks, how `wtype` handles German umlauts and dead keys, whether the SNI host shows the icon).
   Each surprise costs a redesign of one backend, 3-10 h; at worst the overlay needs the layer-shell
   shim (+10-15 h) in phase 1 instead of phase 3.
2. **Direct typing quality.** If `wtype` is too slow or breaks in common apps, the clipboard paste
   mode becomes primary and the Linux inserter grows toward the Windows one's transaction logic
   (race with clipboard managers, restore timing): +15-25 h.
3. **Nix packaging of the native dependencies.** `onnxruntime` and `ctranslate2` are not in the binary
   cache today and `onnxruntime-genai` is not in nixpkgs. If a source build or custom packaging is
   needed, phase 4 grows by 10-20 h; the uv + nix-ld route avoids it for development but is not a
   clean package.
4. **Hotkey semantics are a product change, not just code.** The user loses an in-app hotkey picker
   and the busy-key fallback. If the owner wants it back on GNOME/KDE, only the portal path gives it
   (phase 3); on Hyprland it is impossible by design.
5. **Verification without the machine.** If no Hyprland test environment exists besides the notebook,
   every compositor-facing change needs a round trip through the owner; elapsed time, not hours,
   grows (not in the estimate).

## 8. Decisions needed from the owner

Answered on 2026-10-03:

- **Timing:** later -- the port starts once the notebook runs; until then this plan only.
- **Scope:** GNOME, KDE (Wayland) and X11 are required, not optional, so phases 3 and 5 belong
  to "done"; Hyprland first.
- **Hyprland:** always the newest release, Lua configuration (0.55+); the hyprlang syntax needs no
  support. Sway was used before.

Still open (ask when the notebook runs):

- Which GPU the notebook has (decides whether CUDA/ROCm for CTranslate2 or ONNX is in scope).
- Whether direct typing (`wtype`) is acceptable as the default insertion, or clipboard paste must be
  the default (it changes phase 1 content).
- Whether a status bar with a `tray` module and a notification daemon are in use.

## 9. Not checked

Everything tagged **[U]** above. In addition: no code was changed; the Windows suite was not run;
nothing was executed on Hyprland, in a portal, or with `wtype`/`wl-clipboard`; Nix evaluation and
cache checks used `nixpkgs-unstable` as of 2026-10-03 and move daily; the Hyprland wiki read was its
`main` branch, which may be ahead of the owner's installed version.
