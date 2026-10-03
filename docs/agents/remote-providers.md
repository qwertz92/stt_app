# Remote providers: design decisions

Binding project rules, condensed from the entries moved out of `AGENTS.md` on
2026-09-30. Read this file before changing `transcriber/*_provider.py`,
remote batch splitting and provider error handling. Entries keep their order,
so "the entry above/below" refers to this file; "Known limitations" is
`docs/agents/known-limitations.md`. Measurements, vendor quotations and
history are in `docs/learning-log.md` and git history.

Verbatim pre-condensation text: `git show e608f86:docs/agents/remote-providers.md` (original AGENTS.md: `df2642a`).

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
  reader for all REST providers: capped at 300 chars, status phrase when
  the body is empty or unreadable. **An HTML body (`is_html_page`) is
  never pasted** (2026-10-03): a proxy's 403/407/413 block page is reported
  as `the reply was an HTML page titled "<title>" (a proxy or firewall block
  page?)` (title as text only, 80 characters) or, without a `<title>`, the
  same sentence without it (`markup_page_description`). Only a body that
  *starts* as HTML (`<!doctype html`, `<html`, `<head`, `<body`, `<title`)
  counts: an XML error (`<Error><Message>...`) carries the provider's
  message and is passed through like any other text. `is_markup_page` (any
  `<`) stays the test for a 200 reply that should have been JSON. The
  title is searched in the first 4 KB only: the lazy regex was quadratic on
  64 KB of unclosed `<title>` (3.95 s).
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
  - **Socket timeout = 120 s + the recording's duration** (2026-10-03). The
    request is synchronous and Microsoft documents only "faster than
    real-time" (fast-transcription page, read 2026-10-03; no figure, no
    timeout guidance), so the duration is the longest an answer can take: an
    hour-long part, the engine's bound, got 120 s before. Read from the WAV
    header (`_audio_parts.wav_seconds`); audio it cannot read keeps 120 s.
    Mistral instead shortens its parts to fit a fixed 300 s.
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
- **Soniox was not added (2026-09-21)**: a new provider must be both more
  accurate and cheaper than what is offered (MAI-Transcribe-2 and ElevenLabs
  Scribe v2 lead). Open argument: Soniox's $0.12/h realtime. Figures:
  `docs/provider-costs.md`, `docs/local-asr-model-candidates-2026.md`.
  Speechmatics and Mistral were assessed the same way and are integrated
  anyway, on request (2026-10-01, entries below); neither beats both leaders
  on the 2026-09-21 reading.
- **Speechmatics is a polled batch job (`speechmatics_provider.py`,
  2026-10-01).** `POST /v2/jobs/` with `data_file` and a JSON `config`, then
  `GET /v2/jobs/{id}` until the status is terminal, then `GET
  /v2/jobs/{id}/transcript?format=txt`. Vendor pages read 2026-09-27 (quick
  start, models, languages, language identification, batch limits, the
  `batch.yaml` spec, authentication). Rules:
  - **The loop is `_job_poll.poll_job`, the transcript fetch
    `fetch_with_retries`**: a total budget (`SPEECHMATICS_BATCH_MAX_WAIT_S`,
    1800 s) plus one request, three consecutive failed fetches, the shutdown
    flag, the job id in every message, a terminal status as the positive
    test. An answer a retry cannot change (401/403, and 404/410 for a job
    "expired" or "deleted from the storage") raises `TranscriptionError` and
    ends the wait at once.
  - **Melia 1 is the default model** because the app's default language is
    Auto: Melia "does not support the `auto` option", so Auto is sent as
    `"language": "multi"` and a chosen language as `language_hints`. Enhanced
    ("the highest accuracy") and Standard offer no Auto, because automatic
    identification needs "at least 60 seconds of speech" and rejects the job
    otherwise; a stored Auto reaching them is sent as `de`, as the Cohere
    runtime does. `zh` is sent as `cmn`; the bilingual packs (`tl` is one),
    `eo`, `ia` and `ug` are not offered.
  - **The custom vocabulary is per model**: Enhanced and Standard send it as
    `additional_vocab` `{"content": term}` entries; Melia's custom dictionary
    is "Not yet.", so `config.CUSTOM_VOCABULARY_EXCLUDED_MODELS` makes
    `supports_custom_vocabulary` answer False for it, the factory does not
    hand the terms over, and the controller identity ignores them there.
  - **Region** `speechmatics_region`: `eu1` (default), `us1`, `au1`, read
    through `config.normalize_speechmatics_region`; the enterprise-only
    `eu2`/`us2` are not offered. Melia in `au1` is refused at construction
    ("EU and US regions only"), before an upload that could only be rejected.
    A slot of the runtime identity through `_ENGINE_REGION_FIELDS`.
  - **Parts**: at most 1800 s and 999,000,000 bytes per job ("less than 1 GB";
    no duration limit is stated), so one job stays well inside the wait
    budget.
  - **Not verified live**: no Speechmatics key exists here. The request shape
    is the documented one; the error body `{code, error, detail}` is unwrapped
    by `read_http_error_detail`.
- **Mistral is one synchronous request (`mistral_provider.py`, 2026-10-01).**
  `POST https://api.mistral.ai/v1/audio/transcriptions`, multipart `model`,
  `file`, optional `language`, repeated `context_bias`; the answer's `text`.
  Model `voxtral-mini-2602` (Voxtral Mini Transcribe 2, released 2026-02-04,
  $0.003/min), pinned rather than `voxtral-mini-latest`. Rules:
  - **13 languages** (Mistral's announcement), plus Auto (no `language`
    field); any other code is sent as Auto.
  - **The custom vocabulary is `context_bias`**, one repeated field per term
    (up to 100, the app's cap as well; "optimized for English ... experimental"
    for other languages). Repeated fields, not `context_bias[]` and not a
    JSON array, is what Mistral's own Python SDK sends: `context_bias` is
    `Optional[List[str]]` with `FieldMetadata(multipart=True)` in
    `src/mistralai/client/models/audiotranscriptionrequest.py`, and
    `src/mistralai/client/utils/forms.py` serializes a list field as
    `array_field_name = f_name` with the list as the value, i.e. one form
    field per element under the plain name (mistralai/client-python `main`,
    read 2026-10-01).
  - **An answer that is not a transcript is an error**: an HTML page, JSON
    without a string `text`, or a body that is not JSON. `"text": ""` is a
    valid empty answer. One reader for both providers asked for JSON,
    `_http_utils.transcript_from_json`, with `is_markup_page` (below).
  - **No region**: `api.eu.mistral.ai` lists no audio route, and of the global
    endpoint "Mistral does not commit to a specific inference location"
    (regional-inference page, read 2026-09-27).
  - **Parts**: 1800 s and 500,000,000 bytes ("60 minutes", "500 MB"), with a
    300 s socket timeout for the synchronous request.
  - **Not verified live**: no Mistral key exists here.
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
    part i of n failed: ..." + `recovered_text_suffix`). Join with one space;
    progress names the part; the cancel hook is checked before every part.
  - **An empty part never fails the recording (2026-10-01, owner's call).** A
    part that holds sound -- loudest 100 ms window >= the silence gate's
    threshold *as the user set it* (passed by the factory to OpenAI, Groq,
    Azure and the custom endpoint, and part of their runtime identity), or
    unmeasurable -- leaves `[no text returned for m:ss-m:ss]` in its place
    (adjacent gaps share one marker; start rounded down, end up, so a
    short tail never reads `3:00-3:00`) and logs `remote_audio_part_empty`
    at WARNING; a silent one is skipped. `transcript_has_gap`
    (`transcriber/base.py`, beside `gap_marker`) makes the controller mark
    such a recording failed rather than completed, so with `save_last_wav`
    off the stretch the marker names is not deleted with the rest (found by
    the review of a1e0b01). Every road that completes a recording asks it:
    the dictation roads, the import of the managed last recording, and the
    startup check `main._complete_unless_gap`, which finds the transcript in
    history and used to complete -- i.e. delete -- the kept recording on the
    next launch (review of 384f84f). Such a recording has no Retry (its
    overlay is Done); History and Import reach it.
    Failing used to throw away every other part's minutes of speech; skipping
    unmarked would hand back a transcript with a hole that reads complete,
    and the marker is the one channel that reaches the overlay, the document
    and history without a new controller path. Only a recording of which no
    part returned text while one held sound fails, the single request's
    "Empty model text is a failure" rule. A speech-run check
    (`vad.measure_longest_speech_run_s`) instead of the level was considered
    and left out: with a marker instead of a failure, a false "sound" costs a
    marker to delete, a false "silent" costs speech without a trace.
  - Split, not compressed (new dependency; OpenAI rejects FLAC).
  - Sent whole: Deepgram (2 GB), ElevenLabs (3 GB / 10 h), AssemblyAI
    (2.2 GB / 10 h), Fun-ASR (streams).
- **AssemblyAI and Deepgram take a data-residency region (2026-10-01).**
  `assemblyai_region` is `auto` (default), `us` or `eu`; `deepgram_region` is
  `global` (default) or `eu`; read through `config.normalize_assemblyai_region`
  / `normalize_deepgram_region` (anything else is the default). The host
  tables `ASSEMBLYAI_API_BASE_URLS`, `ASSEMBLYAI_STREAMING_HOSTS` and
  `DEEPGRAM_API_HOSTS` sit in `config.py` with the vendor sentences behind
  each guarantee. Rules:
  - **A choice claims no more than the vendor guarantees** (review of
    2026-10-01). AssemblyAI's default streaming host
    `streaming.assemblyai.com` is edge routing ("Your data may be processed
    in any of the US or EU locations"), so the default is `auto`, labelled
    "Automatic", and only `us` streams to the US data zone
    `streaming.us.assemblyai.com`. Batch has no separate US host:
    `api.assemblyai.com` "processes your pre-recorded audio transcription
    requests in the US region", so `auto` and `us` share it. Deepgram calls
    `api.deepgram.com` "the default global endpoint" and states no location
    for it, so its default is `global`, not `us`; Deepgram also offers
    `api.au.deepgram.com` and `api.in.deepgram.com`, not offered here. The
    first version offered `us`/`eu` for both and streamed AssemblyAI "US" to
    the routed host; that never shipped, so no migration exists.
  - **Speechmatics**: "Jobs are created in the region corresponding to the
    endpoint used" (authentication page, read 2026-10-01); the page states
    no further storage guarantee, and the tooltips say exactly that.
  - **Batch, streaming and the connection test use the same region**: a
    connection test against the US host would pass for a key the EU host
    then refuses, or the reverse.
  - **The defaults are exactly what was sent before**: `auto` and `global`
    use the hosts every earlier build used.
  - **AssemblyAI's `_configure()` sets `settings.base_url` on every call, in
    both regions**: the SDK's settings are process-global and
    `Client.get_default()` rebuilds its client when they change, so a US
    runtime created after an EU one would otherwise inherit the EU host. A
    test drives the installed SDK's `Client.get_default()`. Two runtimes of
    different regions transcribing at the same moment would still race on
    that global (only an isolated runtime beside the shared one can do it).
  - **The region is a slot of the runtime identity** (`remote_region`, read
    through `_ENGINE_REGION_FIELDS` for the selected engine only), because the
    host is baked into the provider at construction.
  - **One field map, `settings_store._REMOTE_REGION_FIELDS`**: the
    controller imports it as `_ENGINE_REGION_FIELDS`, and the Settings
    dialog's region selectors (Providers tab, `_REMOTE_REGION_CHOICES` in
    `settings_dialog_helpers.py`) read and write through it.
  - No schema bump: an absent key is the default.
  - **Not verified live**: no request was sent to either EU host (no Deepgram
    key; the AssemblyAI key was not used). Deepgram's page names the REST
    host only; `wss://api.eu.deepgram.com/v1/listen` is derived from it.
    AssemblyAI's pages say nothing about key or model availability in the EU,
    so whether Universal-3.6 Pro streams there is open.
- **Diagnostics**: workers log `transcription_timing`. Groq reuses its
  SDK/HTTP client for the cached transcriber's lifetime.
- **The custom endpoint is one engine with two API styles, free-text model
  ids and a key command (2026-09-30).** `custom_endpoint_provider.py`, modelled
  on the OpenAI provider (urllib, `create_ssl_context()`, `http_error_suffix`,
  `transcribe_in_parts`). Rules:
  - **The base URL is used as given** (stripped, trailing `/` removed, http(s)
    only, no credentials/query); gateways serve the routes under different
    prefixes, so nothing is appended. The one change: a route the app
    appends itself (`/audio/transcriptions`, `/chat/completions`,
    `/models`, any case) is taken off the end (2026-10-01), because a
    pasted request URL otherwise doubled the route into a 404.
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
    (POSIX: `shlex.split`; Windows: `_split_windows_command`), with
    stdin closed, `CREATE_NO_WINDOW` and a 30 s timeout; its last stdout line
    is the token, cached `CUSTOM_KEY_COMMAND_TTL_S` (300 s); a 401 re-runs it
    once and retries once. Errors name the exit code and the last stderr
    line, never the token, and nothing logs it.
    **Windows quoting (2026-10-03)**: a backslash is never an escape (paths
    stay intact); a `"` groups anywhere in a word (`--opt="a b"`), a `'`
    only at the start of a word, so `wsl.exe -e bash -lc 'echo x'` reaches
    bash as one script and an apostrophe in `C:\Users\O'Brien` stays
    literal; inside one kind of quote the other is literal; an unclosed
    quote is refused. A literal `"` cannot be written (put it in a script).
    Why not reject single quotes: the owner's helper is a WSL command, where
    `'...'` is what everyone writes, and `shlex` with `posix=False` had
    kept the quotes (`'echo x'` reached bash literally) and split
    `--opt="a b"` in two. The exit code is judged **before** the output: a
    failing helper that printed "Please run 'login' first" to stdout was
    reported as a malformed token.
    **PowerShell keeps its single quotes after `-Command`** (2026-10-03,
    regression of the rule above): the old splitter kept every single
    quote, which PowerShell needs for `-Command Get-Content 'C:\a b\t.txt'`
    and `-Command '$env:USERNAME'`; stripping them split the path in two
    and let `$env:USERNAME` be evaluated. Rule: for `powershell`/`pwsh`
    (any path, `.exe` optional), every word after `-Command` or an
    abbreviation (`-c`, `-co`, ...) keeps the quotes of a single-quoted
    word; before it (`-File 'x.ps1'`) they are stripped. Chosen over "stop
    stripping everywhere" because the WSL case needs the strip, and over
    "keep after any `-c`" because `-c` means something else for other
    programs. Run for real with Windows `powershell` (path with a space,
    `&` call, `$env:` literal, `-File`) and `wsl.exe`; `pwsh` only in the
    splitter test.
    **The program is resolved with `shutil.which`** (2026-10-03):
    CreateProcess appends only `.exe`, so the `.cmd` shims `az`, `gcloud` and
    `npm` were "not found". A resolved `.cmd`/`.bat` goes to `run_bounded`
    as it is: CreateProcess starts cmd.exe for it, inside the job object,
    so the grandchild kill still holds (test: a `.cmd` whose python child
    leaves a heartbeating grandchild, ended at the timeout). Not an
    explicit `cmd.exe /c` wrapper: no quoting from the caller can stop
    cmd.exe reading `& | < > ^ %` in an argument (reproduced: `x.cmd "a&b"`
    ran `b`), so `_resolve_program` refuses such an argument for a batch
    target, naming the character, never the argument (it may be a secret).
    An unresolvable name stays as typed for the "not found" message.
    **It runs through `process_tree.run_bounded`, never `subprocess.run`**
    (2026-10-01): `subprocess.run(timeout=...)` kills the direct child and
    then reads the pipes to the end, which a grandchild holding them
    (`wsl.exe -e ...`, a `.cmd` wrapper) keeps open -- 15.1 s on a 2 s
    timeout, 25 s in the test's reproduction. `run_bounded` kills the
    process tree (`kill_process_tree`, shared with the benchmark worker)
    and reads for at most 2 s after.
    **On Windows the child runs in a job object** (review of 2026-10-01):
    a child that printed its token and exited 0 while a grandchild kept the
    inherited pipes open made `communicate` wait for the timeout, and
    `taskkill /T` cannot reach a grandchild whose parent has exited. The
    child is created suspended, put into a job and resumed
    (`NtResumeProcess`, since `Popen` closes the thread handle), and once the
    direct child has exited and the pipes stay open
    `_PIPES_GRACE_AFTER_EXIT_S` (0.5 s) longer, the job is terminated and
    the child's own output and exit code returned. The job has no
    kill-on-close limit, so a helper that detached a daemon from its stdio
    leaves it running. POSIX does the same through `killpg` on the child's
    session, which reaches the group after the leader exited.
  - **The key and a command's token must be visible ASCII**
    (`_header_unsafe`, 2026-10-01): `http.client` refuses a header value
    with a line break and puts the whole value -- `Bearer <key>` -- into
    its `ValueError`, which reached the overlay and the log. Such a key is
    refused at construction without being echoed, and `_other_error`
    replaces any "invalid header" message.
  - **Redirects go through `_RedirectGuard`** (2026-10-01): urllib's own
    handler copies `Authorization` to whatever host `Location` names
    (reproduced 127.0.0.1:A -> localhost:B; https -> http would send it
    in clear) and turns a redirected POST into a bodiless GET answered
    with a 405. An upload is never redirected; a model list only within
    its origin (same scheme, host and port, or http -> https on the
    default ports, `_redirect_keeps_origin`). A refusal names the target
    without its query. `_open` builds the opener; tests patch `_open`.
  - **An answer that is not a transcript is an error** (2026-10-01): an
    HTML page (a proxy's sign-in or block page answering 200) used to be
    returned as the transcript and pasted; JSON without a string `text`
    used to read as silence. `"text": ""` stays a valid empty answer.
    **Any body starting with `<`** (after whitespace and a UTF-8 BOM) is a
    markup page (`_http_utils.is_markup_page`): matching `<!doctype html` /
    `<html` let a BOM, `<!-- -->`, `<?xml ?>` or a bare `<head>` through.
    **A body that is not JSON is an error naming its first 80 characters**:
    the request sends `response_format=json`, so "Internal Server Error"
    was a proxy's error page, not a transcript (it used to be pasted). A
    top-level JSON string is still taken.
  - **A credential never reaches an error message** (2026-10-03): every
    error raised while sending a request or listing models passes
    `_errors_scrubbed`, which replaces the active token and the stored key
    with `[hidden]` (only credentials of 8+ characters: `none` is a
    placeholder and would be cut out of ordinary words). **The scrub runs
    before every cut** (2026-10-03): the readers that shorten server text
    (`read_http_error_detail` 300 characters, `body_excerpt` 80,
    `reply_error_text` 300, a block page's title) take a `redact` callable
    (`self._scrub`) and apply it to the decoded text and to each extracted
    field first; scrubbed afterwards, a key that straddled the cut left its
    first characters (`...xxxsk-secret`). `_scrub` also replaces the key as
    JSON writes it (`"` and `\` escaped). `_errors_scrubbed` stays as the
    net over whatever else reaches a message. A gateway's 401
    reason ("Key expired on ...") is shown after the standard text, except
    when it holds the credential: then it is dropped rather than masked,
    because a partly masked key is still part of the key (LiteLLM answers
    "Received API Key = ..."). A key that the gateway masks on its own, so
    that only a part reaches us, cannot be recognised and is shown as the
    gateway sent it. `{"detail": {"error": "..."}}` (FastAPI/LiteLLM 403)
    reads as its message (`nested_error_text` also tries `error`, which
    Fun-ASR's `task-failed` reader shares), and a 200 reply whose body is
    an `error` object (`reply_error_text`) shows that text, in both API
    styles, instead of "no 'text' field" / "no message content".
  - **Chat: `content: null` is an error, not silence** (2026-10-01): it is
    a refusal (named, shortened to 80 characters) or an answer cut off
    (`finish_reason` named, e.g. `length`). `content: ""` stays silence.
    **`finish_reason: "length"` (or `max_tokens`, any case) is an error even
    when text came with it** (2026-10-03): the text is the start of the transcript, and pasted as a
    whole it lost the end; the message names the model's output limit and
    suggests the transcription style or shorter dictations.
  - **The identity reads endpoint, API style and key command**, and
    `has_api_key` is true for a stored key *or* a key command (a command
    alone makes the engine runnable); the connection test, the "all
    configured" target and the Import tab's credential check treat a typed
    command the same way.
  - **Model ids are free text**: the Transcription tab's remote combo is
    editable only for `custom`, "Refresh" lists `GET {base}/models` on a
    worker thread with the typed (unsaved) fields and rolls back if
    `Thread.start` fails. The list is saved by Save as `custom_models`
    (2026-10-03; in the unsaved-changes fingerprint, a list in the JSON --
    `to_dict` writes a list, since `load` rewrites a file that differs from
    the payload and a tuple never equals JSON's list) and offered again after
    a restart; a failed Refresh, or one that lists no models, keeps the
    previous list (the note says so). **A list belongs to the base URL that
    listed it** (`custom_models_endpoint`, `listed_custom_models`): the
    dialog keeps one list per URL for the session and shows the Base URL
    field's, and a saved list whose URL differs from `custom_endpoint` (Save
    API Keys writes the URL alone) is not offered; changed from A to B and
    saved, the dialog used to offer A's models as B's. Entries whose LiteLLM
    `mode` is `embedding`/`image_generation`/`rerank` are dropped,
    `audio_transcription` then `chat` sort first, and within each group an
    id that names a speech recognizer (`whisper`, `transcri`, `parakeet`,
    ...; not `tts`) comes first, since OpenAI's own list sends no `mode`;
    ordering only, never a filter. Reads are bounded (2 MB list, 8 MB
    reply).
  - **Verified live, in part (2026-09-30, `docs/learning-log.md`)**: the
    owner's LiteLLM gateway, through the app's own code path -- the key
    command with an 861-character JWT, the model list (19 entries), the
    connection test, and a 25 s German clip transcribed in the chat style
    in 4.3 s. Not verified: the transcription style produced a transcript
    nowhere (on that gateway it reaches the backend's own HTTP 403), so the
    multipart request has only been seen by fake servers; a local speech
    server (speaches, LocalAI), a hosted OpenAI-compatible API and a
    recording split into parts were not run live either. The 2026-10-03
    fixes (key-command lookup and quoting, error scrubbing, HTML block
    pages) were tested with fakes and, for the key command, with real
    `npm`, `wsl.exe` and `.cmd` shims on Windows, not against the gateway.
  - **Proxies** (2026-10-03, `_open` builds a standard urllib opener):
    `HTTP_PROXY`, `HTTPS_PROXY` and `NO_PROXY` (either case) are honoured;
    on Windows the registry's proxy (Internet Options, `ProxyServer` with
    `ProxyEnable`) is read only when none of these variables is set; a PAC
    script (`AutoConfigURL`) and WPAD are not supported, so a company that
    publishes only a PAC file needs the variables set by hand. A proxy that
    refuses `CONNECT` surfaces as `Tunnel connection failed: <status>`.
    Reproduced against a fake proxy for the variable cases; the registry
    path is urllib's own and was not run.
  - **Part sizes are fixed** (`config.remote_batch_part_limit`): 10 min / 25
    MB for the transcription style, 5 min / 15 MB for the chat style
    (base64 grows the body by a third). A gateway or proxy with a lower
    body limit answers 413; the error says so and names these figures
    (`_request_too_large_hint`), because no setting can shrink the parts.
    The advice depends on the style: the chat parts are the *smaller*
    requests (15 MB of audio is about 20 MB base64-encoded, against 25 MB),
    so a chat 413 says "raise the limit or dictate shorter recordings; the
    transcription style sends larger requests", and a transcription 413
    adds "or try the chat style" with its figures. The first version told a
    chat user to switch to the larger style.
  - **Chat audio is `wav` or `mp3` only** (2026-10-03): `input_audio.format`
    takes those two values in the OpenAI shape, and the app ships no
    decoder, so another suffix (an imported `.m4a`, `.flac`, ...) is refused
    before sending, with the transcription style named as the way out. The
    transcription style sends the file as it is.

