# Persistence, secrets and history: design decisions

Binding project rules, moved verbatim from `AGENTS.md` on 2026-09-30 so that
the always-loaded agent file stays small. Read this file before changing
`persistence.py`, the JSON stores, `secret_store.py`, transcript history and history audio. Entries keep their original order, so "the entry above/below" still
refers to this file; "Known limitations" is `docs/agents/known-limitations.md`.
History is in `docs/learning-log.md`.

- **`recordings_max_count` 0 means unlimited**: the retention cap was 500 with
  no way to keep everything, so the decision was the app's rather than the
  user's. 0 now prunes nothing (`RECORDINGS_MAX_COUNT_UNLIMITED`), the ceiling
  is `RECORDINGS_MAX_COUNT_CEILING`, and the default stays 10. **No schema
  bump**: every earlier version clamped this field to >= 1 when reading it
  (`from_dict`) and the spin box could not go below 1, so no older app ever
  held a 0 to write and a stored 0 cannot come from one. A *negative* value falls back to the
  default instead of clamping to 0 -- the spin box cannot produce one, so it is
  garbage, and reading "unlimited" into garbage would switch pruning off
  silently. Note what the old clamp did with 0: `max(1, int(keep_count or 1))`
  made it "keep exactly one", the most destructive value in the range.
  **`int()` truncates toward zero before the sign check**, which walked
  around that negative branch entirely: `-0.5`, `0.9` and `False` in
  `settings.json` all became 0 -- "keep every recording" -- from values the
  app never writes and the spin box cannot produce. `_exact_int_or_none`
  refuses a bool, refuses a non-integral float (`is_integer()` answers
  False for NaN and both infinities, so one test covers the non-finite
  cases too) and otherwise delegates to `_int_or_none`; integral floats and
  numeric strings keep working. `True` is refused with `False`: `int(True)`
  is a legal count of 1, which would make a boolean in the file look like a
  setting the user chose. `history_max_items` reads through the same
  helper, and there the boolean *was* the destructive value: `True` parsed
  to a limit of 1, and one dictation at that limit deleted every transcript
  but the newest (measured: 5 lost from a hand-edited file).
- **A store's existence check covers the backup too, not just the primary.**
  `atomic_write_json(keep_backup=True)` writes a `.bak` beside every store and
  `load_json_with_backup` reads it when the primary will not parse -- but five
  stores opened their load with a bare `if not path.exists(): return <empty>`,
  which the backup never got past. So a *deleted* primary read as "nothing
  saved yet", and the next write put that emptiness over the backup as well.
  Measured on the transcript history: five entries, delete
  `transcript_history.json`, load returns 0, one more dictation leaves the
  backup holding 1. `settings_store` was worse -- a missing primary writes
  defaults *and* `save` refreshes the `.bak` in the same call, so every setting
  was reset and the last copy destroyed together. The primary goes missing for
  ordinary reasons (an antivirus quarantine, a sync client, a user tidying
  `%APPDATA%`), which is precisely what the backup is for. The guard had to
  *widen*, not disappear: with neither file present there is nothing to
  recover and the store must still return its empty default without
  quarantining anything. Every store that recovers from the backup also
  republishes the primary, so a second loss cannot take the data.
  `tests/test_store_backup_recovery.py` holds all three properties for all
  five stores.
- **A store file that cannot be opened is neither damage nor empty, and is
  never written over** (2026-09-16, F10 of the external review). Every
  store is read-modify-write, and `load_json_with_backup` answered
  "missing" for a read that raised `OSError` exactly as for a file that is
  not there -- so a primary held by an antivirus scan, a backup tool or a
  sync client loaded as the empty default, the next write put that default
  plus one entry over the user's data, and the transcript history
  quarantined both files first (measured: five entries, both files held
  open, one appended dictation, the five survived only as `.corrupt.*`
  copies). Three rules:
  - **The loader separates three states.** `"missing"` is no candidate
    at all, or candidates that opened and were unusable -- nothing saved
    yet, or damage, answered by starting empty and quarantining.
    `SOURCE_UNREADABLE` is a candidate that is there and whose read
    raised: its content is unknown, so nothing is moved and nothing is
    written; the store logs `store_unreadable path=` once per load and
    answers its empty default in memory (`SettingsStore.load` returns
    defaults so the app starts, and no longer reaches the branch that
    saves them). `SOURCE_BACKUP_PRIMARY_UNREADABLE` is the backup parsing
    beside a primary that raised: the backup's data is answered, and the
    republish and the transcript history's quarantine are skipped, because
    the two copies were never compared and the primary moved aside may be
    the newer one. A non-UTF-8 read stays damage.
  - **Every read-modify-write refuses while the last read never reached
    the file** (`_last_read_unreadable`, written by `load()` and read by
    `_refuse_when_unreadable()` under the store's path lock, which every
    such method holds across the two), raising
    `persistence.StoreUnavailableError` -- an `OSError` subclass, so every
    caller already guarding a failing disk catches it unchanged -- whose
    text names the file and says to try again in a moment.
    `SettingsStore.save` probes the file itself
    (`refuse_to_overwrite_unreadable`): the dialog merges its edits onto a
    fresh `load()`, and the overlay's opacity slider and pin button save
    through stores that never loaded. `TranscriptHistoryStore.export_to_file`
    refuses after its load, because the destination is a file the user
    picked, routinely a previous export, and an unreadable history
    replaced it with `[]` under "Exported 0 entries".
    `LastRecordingStore.mark_completed` no longer falls back to the
    orphaned-audio state, whose `keep_after_success` is False and which
    deleted a recording the state file had asked to keep.
  - **Thirteen Qt call sites report the refusal through the presentation
    they already use** -- the controller's `_paint_status_keeping_offer`
    (for Edit, and through `show_overlay_error` for the overlay's opacity
    slider, pin button and Lang menu, whose setters save straight to the
    store and caught the refusal with a bare `except Exception` that only
    logged it until wave 11: the session kept the new value, the file the
    old one, and which was real showed at the next start; while a
    recording or a transcription owns the overlay the report is a tray
    notification instead -- the `show_overlay_error` entry), the History
    dialog's warning box, the Settings History tab's status label, the
    Benchmark tab's status line, the import and clear boxes of
    `history_ui_actions` (its export box caught the refusal already, through
    the `except Exception` it had) -- with `str(exc)` verbatim. PySide6 does not
    propagate an exception from a slot invoked from C++: it prints the
    traceback to stderr and returns, and a windowed build has no stderr,
    so an unguarded refusal was a button that did nothing, twice in a row.
    The two `clear()` sites became reachable only with the first rule's
    third state, because a locked primary beside a readable backup now
    answers a non-zero count; any UI action that asks a store `count()`
    and then writes has that shape.
  `tests/test_store_backup_recovery.py` drives all six stores through the
  shared `files_no_read_gets_past` fixture (`Path.open` raising
  `PermissionError` for the named files; `exists()` is a `stat` and is
  deliberately not patched). **Measured with a real lock on 2026-09-18**:
  `CreateFileW` with share mode 0 -- the way an antivirus scan or a backup
  tool holds a file -- on the transcript history, `settings.json` and the
  last-recording state, 23 checks: the second open really fails, reads
  answer the in-memory default, every write raises
  `StoreUnavailableError`, nothing is moved aside or added, and every byte
  is unchanged once the lock is gone. Not measured: a named antivirus
  product.
- **Deleting a store's primary means deleting its backup, in that order.**
  The recovery above is exactly what makes a half-deletion permanent:
  `LastRecordingStore.clear()` unlinked the state file and left the `.bak`,
  `load` reads the backup precisely when the primary is missing, and it
  republishes what it finds -- so the cleared state came back pointing at a
  WAV that had been deleted a moment earlier, and `_orphaned_audio_state()`
  became unreachable. Measured: `clear()` returned True, the audio was gone,
  and the next `load()` returned the same `recording_id` with the primary
  rewritten. The backup goes first, so a failure there leaves the pair intact
  rather than a primary with no recovery copy, and a backup that cannot be
  removed refuses the clear the same way a state file that cannot be removed
  already did.
- **`quarantine_corrupt_file(include_backup=True)` is only for a backup
  already known to be unusable**, which is what its own docstring says and
  what the `payload is None` arm means. `ProviderConnectionTestStore` passed
  it for a payload that *parsed* but whose `results` key was not an object --
  a shape only external damage produces, i.e. exactly when the backup is the
  good copy. Measured: both files moved aside and every later load returned
  nothing. Quarantine the file the payload actually came from: `source` is
  what tells the two apart, and without it a bad `.bak` behind a missing
  primary makes every load fail identically forever. The two history stores
  already quarantined just the primary in their equivalent arm.
- **Persistent JSON read-modify-write operations are path-serialized**:
  `persistence.lock_for_path` is the single in-process lock registry. Stores for
  history, benchmarks, settings, provider diagnostics, local inventory, last
  recording, and insecure keys reuse it so separate store instances cannot
  overwrite each other's concurrent updates. Keep writes atomic as well.
- **A stored file that is not UTF-8 must reach the recovery path**:
  `Path.read_text(encoding="utf-8")` raises `UnicodeDecodeError`, not
  `json.JSONDecodeError`. Both are `ValueError`s and only the JSON one was
  named, so a single non-UTF-8 byte in settings, history or the inventory
  cache escaped the store constructor and the app died before its first
  window -- with the recovery path that exists for exactly this sitting
  unused, because the damage was of the wrong kind. `persistence` and
  `local_model_scan` catch `(OSError, ValueError)` and say why both members
  are meant; `transcript_history.import_from_file` has its own arm with a
  user-facing message, since there the file is one the user chose.
- **An unreadable insecure key file is not an empty one**: the fallback store
  read its JSON through one `try/except` returning `{}` for every failure, so
  a permission error, a lock held by a backup tool or a truncated write looked
  exactly like "no file yet" -- and the next `set_api_key` wrote that `{}`
  plus one key back, silently deleting every other provider's key while the UI
  reported success. `_load_insecure_payload` returns `(payload, damaged)`;
  `_set_insecure_api_key` and `delete_api_key(provider, strict=True)` raise a
  `RuntimeError` naming the path instead of overwriting. The stale-copy
  cleanup after a successful keyring write stays tolerant on purpose --
  failing there would undo a save that already succeeded.
- **A refused keyring write over a key the keyring still holds is refused,
  never spilled into the fallback** (F11 of the 2026-09-12 review).
  `set_api_key` fell through to the insecure fallback file whenever
  `keyring.set_password` raised and the fallback was enabled -- also for a
  keyring that reads fine and only refuses writes (a locked vault, a
  policy, a transient backend error). `get_api_key` and
  `get_api_key_source` read the keyring before the file, so the new key B
  sat unused in plaintext while every request kept using the old key A,
  the dialog said "API key storage updated", the API Keys tab said
  "keyring", and nothing reconciled the two when the keyring recovered
  (measured with a backend whose read and write fail independently: no
  exception, keyring A, fallback B, `get_api_key -> A`).
  `_refuse_a_fallback_the_keyring_would_shadow` re-reads the keyring after
  a refused write -- the primary service name and then each legacy name,
  in the order `get_api_key` reads them, because a legacy-only key shadows
  the file exactly as the primary does and a keyring that refuses writes
  also refuses the migration a successful read would perform -- and raises
  `RuntimeError` naming the provider without writing anything while the
  keyring still answers a value that is neither `None` nor the new key.
  Three answers proceed to the fallback write as before: `None` (nothing
  can shadow the copy), the new key itself (the write landed before the
  backend raised), and a read that raises (no evidence of an old key, and
  every reader treats it as nothing stored, so the copy is what they will
  all return -- refusing here would block every save on the machine the
  fallback exists for). The read precedence is unchanged: the keyring
  stays authoritative, and `delete_api_key` is untouched. The settings
  dialog's failure arm keeps the typed value and appends the message to
  the key-storage status line; that line's generic advice no longer says
  "Enable insecure fallback storage" when the checkbox is already on.
  **A blank keyring answer is nothing stored** (wave 11): `get_password`
  may answer "" for a credential with an empty blob, and read as a value
  it was returned by `get_api_key` without a look at the fallback file,
  counted by `has_api_key`, reported as "keyring", and taken by the guard
  for a previous key -- the fallback write was refused and the new key
  stored nowhere, under an error claiming the keyring still held the old
  one. `_get_keyring_value` answers `None` for it, as for a read that
  raised, so every reader and the guard treat the two alike.
- **Transcript history retention**: history defaults to 500 saved entries, and
  legacy settings that still have the old 20-entry default are migrated upward.
  Successful transcriptions are added to history before text insertion, so a
  paste/focus failure does not drop the transcript. The stored model name comes
  from the transcription settings snapshot, not from later UI changes.
  Settings History and the overlay History dialog both support multi-select
  copy/delete for bulk cleanup; editing remains single-entry only. History-limit
  spin boxes disable keyboard tracking: typed intermediate values are not
  applied until the edit is committed, so increasing a limit such as `224` to
  `300` never prompts to trim at the temporary `3` value. Re-clicking History
  while the dialog is open re-presents the existing window and refreshes it
  once via `reload(force=True)` (selection and scroll position are preserved);
  it must not create another dialog.
- **History export/import/clear parity**: the standalone History dialog and the
  Settings History tab share the same export, import (including the overflow
  choice between "import only free slots" and "import all and set unlimited"),
  and clear flows via `history_ui_actions.py`, so the logic exists exactly once.
  Only feedback presentation (popup vs. inline status label) and how the active
  limit is read/persisted differ per caller. The Settings tab persists a
  switch-to-unlimited decision immediately (via `_settings_store` plus
  `dataclasses.replace` on `_loaded_settings`), the same way the dialog does,
  so a later Save does not see it as a phantom change.
- **History audio linkage is shared, and the two retranscribe paths differ on
  purpose**: `history_audio.py` owns resolving an entry's retained audio
  (stored `source_audio_path` first, then the managed last recording only
  while it still describes that exact entry) plus the file-manager
  reveal/open calls; both history views use it, and
  `app_paths.resolve_recordings_dir` is the single "configured dir else
  default" rule. The overlay's "Recent Transcriptions" dialog offers
  Retranscribe/Show audio file per entry (buttons plus a right-click menu)
  and a Recordings-folder shortcut. `retranscribe_dialog.py` preselects the
  entry's own engine and model and the *configured* language -- history
  entries store no language -- and repeating a run with a corrected language
  is the case it exists for, so all three stay changeable and a quick "try
  the bigger model on this one" needs no detour through Settings. The note
  under the language picker says when the chosen model does not offer the
  language selected before it (`_LANGUAGE_SUBSTITUTION_NOTE`, one literal
  shared with the height reservation's worst case); it used to say so for
  Canary alone, while Cohere and Granite, which refuse `auto` as well, and a
  remote provider lacking the configured language substituted silently.
  Canary additionally warns that a wrong language yields a translation.
  The dialog is resizable (long transcripts) and its pickers are dependent:
  changing the engine repopulates the models (restoring the entry's model when
  the user returns to its engine) and the language list follows both via
  `config.language_modes_for_selection`. It duplicates none of the Import Audio
  tab's machinery: `settings_dialog_helpers.model_choices_for_engine` and
  `local_model_label` are the shared source for every model picker, and
  `settings_store.apply_engine_model_selection` is the one place that maps an
  engine/model pair onto the engine's own model field. Settings > History >
  Retranscribe... still prefills the Import Audio tab, which additionally
  offers credential checks and progress for one-off external files. Both paths
  write a new history entry and never modify the original.
- **Ctrl+C in either history view copies the whole selection**: both the
  standalone History dialog and the Settings History tab install an explicit
  `QKeySequence.Copy` shortcut on their list/table bound to the same handler as
  "Copy selected". Without it the view's own handling yields only the current
  cell, so a three-row selection silently produced one entry in the clipboard.
- **Nothing that only reads may call `appdata_root`**: it creates the data
  folder and renames a legacy `tts_app` install onto the current name, so a
  path *lookup* migrated a user's settings, history and recordings. Use
  `existing_appdata_root` / `existing_settings_path`, which return `None`
  rather than a path that does not exist yet. `SettingsStore` is the one
  legitimate caller -- it is about to write there.
- **`file_lock` writes `_held_key` before registering the key, and every exit
  goes through `_close_handle`.** `_close_handle` is the only place that takes
  a key back out of `_HELD_RESOURCES`, and it finds it through `_held_key` --
  so an exception landing between the registration and the assignment left the
  key registered with nothing able to remove it, and every later `acquire` for
  that resource in this process raised `LockHeldInThisProcess`. In practice:
  no download can start again until the app restarts. The reverse order is
  harmless, because `discard` does not care about a key that was never added.
  `release()`'s early return for a handle that is already gone calls
  `_close_handle` for the same reason.
