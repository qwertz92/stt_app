# Windows platform integration: design decisions

Binding project rules, moved verbatim from `AGENTS.md` on 2026-09-30 so that
the always-loaded agent file stays small. Read this file before changing
hotkeys, tray icon, updates, taskbar identity and Win32 handles. Entries keep their original order, so "the entry above/below" still
refers to this file; "Known limitations" is `docs/agents/known-limitations.md`.
History is in `docs/learning-log.md`.

- **Update checks**: update discovery uses GitHub Releases directly through
  `update_checker.py`; no custom domain or update server is required. The app
  schedules one asynchronous check after startup and shows a tray notification
  only when a newer release exists. Manual checks are available from Settings
  and the tray menu. Keep update checks non-blocking and avoid downloading or
  executing installers automatically without a separate review. Release JSON is
  size-bounded, tags use strict numeric SemVer, and release links are restricted
  to this repository's HTTPS GitHub release paths. A manual request during the
  startup check promotes the active request so its result remains visible.
  Update dialogs use an explicit high-contrast stylesheet instead of platform
  button hover colors. An in-app download requires the exact installer and
  `.sha256` release assets, trusted HTTPS GitHub redirects, the declared byte
  size, and a matching checksum; incomplete data stays in `.partial`. Launching
  the installer additionally requires Windows to report a valid Authenticode
  signature whose full subject is pinned in
  `TRUSTED_WINDOWS_PUBLISHER_SUBJECTS`. Keep that set empty until the real
  signing identity exists; a GitHub Verified commit does not sign release EXEs.
- **A hotkey's key must never be a modifier**: `RegisterHotKey` matches the
  modifier state *exactly*, and pressing a modifier raises its own modifier
  bit, so `Ctrl+Win+LShift` registers `Ctrl+Win` + key `LSHIFT` while the real
  keystroke reports `Ctrl+Win+Shift` — a different hotkey. Registration
  *succeeds*, so this failed silently: the app reported a working hotkey that
  could never fire, which is exactly what the old `FALLBACK_HOTKEY` did. Proven
  by registering both variants at once: Windows accepts them as two separate
  hotkeys. `parse_hotkey` now rejects a modifier as the key, which also stops a
  user configuring one in Settings, and every entry in `FALLBACK_HOTKEYS` ends
  in a real key. `Ctrl+Win+Space` is deliberately not among them: Windows owns
  it for input-language switching.
- **A busy hotkey never overwrites the user's choice**: another program holding
  the preferred combination (a terminal, an IDE) is temporary, but persisting
  the fallback into settings made it permanent — once that program closed, the
  app had already forgotten what the user wanted. `_register_hotkey_with_fallback`
  keeps `settings.hotkey` and records the substitution in `_active_hotkey`
  only; `_hotkey_reclaim_timer` retries the preferred one every
  `HOTKEY_RECLAIM_INTERVAL_MS` and stops once it succeeds. The idle line shows
  `_active_hotkey`, because printing the stored preference would name a key
  that does nothing. The reclaim never swaps the binding while a dictation is
  running. **A resume repaints the idle line**: `refresh_hotkey_registration`
  re-registered all four hotkeys and painted nothing, while every other
  writer of the registration state calls `show_idle_status`, so a resume that
  substituted a fallback -- or lost every combination -- left the overlay
  advertising a key that no longer fired, and one that repaired an earlier
  failure left it on Error until the next save. It repaints through
  `show_idle_status` -- whose session check keeps it off a live overlay --
  **only when `_hotkey_registration_state()` changed**: the unconditional
  repaint that fixed this first painted "Idle" over a finished Done
  transcript and over the Error whose Insert button is the only way to
  recover a failed streaming tail, after every wake from sleep and 500 ms
  after the two "open recordings folder" buttons. When the registration
  *did* change the repaint still replaces a Done or Error result, because
  the user has to see the key that now fires; a failed transcription's
  Retry then lives in the tray's "Retry transcription" action, which reads
  the same retained bytes (`_last_failed_wav_bytes`) as the overlay button.
- **AltGr hotkey alias**: Windows reports AltGr as Ctrl+Alt. The hotkey
  manager ignores Ctrl+Alt hotkey messages while the right Alt key is down so
  AltGr combinations do not trigger dictation accidentally.
- **Hotkey state follows Win32 cleanup success**: a failed `UnregisterHotKey`
  keeps the manager marked registered and blocks replacement registration.
  Shutdown logs and continues, while disabling a cancel hotkey reports the
  cleanup failure instead of pretending the key was released.
- **Overlay visibility after activity/resume**: every recording start *and
  stop* (and a hotkey press while a streaming finalize is pending) re-presents
  the overlay without activation and reasserts native Windows topmost z-order,
  so a floating overlay shows the new state on the hotkey press itself rather
  than only after the transcript finishes. The reveal is non-activating
  (`reveal_temporarily`), so focus stays on the target window and the pending
  insertion is unaffected. `WM_POWERBROADCAST` resume events also restore
  overlay visibility and refresh all global hotkey registrations after
  display/session state has stabilized.
- **App icon**: `src/stt_app/assets/app_icon.ico`/`.png` are generated by
  `scripts/generate_app_icon.py` and committed. `app_icon.py` is the single
  loader; the icon is wired into the Qt app/tray icons and the Settings and
  History dialog windows (with a standard-icon fallback), the wheel, the
  PyInstaller bundle/EXE, and the Inno Setup installer. Rerun the script only
  when the design changes.
- **Show-overlay hotkey (preset, clearable)**: `show_overlay_hotkey`
  (default `Ctrl+Alt+F11`, schema 21) registers a third global hotkey
  (`DEFAULT_SHOW_OVERLAY_HOTKEY_ID`) whose only action is
  `controller.bring_overlay_to_front` — the same reveal as the tray "Show
  overlay" action, e.g. to check the last transcript on a floating overlay.
  Optional hotkeys use `_normalize_optional_hotkey`: an empty stored value is
  a deliberate disable and must stay empty (saving never substitutes the
  default combo back); only invalid non-empty values fall back to the
  default. Schema-20 files briefly stored "" for "never configured", so a
  `< 21` empty value migrates to the default once. The Save flow validates
  the combo and rejects conflicts with the recording and cancel hotkeys;
  registration mirrors the cancel hotkey (disabled -> unregister, failure ->
  notice + Error idle state) and is included in the resume-path
  `refresh_hotkey_registration`.
- **The tray icon is registered by hand on Windows (`win_tray_icon.py`)**:
  `QSystemTrayIcon`'s menu closed Windows 11's "hidden icons" flyout while
  other apps in the same flyout kept it open. Everything observable at menu
  time was measured and refuted — Qt's `SetForegroundWindow`, the menu taking
  the foreground (the reference app's does too), window styles and owners
  (identical), and activating our icon window first (`accepted=True`, flyout
  still closed). Two experiments then isolated it: a hand-registered icon keeps
  the flyout open, and of two such icons differing only in their menu, only the
  one with a native `TrackPopupMenu` does. **Both the registration and the menu
  must be native**; do not "simplify" either half back to Qt.
  `WindowsTrayIcon` mirrors the `QSystemTrayIcon` API this app uses
  (`activated` with the same `ActivationReason` values, `showMessage`, `show`,
  `setContextMenu`, `setToolTip`), so callers do not branch, and
  `create_tray_icon` falls back to `QSystemTrayIcon` on other platforms and on
  any Win32 failure. The context menu stays a `QMenu` — it is the model
  (labels, order, enabled state, callbacks) and is only *rendered* natively.
  Menu width is `longest label + 70 px`: that 70 px is Windows' own padding and
  is constant for any content (measured across five label sets), so the labels
  are the only lever — `MNS_NOCHECK` reclaims the check-mark column while no
  entry is checkable (233 -> 205 px), and dropping the redundant "last" from
  three labels took it to 184 px. Entries report their checkable state, so
  adding a checkable action brings the column back instead of losing its check
  mark. Anything narrower would need owner-drawn items, which means drawing
  hover/disabled/dark-mode states by hand — not worth it.
  Invariants that Qt used to provide and that this module must keep: the window
  class is registered once per process with a dispatcher that routes by HWND (a
  per-instance window procedure dangles as soon as one instance is collected),
  every `ctypes` call declares `argtypes`/`restype` (defaults truncate handles
  and overflow on a large `LPARAM`), the icon is re-added on `TaskbarCreated`
  after an Explorer restart, and it is deleted before its window is destroyed
  or a dead icon lingers in the tray.
  **Re-adding it is a retry, not a call.** Explorer broadcasts
  `TaskbarCreated` before it will accept icons, so the re-add right after a
  restart routinely fails once -- and `NIM_ADD` failing raised out of
  `show()`, whose only caller is `main` before `app.exec()`, so a logon race
  ended the process: a dictation app with no window died because a
  notification icon was a few hundred milliseconds early. `show()` therefore
  never raises; `_attempt_add` schedules `_ADD_RETRY_DELAYS_MS` retries and
  logs. Three pieces are load-bearing:
  - **`_wanted_visible`, not `_visible`, is what the retry and the
    `TaskbarCreated` arm read.** `_visible` is what the shell has accepted, so
    keying the restart arm on it meant a restart arriving while a re-add was
    still failing saw a hidden icon and did nothing -- the icon was gone for
    the session, and because `showMessage` returns early on `_visible`, every
    later tray notification was dropped with it.
  - **A retry carries its generation.** `hide()` then `show()` while one is
    pending leaves `_wanted_visible` true again by the time the stale retry
    wakes, and `_attempt_add` does not look at `_visible`, so without the
    check it calls `NIM_ADD` a second time for an icon the shell already has
    -- two retry chains, and a second add Windows rejects. Bumping the
    generation in `hide()` as well was tried and removed: mutation cannot tell
    it apart, because `show()` bumps it and `_wanted_visible` covers the rest.
  - **`SetIconVersionError` is separate from a failed add.** `NIM_ADD`
    succeeded there, so retrying would add a second icon; the icon is kept and
    the failure reported. Without version 4 the shell sends legacy button
    messages while `_handle_message` decodes lParam the version-4 way.
  Also: `RegisterWindowMessageW` returns 0 on failure and 0 is `WM_NULL`, so
  the result is stored as `None` rather than unchecked -- otherwise every
  `WM_NULL` this window received would look like an Explorer restart. And a
  notification that cannot be shown is logged: silence there made a failed
  background transcription indistinguishable from one that never ran, which is
  the case those notifications exist for. `scripts/diagnose_tray_flyout.py` and
  `scripts/experiment_native_tray_icon.py` reproduce the measurements.
- **Tray left-click reveals the overlay**: a single left click (`Trigger`) has
  no other meaning and there is no main window, so it calls
  `controller.bring_overlay_to_front`. Together with the overlay's Record
  button this is the keyboard-free path to dictation; double-click still opens
  Settings.
- **Tray middle-click toggle (`tray_middle_click_toggle`, default on)**:
  middle-clicking the tray icon calls `controller.toggle_recording`, exactly
  like the recording hotkey; double-click keeps opening Settings. The guard
  reads `controller.settings` at click time so the Display-tab checkbox takes
  effect without restart.
- **Windows taskbar identity**: `main._set_windows_app_user_model_id` sets an
  explicit `APP_USER_MODEL_ID` before the first window is created. Without it
  Windows groups our windows under the host process (python.exe) and shows its
  generic icon on the taskbar (most visibly for the Settings dialog). Keep the
  ID stable so taskbar pinning/grouping is consistent.
- **`window_focus` and `hotkey` take their own `WinDLL` handle, and the handle
  is opened `use_last_error=True`, which changes where the error goes.**
  `ctypes.windll.user32` is a process-wide cached object, so declaring
  `argtypes`/`restype` on it redefines those functions for every other caller;
  `text_inserter` and `win_tray_icon` already each take their own. What the
  declarations rule out is an `HWND` at or above 0x8000_0000 coming back
  negative from the default 32-bit signed `restype` and being passed back
  sign-extended -- a different window -- and `GetAsyncKeyState` being read as
  an `int` when it returns a `SHORT`. Measured on this machine a real `HWND`
  is 0x30766 and the declared and undeclared calls agree exactly, so *for
  `window_focus`* this is hardening rather than a fix for anything observed.

  **"Hardening, not a fix for anything observed" was wrong as a statement
  about the change as a whole**, and this entry said it for one round. The
  same switch broke `hotkey`'s error reporting: `use_last_error=True` saves
  the Windows error into ctypes' own per-call slot and *restores* the thread's
  `GetLastError` to its previous value, so `Win32HotkeyApi.get_last_error`'s
  `ctypes.GetLastError()` answered 0 and every failed registration reported
  "Unknown Windows hotkey registration error" instead of naming the cause.
  Measured against a real double `RegisterHotKey`: thread reader 0, ctypes
  slot 1409 -- "another program holds this combination", the code the fallback
  and reclaim machinery exists for. Read `ctypes.get_last_error()` on any
  handle opened this way; the other two modules already did.

  **A 64-bit handle fails outright only on the *undeclared* call.** Because
  `wintypes.HWND` *is* `c_void_p`, declaring it removes that check rather than
  keeping it: measured with 0x7FF8_1234_5678, the undeclared call raises
  `ArgumentError: int too long to convert` and the declared one accepts it and
  returns 0. That is the correct direction -- the overflow was ctypes refusing
  a legal handle, not a safety net -- but the earlier wording had it backwards.
  Windows does not produce such a handle today either way.
