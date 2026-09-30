# Persistence, secrets and history: design decisions

Binding rules, condensed from the entries moved out of `AGENTS.md` on
2026-09-30; history and full measurements are in `docs/learning-log.md` and
git history. Read before changing `persistence.py`, the JSON stores,
`secret_store.py`, transcript history or history audio. "Above/below" refers
to this file; "Known limitations" is `docs/agents/known-limitations.md`.

- **`recordings_max_count` 0 means unlimited**
  (`RECORDINGS_MAX_COUNT_UNLIMITED`, ceiling `RECORDINGS_MAX_COUNT_CEILING`,
  default 10); no schema bump, since older `from_dict` clamped to >= 1. A
  negative value falls back to the default, never to unlimited. **Integer
  settings read through `_exact_int_or_none`**, which refuses bools and
  non-integral floats (NaN/inf too) before `_int_or_none`, because `int()`
  truncates `-0.5`/`0.9`/`False` to 0; for `history_max_items`, `True` once
  meant limit 1 and deleted all but one transcript.
- **A store's existence check covers the backup too**: a bare exists-check on
  the primary made a deleted primary read as empty and the next write destroy
  the `.bak` written by `atomic_write_json(keep_backup=True)` and read by
  `load_json_with_backup`. With neither file present, return the empty
  default without quarantining; a store recovering from the backup
  republishes the primary. `tests/test_store_backup_recovery.py`.
- **A store file that cannot be opened is neither damage nor empty and is
  never written over** (2026-09-16; a scanner-held file used to load empty
  and be overwritten):
  - **Three loader states.** `"missing"`: nothing there, or unusable content
    (start empty, quarantine). `SOURCE_UNREADABLE`: the read raised; move and
    write nothing, log `store_unreadable path=` once per load, answer the
    default in memory (`SettingsStore.load` does not save it).
    `SOURCE_BACKUP_PRIMARY_UNREADABLE`: answer the backup, but no republish
    and no history quarantine (the primary may be newer). Non-UTF-8 is damage.
  - **Every read-modify-write refuses while the last read failed**:
    `_last_read_unreadable` (set by `load()`), checked by
    `_refuse_when_unreadable()` under the path lock, raises
    `persistence.StoreUnavailableError` (an `OSError`, names the file).
    `SettingsStore.save` probes itself (`refuse_to_overwrite_unreadable`),
    since the overlay setters save through stores that never loaded;
    `TranscriptHistoryStore.export_to_file` refuses after its load;
    `LastRecordingStore.mark_completed` must not fall back to the
    orphaned-audio state (`keep_after_success=False` deleted kept audio).
  - **Thirteen Qt call sites report the refusal with `str(exc)` in their own
    presentation**: `_paint_status_keeping_offer` (Edit; via
    `show_overlay_error` for the opacity slider, pin button and Lang menu),
    the History dialog box, the Settings History and Benchmark status lines,
    and `history_ui_actions`' import/clear/export boxes. PySide6 prints slot
    exceptions to a stderr a windowed build lacks, so unguarded means a dead
    button. Any UI action that asks `count()` and then writes has this shape.
  Tests use the `files_no_read_gets_past` fixture (`Path.open` raises
  `PermissionError`; `exists()` unpatched). Verified 2026-09-18 with real
  share-mode-0 `CreateFileW` locks (23 checks).
- **Deleting a store's primary deletes its backup first**, or `load`
  republishes it: `LastRecordingStore.clear()` brought back a state pointing
  at a deleted WAV (`_orphaned_audio_state()` unreachable). An undeletable
  backup refuses the clear.
- **`quarantine_corrupt_file(include_backup=True)` only for a backup known
  unusable** (the `payload is None` arm); a parsed payload of the wrong shape
  quarantines only its `source` file (`ProviderConnectionTestStore` lost both).
- **JSON read-modify-write is path-serialized** through
  `persistence.lock_for_path`, the single in-process lock registry every store
  reuses. Keep writes atomic.
- **Non-UTF-8 must reach recovery**: it raises `UnicodeDecodeError`, not
  `JSONDecodeError`, and killed the app at startup. `persistence` and
  `local_model_scan` catch `(OSError, ValueError)`;
  `transcript_history.import_from_file` has a user-facing arm.
- **An unreadable insecure key file is not empty**: `_load_insecure_payload`
  returns `(payload, damaged)`; `_set_insecure_api_key` and
  `delete_api_key(provider, strict=True)` raise `RuntimeError` naming the path
  rather than overwrite every other key. The stale-copy cleanup after a
  successful keyring write stays tolerant.
- **A refused keyring write is never spilled into the fallback while the
  keyring still holds another key**, which `get_api_key` would keep using.
  After `set_api_key`'s keyring write raises,
  `_refuse_a_fallback_the_keyring_would_shadow` re-reads the primary and each
  legacy service name and raises `RuntimeError` for any value other than
  `None` or the new key; a read that raises proceeds (no evidence). Read
  precedence (`get_api_key`, `get_api_key_source`) and `delete_api_key`
  unchanged. The dialog keeps the typed value
  and appends the message. **A blank keyring answer is nothing stored**:
  `_get_keyring_value` answers `None` for "", so `has_api_key` and the guard
  treat it like a failed read.
- **History retention**: default 500 (legacy 20 migrates up). A transcript is
  saved before insertion; the model name comes from the job's settings
  snapshot. Both views multi-select copy/delete; edit is single-entry. Limit
  spin boxes disable keyboard tracking (no trim prompt at an intermediate
  `3`). Re-clicking History re-presents the dialog with `reload(force=True)`,
  keeping selection and scroll; never a second dialog.
- **History export/import/clear exists once, in `history_ui_actions.py`**,
  for both views (incl. "import only free slots" vs "import all and set
  unlimited"). The Settings tab persists a switch to unlimited at once (via
  `_settings_store` and `dataclasses.replace` on `_loaded_settings`), so Save
  sees no phantom change.
- **History audio linkage is shared; the retranscribe paths differ on
  purpose.** `history_audio.py` resolves audio (`source_audio_path`, else the
  managed last recording only while it describes that entry) and
  reveals/opens it; `app_paths.resolve_recordings_dir` is the one
  recordings-dir rule. `retranscribe_dialog.py` preselects the entry's engine
  and model and the *configured* language (entries store none), all
  changeable; the language note (`_LANGUAGE_SUBSTITUTION_NOTE`, also the
  height reservation's worst case) says when any model lacks the previous
  language, plus Canary's translation warning. Pickers follow
  `config.language_modes_for_selection`; shared helpers
  `settings_dialog_helpers.model_choices_for_engine`, `local_model_label`,
  `settings_store.apply_engine_model_selection`. Settings > History >
  Retranscribe... prefills Import Audio instead. Both write a new entry.
- **Ctrl+C in either history view copies the whole selection** (explicit
  `QKeySequence.Copy` shortcut; the default copies one cell).
- **Nothing that only reads may call `appdata_root`** (it creates the folder
  and migrates a legacy `tts_app` install). Use `existing_appdata_root` /
  `existing_settings_path`. `SettingsStore` is the one legitimate caller.
- **`file_lock` writes `_held_key` before registering in `_HELD_RESOURCES`,
  and every exit goes through `_close_handle`** (the only remover, also from
  `release()`'s early return); the reverse order stranded the key, so every
  `acquire` raised `LockHeldInThisProcess` until restart.
