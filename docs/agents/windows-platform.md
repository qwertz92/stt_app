# Windows platform integration: design decisions

Binding rules, condensed from the entries moved out of `AGENTS.md` on
2026-09-30; history and full measurements are in `docs/learning-log.md` and
git history. Read before changing hotkeys, tray icon, updates, taskbar
identity or Win32 handles. "Above/below" refers to this file; "Known
limitations" is `docs/agents/known-limitations.md`.

Verbatim pre-condensation text: `git show e608f86:docs/agents/windows-platform.md` (original AGENTS.md: `df2642a`).

- **Every child process is started with `process_tree.no_window_flags()`**
  (`CREATE_NO_WINDOW`). The installed app is windowed (`console=False` in
  `stt_app.spec`) and owns no console, so Windows opens a console window of
  its own for every console-program child -- node.exe, powershell.exe, npm,
  a key command. From a terminal (`uv run main.py`) the child shares that
  terminal's console, so a missing flag never shows during development: the
  ONNX/WebGPU runner opened a window on every Cohere/Granite transcription
  on an installed work machine, and closing it killed the runner, which the
  next transcription started again (fixed 2026-10-03). `run_bounded` adds
  the flag itself; `tests/test_child_process_windows.py` fails on a
  `subprocess` spawn call in `src/stt_app` (aliases included) without
  `creationflags`. It cannot see `QProcess`, `os.startfile` or a `**kwargs`
  spread's contents: those sites start GUI programs or are checked by hand.
- **Update checks use GitHub Releases directly** (`update_checker.py`): one
  asynchronous check after startup, a tray notification only for a newer
  release, manual checks in Settings and tray (a manual request during the
  startup check promotes it). Never download or run an installer
  automatically without a separate review. Release JSON is size-bounded, tags
  strict numeric SemVer, links limited to this repo's HTTPS release paths;
  dialogs use an explicit high-contrast stylesheet. A download needs the exact
  installer and `.sha256` assets, trusted redirects, the declared size and a
  matching checksum (partial data stays `.partial`); launching needs an
  Authenticode subject in `TRUSTED_WINDOWS_PUBLISHER_SUBJECTS`, which stays
  empty until a real signing identity exists.
- **A hotkey's key must never be a modifier**: `RegisterHotKey` matches
  modifiers exactly and pressing a modifier sets its own bit, so
  `Ctrl+Win+LShift` registers and never fires. `parse_hotkey` rejects it;
  every `FALLBACK_HOTKEYS` entry ends in a real key; `Ctrl+Win+Space` is
  excluded (Windows input-language switch).
- **A busy hotkey never overwrites the user's choice**:
  `_register_hotkey_with_fallback` keeps `settings.hotkey` and records the
  substitute in `_active_hotkey`, which the idle line shows;
  `_hotkey_reclaim_timer` retries the preferred one every
  `HOTKEY_RECLAIM_INTERVAL_MS`, never during a dictation. **After a resume,
  `refresh_hotkey_registration` repaints via `show_idle_status` only when
  `_hotkey_registration_state()` changed**: always repainting put "Idle" over
  Done and over an Error whose Insert was the only recovery; a real change
  must be shown (Retry stays in the tray, same `_last_failed_wav_bytes`).
- **AltGr**: reported as Ctrl+Alt; Ctrl+Alt hotkey messages are ignored while
  right Alt is down.
- **A record-hotkey press made while a stop held the Qt thread is dropped**
  (2026-10-10, review round 2 P2). WM_HOTKEY is handled on the Qt thread, so
  a press made during a stop's backlog wait (up to 12 s, see
  `docs/agents/audio-capture.md`) was dispatched after the stop and started
  a recording nobody wanted. The record filter passes the message's own time
  (`QtHotkeyEventFilter(..., with_message_time=True)` ->
  `toggle_recording_from_hotkey(MSG.time)`); after a stop that waited, the
  controller drops presses stamped no later than `message_clock_ms()` when
  that whole stop returned -- not when its wait did, since the persist,
  artifacts, silence scan and submit after it hold the Qt thread too
  (review round 3 F3) -- and logs `hotkey_press_during_stop_wait_ignored`. `MSG.time` is
  `GetTickCount`'s wrapping 32-bit millisecond count, so times are compared
  with `message_time_not_after`, and the mark is cleared by the first later
  press. Chosen over removing WM_HOTKEY with `PeekMessage` after the wait:
  that needs the hidden window's handle and would also eat a press made just
  after the wait. Tray and overlay clicks carry no message time and are not
  filtered; the cancel hotkey is not filtered either (a cancel pressed during
  the wait cancels the transcription the stop submitted, as asked).
- **Hotkey state follows `UnregisterHotKey` success**: on failure the manager
  stays registered and blocks a replacement; shutdown logs, disabling the
  cancel hotkey reports it.
- **Overlay visibility**: every recording start/stop (and a hotkey press
  during a pending finalize) reveals the overlay non-activating
  (`reveal_temporarily`) with native topmost z-order. `WM_POWERBROADCAST`
  resume restores it and refreshes all hotkeys once the session settles.
- **App icon**: `src/stt_app/assets/app_icon.ico`/`.png`, generated by
  `scripts/generate_app_icon.py` (rerun only on a design change) and
  committed; `app_icon.py` is the single loader for Qt, tray, dialogs (with
  fallback), wheel, PyInstaller and Inno Setup.
- **`show_overlay_hotkey`** (default `Ctrl+Alt+F11`, schema 21,
  `DEFAULT_SHOW_OVERLAY_HOTKEY_ID`) calls `controller.bring_overlay_to_front`.
  **`_normalize_optional_hotkey`: an empty value is a deliberate disable and
  stays empty**; only invalid non-empty values get the default; an empty
  `< 21` value migrates once. Save rejects conflicts with the recording and
  cancel hotkeys; it is part of `refresh_hotkey_registration`.
- **The tray icon is hand-registered (`win_tray_icon.py`) and its menu is a
  native `TrackPopupMenu`; do not move either back to Qt**:
  `QSystemTrayIcon`'s menu closed Windows 11's "hidden icons" flyout, and only
  both halves native keep it open (everything observable at menu time was
  measured and refuted: `SetForegroundWindow`, window styles and owners,
  activating our icon window first). `WindowsTrayIcon` mirrors the
  `QSystemTrayIcon` API used (`activated`/`ActivationReason`, `showMessage`,
  `show`, `setContextMenu`, `setToolTip`); `create_tray_icon` falls back to
  `QSystemTrayIcon` off Windows or on Win32 failure. The `QMenu` stays the
  model. Menu width is longest label + 70 px, so labels are the lever;
  `MNS_NOCHECK` drops the check column while nothing is checkable. Anything
  narrower than that would need owner-drawn items (hover, disabled and
  dark-mode states drawn by hand): not worth it.
  Invariants: one window class per process routing by HWND; every `ctypes`
  call declares `argtypes`/`restype`; re-add on `TaskbarCreated`; delete the
  icon before its window.
  **`show()` never raises; re-adding is a retry** (`_attempt_add`,
  `_ADD_RETRY_DELAYS_MS`): Explorer broadcasts `TaskbarCreated` before it
  accepts icons, and a raising `NIM_ADD` killed the app at logon.
  - **`_wanted_visible`, not `_visible`, drives the retry and the
    `TaskbarCreated` arm**, or a restart during a failing re-add lost the icon
    and every notification (`showMessage` checks `_visible`).
  - **A retry carries its generation** (no double `NIM_ADD` after
    `hide()`/`show()`).
  - **`SetIconVersionError` keeps the icon and reports** (the add succeeded);
    without version 4 `_handle_message` misdecodes lParam.
  `RegisterWindowMessageW` returning 0 (= `WM_NULL`) is stored as `None`.
  Unshowable notifications are logged. `scripts/diagnose_tray_flyout.py` and
  `scripts/experiment_native_tray_icon.py` reproduce the measurements.
- **Tray left-click (`Trigger`) calls `controller.bring_overlay_to_front`**;
  double-click opens Settings.
- **`tray_middle_click_toggle`** (default on) calls
  `controller.toggle_recording`, reading `controller.settings` at click time.
- **Taskbar identity**: `main._set_windows_app_user_model_id` sets a stable
  `APP_USER_MODEL_ID` before the first window, or windows group under
  python.exe.
- **`window_focus` and `hotkey` own their `WinDLL` handle opened
  `use_last_error=True`; read errors with `ctypes.get_last_error()`, never
  `ctypes.GetLastError()`.** Declaring types on the shared
  `ctypes.windll.user32` would change them for every caller (`text_inserter`,
  `win_tray_icon` own theirs too); declarations keep an `HWND` >= 0x8000_0000
  positive and `GetAsyncKeyState` a `SHORT`. `use_last_error=True` restores
  the thread error, so `Win32HotkeyApi.get_last_error` read 0 ("Unknown
  Windows hotkey registration error") instead of 1409. `wintypes.HWND` is
  `c_void_p`, so a declared call also accepts a legal 64-bit handle.
