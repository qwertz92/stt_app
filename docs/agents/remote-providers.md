# Remote providers: design decisions

Binding project rules, condensed from the entries moved out of `AGENTS.md` on
2026-09-30. Read this file before changing `transcriber/*_provider.py`,
remote batch splitting and provider error handling. Entries keep their order,
so "the entry above/below" refers to this file; "Known limitations" is
`docs/agents/known-limitations.md`. Measurements, vendor quotations and
history are in `docs/learning-log.md` and git history.

- **AssemblyAI batch model**: send `speech_models`; `universal-3-5-pro` goes
  alone, never with a silent `universal-2` fallback. Legacy
  `universal-3-pro`/`best`/`nano` migrate to the default and are hidden.
- **No remote batch wait may be unbounded; the AssemblyAI poll fetches a status
  and never calls something that waits.** `Transcript.wait_for_completion` is
  an unbounded loop and `Transcript.get_by_id` (SDK 0.64.33) calls it (still
  blocked after 12 s on a 3 s budget). A stuck poll holds the single
  `max_workers=1` worker and blocks exit: `ThreadPoolExecutor`'s exit handler
  (`threading._register_atexit`) joins started workers even after
  `shutdown(wait=False, cancel_futures=True)`, and the process keeps the
  single-instance lock. `_fetch_transcript` = `api.get_transcript(http_client,
  id)` + `Transcript.from_response`; `_wait_for_transcript` loops against
  `ASSEMBLYAI_BATCH_MAX_WAIT_S`. Terminal status is the *positive* test; a
  submit without id fails at once; the poll ignores the cancel check (a
  finished transcription is never discarded). Each request is bounded by the
  SDK's `settings.http_timeout` (30 s). Tests pin the shape (installed SDK
  with a stub raising on a second poll; a fake poisoning `get_by_id`).
- **Fun-ASR receive loop** (`funasr_provider.py`):
  - **An empty-text final ends the sentence with the pending partial**
    (`result-generated`, `sentence_end`, empty `text` used to drop it and
    succeed truncated); a final with text replaces the partials it refines.
  - **`_UnusableFrameBudget` bounds unusable frames per transcript** (not per
    `_recv_event` call, which `_collect_transcript` makes per event):
    `_MAX_UNUSABLE_FRAMES` in a row fail the request with the text so far.
    Spent only on a frame classified unusable (non-JSON-object in
    `_recv_event`; an object that is not one of the loop's events, incl.
    empty and unknown); reset only by a `result-generated` that carries
    stripped text or ends a sentence that has one, and by a heartbeat (else a
    flooding peer pins a core for the 30-min budget).
  - **Total frames per request are capped** (`_MAX_FRAMES_PER_REQUEST`,
    1,000,000, `_UnusableFrameBudget.received`), since any event resets the
    run count; a 30-min recording is ~20,000 frames. The `task-started` wait
    and connection test have their own budgets.
  - **The transcript is capped** (`_MAX_TRANSCRIPT_CHARS`, 2,000,000, ~40x a
    plausible 30-min maximum) as a running total including join separators
    (also before a pending partial); the failure carries the recovered text.
  - `task-failed` detail: 300 chars, via `_http_utils.nested_error_text`. A
    non-string `text` is ""; `sentence_end` counts only if `True`. The loop
    ends on `transcription_shutdown_requested()`.
  - **A `result-generated` with `sentence.heartbeat` `True` is skipped**
    (`_is_heartbeat`, per the Paraformer server-events page), else its
    `sentence_end` closes the partial early and duplicates the final. The
    run-task sends `heartbeat: true` (documented Paraformer-v2-only; the
    Fun-ASR page recommends it); live behaviour is **unverified** (a
    rejection would be `task-failed` on every request). The upload is unpaced
    (`send_binary` loop).
  - Duplicate finals are not de-duplicated (a user may repeat a sentence).
- **One failed AssemblyAI status fetch does not abort the wait**: up to
  `ASSEMBLYAI_MAX_CONSECUTIVE_FETCH_FAILURES` (3) consecutive failures one
  interval apart, reset on success; the error names the transcript id (the
  way to recover the job). A fetched object without `status` is a fetch
  failure. `_configure()` and `_get_aai()` sit inside `transcribe_batch`'s
  `try`. Fakes cap *fetches*, not just sleeps (else a non-sleeping loop hangs
  pytest); `FakeStreamingClient.on` appends handlers like the SDK.
- **The poll ends when the app quits; its bound is
  `ASSEMBLYAI_BATCH_MAX_WAIT_S` plus one `http_timeout` (30 s).**
  `transcriber/base.py` owns the app-wide `_SHUTDOWN` event
  (`request_transcription_shutdown`, first statement of
  `DictationController.shutdown`), read at the loop top and in
  `_sleep_between_fetches` (`ASSEMBLYAI_SHUTDOWN_POLL_S` slices); Fun-ASR
  reads it too. Once per process, never cleared in `src/`;
  `tests/conftest.py`'s `_reset_the_transcription_shutdown_flag` resets it
  per test (without it: 26 full-suite-only failures).
- **A nested provider error object is unwrapped, never `str()`-ed.**
  `nested_error_text` (`message`, `detail`, `status`, `code`, else JSON via
  `json.dumps(..., ensure_ascii=False)`; numbers/lists fall back to JSON) is
  shared by `read_http_error_detail` and Fun-ASR's `_failure_detail`, which
  tries the message, then `error_code`, then the dump (a blank message hid
  `Throttling.RateQuota`). Uncapped on purpose: HTTP readers cap at 300,
  Fun-ASR at `_FAILURE_DETAIL_MAX_CHARS`. `read_http_error_detail` is **not
  idempotent** (stream body): call it once per `HTTPError`.
- **Never build a provider error from `HTTPError.reason`** (status phrase
  only). `_http_utils.read_http_error_detail` / `http_error_suffix` is the one
  reader for all five REST providers: capped at 300 chars, status phrase when
  the body is empty or unreadable.
- **Every REST call passes `create_ssl_context()`**: `urllib` ignores
  `REQUESTS_CA_BUNDLE`, so AssemblyAI's `test_connection` failed behind a TLS
  proxy while the SDK worked. `format_ssl_error_message` is the shared text
  (names `SSL_CERT_FILE` too).
- **ElevenLabs**: `scribe_v2` only; `scribe_v1` (removed 2026-07-09) migrates
  and is never sent.
- **Azure LLM Speech roster = Microsoft's table; an unconfigured Azure engine
  moves to the default once (schema 24, 2026-09-19).** One engine (no MAI API
  outside Azure): `mai-transcribe-2` (default, 60 languages),
  `mai-transcribe-1.5` (43), `mai-transcribe-1` (selectable, "Deprecated on
  Aug 20, 2026", labelled).
  - Settings store the lower-case id; `AZURE_API_MODEL_NAMES` holds the
    documented spelling that is sent (e.g. `MAI-Transcribe-2`).
  - Languages follow that table via `AZURE_LOCALE_OVERRIDES` (`no` -> `nb`,
    `tl` -> `fil`); a test compares the *sent* codes per model both ways.
  - Schema 24: a pre-24 file without `azure_endpoint` adopts the default;
    with one it keeps its model (MAI-2 is $0.10/h vs $0.36/h).
  - **Not verified live** (`docs/azure-llm-speech.md` says so). No phrase
    list is sent, so custom vocabulary stays unwired.
- **OpenAI: `gpt-transcribe` is the default and the only model with a
  different request shape (schema 25, 2026-09-21).** Offered with
  `whisper-1`, `gpt-4o-transcribe`, `gpt-4o-mini-transcribe` (removed from the
  API 2027-02-26, labelled); not `gpt-4o-transcribe-diarize` (no speaker UI)
  or realtime-only `gpt-live-transcribe`.
  - **Branch on `OPENAI_ARRAY_FIELD_MODELS`**: `gpt-transcribe` sends
    `languages[]=<code>` (never also `language`) and one `keywords[]` per
    term; older models send `language` + comma-joined `prompt` (pinned by a
    multipart-parsing test).
  - **Drop terms containing `<`, `>`, CR or LF** (one rejects the whole
    request); log the count once at INFO, never the terms.
    `parse_custom_vocabulary` does not split on CR.
  - All four models use the engine's language list.
  - Schema 25 = schema 24 with `has_openai_key` for the endpoint (pre-25
    files carry `gpt-4o-mini-transcribe`).
  - **Not verified live** (no OpenAI key).
- **Soniox and Mistral Voxtral were not added (2026-09-21)**: a new provider
  must be both more accurate and cheaper than what is offered
  (MAI-Transcribe-2 and ElevenLabs Scribe v2 lead). Open argument: Soniox's
  $0.12/h realtime. Figures: `docs/provider-costs.md`,
  `docs/local-asr-model-candidates-2026.md`.
- **A remote batch recording past its engine's limit goes out in parts, cut
  at quiet points (2026-09-27).** OpenAI/Groq refuse >25 MB (~13 min of
  16 kHz WAV); `gpt-4o-transcribe`/`gpt-4o-mini-transcribe` (2,000 output
  tokens) truncate silently. `config.remote_batch_part_limit(engine, model)`
  is the single answer (engine seconds + byte cap, tighter per-model seconds,
  180 s for the token-capped two; vendor figures beside the constants).
  `transcriber/_audio_parts.transcribe_in_parts` wraps the provider's own
  single-request method.
  - **Within both limits the recording goes out untouched** (same object,
    judged from the WAV header; byte cap inclusive, pinned). Undecodable
    input (MP3, 24-bit PCM) goes whole; over the cap it logs
    `remote_audio_not_split` with the reason.
  - **Both bounds hold per part**: seconds fit 16 kHz (pinned), the byte cap
    (`max_part_frames`) bounds 44.1/48 kHz imports. Parts are 16-bit mono at
    the input rate.
  - **The cut is `_pcm_audio.split_into_passes`** (shared with Granite CTC;
    `max_samples` beside `max_seconds`): quietest 20 ms frame in the last
    15 s of each window; parts share no audio.
  - **A failed part names itself and carries the earlier text** ("Transcribing
    part i of n failed: ..." + `recovered_text_suffix`), as does an empty part
    that holds sound (loudest window >= the silence gate's default threshold,
    or unmeasurable); an empty silent part is skipped. Join with one space;
    progress names the part; the cancel hook is checked before every part.
  - Split, not compressed (new dependency; OpenAI rejects FLAC).
  - Sent whole: Deepgram (2 GB), ElevenLabs (3 GB / 10 h), AssemblyAI
    (2.2 GB / 10 h), Fun-ASR (streams).
- **Diagnostics**: workers log `transcription_timing`. Groq reuses its
  SDK/HTTP client for the cached transcriber's lifetime.
- **The custom endpoint is one engine with two API styles, free-text model
  ids and a key command (2026-09-30).** `custom_endpoint_provider.py`, modelled
  on the OpenAI provider (urllib, `create_ssl_context()`, `http_error_suffix`,
  `transcribe_in_parts`). Rules:
  - **The base URL is used as given** (stripped, trailing `/` removed, http(s)
    only, no credentials/query); gateways serve the routes under different
    prefixes, so nothing is appended.
  - **`custom_api_mode` picks the request**: `transcriptions` is the OpenAI
    multipart shape (`prompt` = comma-joined vocabulary); `chat` posts an
    `input_audio` part with a verbatim-transcript instruction, `temperature:
    0` and `reasoning_effort: "low"`. A 400 whose detail names `reasoning` or
    `thinking` drops the field once per runtime (INFO log, no body). Why:
    a gateway measured here routes audio only to a multimodal LLM, where
    `low` took a 25 s clip from 8-9 s to 2.5-3.3 s and `minimal`/`none` were
    rejected; chat models without audio input answer 400 as well, and that
    400 is reported, not retried.
  - **Part limits are keyed by API style**:
    `remote_batch_part_limit(engine, model, api_mode)` reads
    `REMOTE_BATCH_API_MODE_LIMITS` first (chat: 300 s / 15 MB raw, since
    base64 grows the body a third); the transcription style uses OpenAI's
    600 s / 25 MB.
  - **The key command wins over the stored key**, runs without a shell
    (`shlex.split(posix=os.name != "nt")`, quotes stripped on Windows), with
    stdin closed, `CREATE_NO_WINDOW` and a 30 s timeout; its last stdout line
    is the token, cached `CUSTOM_KEY_COMMAND_TTL_S` (300 s); a 401 re-runs it
    once and retries once. Errors name the exit code and the last stderr
    line, never the token, and nothing logs it.
  - **The identity reads endpoint, API style and key command**, and
    `has_api_key` is true for a stored key *or* a key command (a command
    alone makes the engine runnable); the connection test, the "all
    configured" target and the Import tab's credential check treat a typed
    command the same way.
  - **Model ids are free text**: the Transcription tab's remote combo is
    editable only for `custom`, "Fetch models" lists `GET {base}/models` on a
    worker thread with the typed (unsaved) fields and rolls back if
    `Thread.start` fails; the list is never persisted. Entries whose LiteLLM
    `mode` is `embedding`/`image_generation`/`rerank` are dropped,
    `audio_transcription` then `chat` sort first; reads are bounded (2 MB
    list, 8 MB reply).
  - **Not verified against a live server by the app's own code path**; the
    chat request shape was verified by hand against one gateway.

