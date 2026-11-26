# Remote providers: design decisions

Binding project rules, condensed from the entries moved out of `AGENTS.md` on
2026-09-30. Read this file before changing `transcriber/*_provider.py`,
remote batch splitting and provider error handling. Entries keep their order,
so "the entry above/below" refers to this file; "Known limitations" is
`docs/agents/known-limitations.md`. Measurements, vendor quotations and
history are in `docs/learning-log.md` and git history.

Verbatim pre-condensation text: `git show e608f86:docs/agents/remote-providers.md` (original AGENTS.md: `df2642a`).

- **A vendor's superseded model is removed, and a saved choice of it is moved
  once with a notice (2026-11-24, owner's rule).** The app offers a vendor's
  newest generation only; an older model stays only for a reason written in
  this file (clearly better at something, or far cheaper). The map is
  `config.RETIRED_REMOTE_MODELS` (engine -> retired id -> successor).
  `AppSettings.from_dict` applies it through `_current_remote_model`;
  `SettingsStore.load` rewrites the file in the same call, so the move happens
  once, and `AppSettings.pending_model_moves` hands `main` one tray message
  ("Speech model updated", cleared once shown) for the engines the user uses (the selected one, or one with a
  stored key: a file written before an engine existed carries its default for
  everyone). An id that is neither current nor in the map is damage and falls
  to the default without a notice. A history row keeps the model id it was made
  with.
- **AssemblyAI models (2026-11-24, pages read that day: `llms/models.md`,
  `docs/openapi.json`, `docs/streaming/api-spec/streaming-websocket.md`,
  `docs/streaming/select-the-speech-model.md`).** Batch: send `speech_models`;
  `assemblyai-stt-3-7` ($0.21/h, 32 languages, same price as the deprecated
  `universal-3-5-pro`) goes alone, never with a silent `universal-2`
  fallback. `universal-3-5-pro` and `universal-3-pro` (deprecated by
  AssemblyAI 2026-11-17; accounts whose default is one of them move to
  STT-3.7 on 2026-12-08, while a `universal-2` default is not upgraded:
  STT-3.7 migration guide, read 2026-11-25) move to it, and `best`/`nano` fall to it silently.
  `universal-2` stays only as the cheaper choice for languages outside the 32.
  - **Always send `disfluencies=false` and `format_text=true`.** STT-3.7
    defaults `disfluencies` to true (the OpenAPI text: "Defaults to true when
    `speech_models` includes `assemblyai-stt-3-7`"), which would put "um" and
    false starts into dictated text; Universal-2 defaults to false. Sent for
    both so no vendor default decides.
  - **Language**: `language_code` (a pinned language) or `language_detection`
    stays; `language_codes` replaces `language_detection_options.
    expected_languages` only, which the app never sent. The 32 codes of
    `ASSEMBLYAI_STT_3_7_LANGUAGE_MODES` are the streaming spec's accepted list;
    AssemblyAI's OpenAPI spec also carries `yue`, `nn`, `xh`, `zu`.
  - **Streaming language** (2026-11-26; streaming WebSocket spec read that
    day, https://www.assemblyai.com/docs/streaming/api-spec/streaming-websocket.md):
    the realtime model has no `language` parameter. `language_codes` steers
    it toward a set of languages ("per-token biasing"); unset, it
    code-switches across its 32 languages by itself, and the spec's
    `language_detection` only reports the detected language. So Auto sends
    nothing, a chosen language is sent as `language_codes=[code]`
    (`_stream_language_codes`; SDK 1.10.11 `StreamingParameters` has the
    field), and the picker in Streaming offers `ASSEMBLYAI_STT_3_7_LANGUAGE_MODES`
    whatever the batch model is. A saved language outside the 32 (a
    Universal-2 batch choice) streams in Auto with a log warning; the picker
    shows Auto with a note naming the language and keeps the saved value.
    Until 2026-11-26 the picker was locked to Auto and the choice never
    reached the request (a leftover of Universal-Streaming), so a language
    an existing user had saved for batch steers streaming from then on;
    Auto restores code-switching. **Unverified
    live** (no key): that the service honours `language_codes` as the spec
    says.
  - **Streaming**: `assemblyai-stt-3-7-realtime` ($0.30/h), not the `-agent`
    voice-agent model ($0.45/h) and not `universal-3-6-pro`, which AssemblyAI
    keeps supported as the previous generation. Same endpoint and message
    protocol; the streaming spec has no `disfluencies` parameter, and the
    prompting page says the transcription instruction is built in, so whether
    filler words appear in streamed text is **unverified** (no key).
  - **SDK 1.10.11** (was 1.6.1): the streaming `speech_model` is an enum, and
    1.6.1 has no `assemblyai-stt-3-7-realtime` member, so `StreamingParameters`
    would refuse it. 1.10.11 (2026-11-17) has no advisory on OSV on 2026-11-24.
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
- **Every REST request that carries a key is opened by
  `_http_utils.open_request`** (2026-11-26; review finding: Meta sent its key
  to another host on a redirect, and the OpenAI, Mistral, Deepgram,
  ElevenLabs, Azure, Speechmatics and AssemblyAI-check requests did the same,
  because `urllib.request.urlopen` follows redirects and copies
  `Authorization` / `xi-api-key` to the new host: reproduced with a fake
  vendor answering 301, 302, 303, 307 or 308 to a second localhost port). The
  helper does three things. It passes `create_ssl_context()` (`urllib` ignores
  `REQUESTS_CA_BUNDLE`, so AssemblyAI's `test_connection` once failed behind a
  TLS proxy while the SDK worked; `format_ssl_error_message` is the shared
  text, naming `SSL_CERT_FILE` too). It installs `RedirectGuard`: **an upload
  (a request with a body) is never redirected** -- urllib would re-send it as a
  GET without the audio, which the vendor answers with a 405 that names
  nothing the user can act on -- and **a GET (a key check, a poll, a model
  list) follows a redirect only inside its origin**: the same scheme, host
  and port, or http -> https on the default ports of one host
  (`redirect_keeps_origin`). A same-origin redirect is followed because the
  key then goes only to the host the user already trusts with it (a vendor
  moving a route inside its own host); another host, another port or
  https -> http is refused. The refusal names the engine, the HTTP status and
  the target's scheme, host, port and path (no query), says the key was not
  sent there, and names a likely cause: a TLS-intercepting proxy answering with
  its sign-in page is the realistic trigger. Third, it refuses a header value
  that cannot be sent (a key with a line break, a non-breaking space or a
  non-ASCII letter) before any request, with a sentence that does not print
  it; `http.client` would put the whole `Bearer <key>` into its `ValueError`.
  Call sites (`grep -rn "open_request" src`): OpenAI x2 (upload, key check),
  Mistral x2, ElevenLabs x2, Deepgram x2, Azure x2 (upload; the key check is a
  one-second upload too), Speechmatics (`_request`: job upload, polls and
  transcript fetch), Meta (upload and key check), Soniox (`_request`: upload,
  job, polls, transcript, deletions, key check), AssemblyAI (key check
  only), the custom endpoint and OpenRouter (upload, model list, key check).
  Not covered: AssemblyAI's and Groq's SDK calls (their redirect handling is
  the SDK's and was not checked); the AssemblyAI, Deepgram and Fun-ASR
  WebSockets (not urllib); `update_checker` and `update_installer` (GitHub,
  no key); `modelscope_mirror` (its own https-only handler, no key);
  `_job_poll` (opens nothing itself; its callers use `open_request`).
  Tests: `tests/test_provider_redirects.py` (every REST engine x the five
  codes, upload and key check, and the unsafe keys).
- **A 200 answer that is not a transcript is an error in every REST engine**
  (2026-11-26; review of the last-round providers). The reader is shared:
  `_http_utils.json_from_reply` (an HTML page or a non-JSON body gets a
  sentence starting with the engine's prefix, never the parser's "Expecting
  value") and `missing_field_error` (quotes a gateway's `error` member, else
  names the answer's keys); `transcript_from_json` is both. Measured with one
  parametrized test (`tests/test_rest_reply_guard.py`, a proxy's HTML page and
  JSON without a transcript through each engine's real batch path): **open**
  were OpenAI and ElevenLabs (the page's markup was *returned as the
  transcript*, so it would have been pasted), Deepgram and Azure (HTML ended
  as "Expecting value: line 1 column 1", JSON without the transcript member
  as silence, `""`), and Speechmatics (`format=txt` is plain text, so the page
  or a JSON object at 200 was the transcript). **Already safe**: Mistral, Meta,
  the custom endpoint in both API styles, OpenRouter. What counts as silence
  stays: `"text": ""`, Azure's `{"combinedPhrases": []}` and Deepgram's empty
  `channels` or `alternatives`; an answer without Deepgram's `results`, or
  without Azure's `combinedPhrases` and `text`, is not silence. Speechmatics
  names the job in these errors too, since the job already runs.
- **No REST engine's error text or log shows its own key** (2026-11-26,
  review). A gateway may echo the credential it refused (`{"error": {"message":
  "quota exceeded for <key>"}}` in a 200 reply, or the same in a 4xx body);
  OpenAI, ElevenLabs, Deepgram, Azure, Speechmatics' fetch and Mistral printed
  it, while Meta, the custom endpoint and OpenRouter were clean. The shared
  readers (`read_http_error_detail`, `http_error_suffix`, `reply_error_text`,
  `json_from_reply`, `missing_field_error`, `transcript_from_json`) now take a
  **required** `redact`, so an engine cannot forget it, and every engine builds
  it with `_http_utils.key_redactor(api_key)` (replaces the key and its
  JSON-escaped form by `[hidden]`, ignores values under 8 characters; Meta's
  and the custom endpoint's copies were replaced by it). Tests:
  `tests/test_rest_key_scrub.py`, every REST engine x a plain and an escaped
  key x a 200 error object, a 4xx body, and the key check, with the log.
- **The SDK and WebSocket engines scrub their key too** (2026-11-26, review
  of the key scrubbing): Groq printed an SDK exception's text ("Error code: 400
  - {... 'invalid key <key>' ...}") in a batch and in its key check, and
  AssemblyAI and Fun-ASR quote SDK or service messages the same way. Their
  `transcribe_batch` runs inside `_http_utils.errors_scrubbed(self._scrub)`
  (the custom endpoint's old private copy), which re-raises a
  `TranscriptionError` with the key replaced; the strings returned by
  `test_connection`, AssemblyAI's streaming errors and Fun-ASR's key check go
  through `self._scrub` directly. Tests: `tests/test_sdk_key_scrub.py`. Not
  covered: tracebacks that `logger.exception` writes for a failed callback
  (AssemblyAI streaming) -- they hold the exception, not a server text, and no
  case where an SDK put the key into one was found.
- **Azure: a transcription result without text is silence** (2026-11-26,
  review; deliberate and **unverified live**, no key exists to try it).
  Microsoft's reference marks no member of the result as required and does not
  say what a silent recording returns; the first version of the guard raised
  when `combinedPhrases` was missing or null, which would have turned silence
  (and the connection test's one second of silence) into an error. The rule
  now: a reply is a transcription result when it has `combinedPhrases` **as a
  list** or `text` **as a string** (a null or wrong-typed member counts as
  absent), or it has a `durationMilliseconds` or a `phrases` member at all. A
  result without usable text, such as `{"durationMilliseconds": 1000,
  "phrases": []}`, is silence (""). Everything else is an error naming the
  answer's keys: `{"combinedPhrases": null}`, `{"text": null}`, `{"text": 5}`,
  `{}`, `[]`, `{"status": "ok"}` and `{"error": {...}}` (the last quotes the
  gateway's message). Before the guard all of those returned "". **Deepgram stays strict**: its
  response schema documents `results` as required (and silence still arrives
  as `results.channels[0].alternatives[0].transcript == ""`), so an answer
  without `results` is not a Deepgram transcription answer.
- **Groq refuses a proxy's page the same way** (2026-11-26, review): it asks
  for `response_format="text"`, so the SDK hands the body back as a string and
  a 200 page was returned (and pasted) as the transcript. The request is
  unchanged (it cannot be tested live here); a string that starts like HTML
  and holds a closing tag is refused with "Groq: an HTML page came instead of
  the transcript -- typically a proxy's sign-in or block page." The test is
  `_http_utils.is_page_instead_of_text`, shared with the Speechmatics fetch.
  A 200 answer that is neither a string nor holds a string `text` (a
  gateway's `{"error": ...}`) is an error through `missing_field_error`, not
  "no speech"; `""` and an object with `text=""` stay silence.
  Engines that refuse a page at status 200: all of them now (the JSON engines
  through `json_from_reply`, the two plain-text ones through this helper).
- **Speechmatics' plain-text transcript is refused only when it is an HTML page
  or a JSON object** (2026-11-26, review): `format=txt` answers text, and a
  dictation may begin with anything (`<b> is the bold tag.`, `< 5 items`,
  `<3 you`, a number, `true`, `[1, 2]`). The first version refused every body
  starting with `<` ("an HTML page") and every body that parsed as a JSON
  object, so those transcripts were lost. Now `is_html_page` (a doctype or an
  `<html`/`<head`/`<body`/`<title` tag at the start) **and a closing tag `</`
  somewhere in the body** ("<html> is the root element." is dictation), and a
  body that parses as a JSON object, are errors, each with the job id and the 7-day retention note;
  everything else is the transcript, as before the change. A plain-text body
  such as "Internal Server Error" cannot be told from a transcript and is
  returned.
- **Speechmatics reads the job-upload and status replies through
  `json_from_reply`** (2026-11-26): a proxy's HTML page there read "Expecting
  value: line 1 column 1 (char 0)". At the upload it names the page; at a
  status poll it is a failed fetch that the poll retries (as before the
  guard: one proxy page between good polls must not cost a second upload and a
  second charge); three in a row end the wait with the poll's own sentence,
  which names the cause ("could not fetch the job status (the answer is an HTML
  page instead of JSON ...). The job may still complete; job id ...").
- **A certificate failure is recognised by type, then by text** (2026-11-26):
  `ssl_utils.is_ssl_error` walks `__cause__`, `__context__` and a `URLError`'s
  `reason` and answers True for any `ssl.SSLCertVerificationError` before it looks at
  words. With `truststore`
  active, as in the running app, Windows verifies the chain and its own text
  reaches urllib: "A certificate chain processed, but terminated in a root
  certificate which is not trusted by the trust provider." (reproduced on
  Windows 11 with a local HTTPS server and a throwaway CA; the exception is an
  `SSLCertVerificationError` whose message holds none of OpenSSL's words). The
  word list alone did not match it, so every engine showed that raw text
  instead of `format_ssl_error_message`. The markers stay for libraries that
  wrap the failure in a plain exception. Any other `ssl.SSLError` (a handshake abort, a protocol mismatch) is not a
  certificate problem: it is recognised only by the markers, as before, so the
  user is not told to install a certificate for it.
- **A refusal of `open_request` during a poll names the job** (2026-11-26):
  `speechmatics_provider._job_request` appends "Job id ..." to the
  `TranscriptionError` that `open_request` raises for a refused redirect or an
  unsendable header, as `_job_poll` requires of every poll error; the status
  and transcript fetches had let it through without the id.
- **A Meta key check that cannot reach Meta says that, not "transcription
  failed"** (2026-11-26): the check is a one-second upload, and a timeout or a
  refused connection surfaced as "Meta (Muse Voice) transcription failed:
  <urlopen error timed out>" although nothing was transcribed. A cause that is
  an `OSError` other than an HTTP error or a certificate failure now gives
  "The connection test could not reach Meta (Muse Voice): no answer within 15
  seconds or the connection was refused." (true for a timeout and for an
  immediate refusal alike);
  the raw text goes to the log (`meta_connection_test_unreachable`, key
  scrubbed). A certificate failure keeps the SSL guidance.
- **Meta refuses a WAVE_FORMAT_EXTENSIBLE float file with the same sentence as
  plain float** (2026-11-26): Python's `wave` reports it as "unknown extended
  format: <GUID>" (3.12), which `_wav_for_meta` did not match, so the user read
  "not a WAV file that can be read" for a file that is a WAV. Both
  "unknown format" and "unknown extended format" now say "a WAV file in a
  format that is not PCM". Tested with a generated 32-bit float extensible file.
- **ElevenLabs**: `scribe_v3` only (2026-11-24: released 2026-11-10, $0.20/h
  like v2, 27% fewer word errors on FLEURS by ElevenLabs' own figure; the
  models page says "Change model_id from scribe_v2 to scribe_v3. Existing
  request parameters work without changes", and the OpenAPI `model_id`
  examples list `scribe_v3`). `scribe_v2` (still sold) is removed by the
  newest-generation rule: nothing in it is better or cheaper. A saved
  `scribe_v2` moves to v3 with a notice; `scribe_v1` (removed 2026-07-09)
  falls to the default and is never sent. `scribe_v3_live` (realtime,
  $0.22/h) is not built. **Not verified live**: no key, so the v3 batch answer
  is read as v2's (`text`).
- **Azure LLM Speech offers `mai-transcribe-2` only (2026-11-24; Microsoft's
  MAI-Transcribe page, updated 2026-11-20, and microsoft.ai/news/
  mai-transcribe-2, read that day).** One engine (no MAI API outside Azure).
  The page still lists `MAI-Transcribe-1.5` and "`MAI-Transcribe-1`:
  Deprecated on Aug 20, 2026"; both are removed from the app (older, fewer
  languages, 1.5 at $0.36/h) and a saved choice moves to MAI-2 with a notice.
  **The $0.10/h price is a promotion**: "priced at $0.10 per hour as a
  limited-time offer until the end of the year" (2026); no 2027 price is
  published, so MAI-2 may end up dearer. Re-judge 1.5 ($0.36/h) when Microsoft
  names it. The request is `enhancedMode.enabled`, `enhancedMode.model`,
  api-version 2025-10-15 on the page, plus `enhancedMode.modelOptions.
  transcribeStyle = "clean"` (added 2026-11-25): the page documents it for
  MAI-Transcribe-2 with values "verbatim" (default, "including filler words
  and false starts") and "clean" ("removes fillers"), and its example request
  nests it exactly there. Pinned by `tests/test_azure_provider.py`.
  **Untested against the live service** (no key): whether the service
  accepts it on every resource and what "clean" does to the text is open.
  - Settings store the lower-case id; `AZURE_API_MODEL_NAMES` holds the
    documented spelling that is sent (e.g. `MAI-Transcribe-2`).
  - Languages follow that table via `AZURE_LOCALE_OVERRIDES` (`no` -> `nb`,
    `tl` -> `fil`); a test compares the *sent* codes per model both ways.
  - **Socket timeout = 120 s + the recording's duration** (2026-10-03). The
    request is synchronous and Microsoft documents only "faster than
    real-time" (fast-transcription page, read 2026-10-03; no figure, no
    timeout guidance), so the duration is the longest an answer can take: an
    hour-long part, the engine's bound, got 120 s before. Read from the WAV
    header (`_audio_parts.wav_seconds`); audio it cannot read keeps 120 s.
    Mistral instead shortens its parts to fit a fixed 300 s.
  - **Not verified live** (`docs/azure-llm-speech.md` says so). No phrase
    list is sent, so custom vocabulary stays unwired.
- **OpenAI: `gpt-transcribe` is the only model (2026-11-24; pages read that
  day: guides/speech-to-text, models/gpt-transcribe, deprecations).**
  `whisper-1`, `gpt-4o-transcribe` and `gpt-4o-mini-transcribe` (and
  `gpt-4o-transcribe-diarize`, removed 2027-04-01) are on OpenAI's deprecations
  page with removal on 2027-02-26 and replacement "gpt-live-transcribe or
  gpt-transcribe". They are removed from the app now and a saved choice moves
  to `gpt-transcribe` with a notice. The only one with a price argument was
  `gpt-4o-mini-transcribe`: $0.003/min ($0.18/h, read 2026-09-21) against
  $0.0045/min ($0.27/h), a third less, not "far cheaper", and the pricing page
  no longer lists it, so nothing to cite. Re-add it only if OpenAI republishes
  a price and the removal date moves. `gpt-live-transcribe` is a realtime
  session model and does not answer on this endpoint.
  - **One request shape**: `languages[]=<code>` (never also `language`) and one
    `keywords[]` per term. The comma-joined `prompt` branch and the 180 s
    token-cap part limit of the two `gpt-4o` models are gone with them.
  - **Drop terms containing `<`, `>`, CR or LF** (one rejects the whole
    request); log the count once at INFO, never the terms.
    `parse_custom_vocabulary` does not split on CR.
  - The language list is the engine's (OpenAI's guide enumerates only code
    formats). The schema-25 adoption (an OpenAI file without a key took the
    default) is gone: every retired id moves, with or without a key.
  - **Not verified live** (no OpenAI key).
- **Soniox was not added on 2026-09-21 and is integrated since 2026-11-26**
  (entry "Soniox is a polled batch job" below): on 2026-09-21 a new provider
  had to be both more accurate and cheaper than what was offered
  (MAI-Transcribe-2 and ElevenLabs Scribe v2 led), and Soniox's accuracy did
  not clear that bar; it is added on the owner's request for v0.15.0, like
  Speechmatics and Mistral on 2026-10-01 (neither beat both leaders on the
  2026-09-21 reading either). Figures: `docs/provider-costs.md`,
  `docs/local-asr-model-candidates-2026.md`. German accuracy is still not
  measured here: run a German A/B before recommending it.
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
    ("the highest accuracy") offers no Auto, because automatic
    identification needs "at least 60 seconds of speech" and rejects the job
    otherwise; a stored Auto reaching them is sent as `de`, as the Cohere
    runtime does. `zh` is sent as `cmn`; the bilingual packs (`tl` is one),
    `eo`, `ia` and `ug` are not offered.
  - **The custom vocabulary is per model**: Enhanced sends it as
    `additional_vocab` `{"content": term}` entries; Melia's custom dictionary
    is "Not yet.", so `config.CUSTOM_VOCABULARY_EXCLUDED_MODELS` makes
    `supports_custom_vocabulary` answer False for it, the factory does not
    hand the terms over, and the controller identity ignores them there.
  - **Region** `speechmatics_region`: `eu1` (default), `us1`, `au1`, read
    through `config.normalize_speechmatics_region`; the enterprise-only
    `eu2`/`us2` are not offered. Melia 1 runs in every region (models page, read
    2026-11-24: "Melia 1 is available in all regions", `au1` batch endpoint
    listed; the page of 2026-09-27 said EU and US only), so the refusal at
    construction and its dialog warning are gone. **Unverified live**.
  - **`standard` is removed (2026-11-24; models and pricing pages read that
    day).** Melia 1's accuracy is "on par with the Standard model", batch
    list price is $0.24/h against $0.45/h, and Enhanced ($0.75/h) has the same
    features as Standard at higher accuracy. A saved `standard` moves to Melia
    1 with a notice; its custom dictionary does not carry over (Melia has
    none), so move to Enhanced if that mattered. Judgement call: Speechmatics
    still sells Standard ("for throughput"), it is not deprecated.
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
  Models (2026-11-24; read: mistral.ai/news/voxtral-3, docs.mistral.ai models
  page and `llms/studio/audio/*` pages): `voxtral-mini-transcribe-3-0`
  (default, $0.0035/min) and `voxtral-small-transcribe-3` ($0.0040/min,
  slightly more accurate), pinned rather than the `-latest` aliases.
  **Small's id was settled 2026-11-25** from the `llms/studio/audio/*` pages
  read raw: the file-transcription page's `model` field says
  "`voxtral-mini-transcribe-3` or `voxtral-small-transcribe-3`" (its
  request examples only use Mini, as `voxtral-mini-transcribe-3`), and the Voxtral 3 migration table lists
  `voxtral-small-transcribe-3` as an alias of Small, while giving its Model ID
  as `voxtral-small-transcribe-latest` (and Mini's as the dated
  `voxtral-mini-transcribe-3-0`). Only the models and languages pages write
  `voxtral-small-transcribe-3-0`; the API reference page still shows the old
  `voxtral-mini-latest`/`-2507` ids (stale). So Small is sent as
  `voxtral-small-transcribe-3`, and Mini keeps the dated id because the
  migration page documents both forms as valid for it. **Unverified live**:
  whether the service accepts each id was not tested.
  `voxtral-mini-2602` is deprecated (retirement "05/03/2027", day/month order
  not stated) and moves to Mini 3 with a notice; its migration table lists
  Mini 3 as the replacement. Same route and fields; the Voxtral 3 migration
  page lists no change to `context_bias` ("No" breaking change; entries may
  now be phrases, "no fixed number", but a request over the 65,536-token
  window shared with the audio is a 400) or to `text`; word timestamps are
  gone, which this app never asked for. Rules:
  - **53 languages** (the languages page for both models), plus Auto (no
    `language` field); a code outside it is a 422, so any other code is sent
    as Auto.
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
  - **No region (yet)**: Mistral now documents regional endpoints
    (`api.eu.mistral.ai`, `api.us.mistral.ai`, +10 % price, "Available models
    vary by region", read 2026-11-24) and the Voxtral 3 announcement says it
    is "hosted through our European, US, and global endpoints"; the global one
    still "does not commit to a specific inference location". Whether the EU
    host answers `/v1/audio/transcriptions` is unverified (no key, and no
    model list was read), so no region picker is built; it is a candidate.
  - **Parts**: 1800 s and 500,000,000 bytes ("60 minutes", "500 MB"), with a
    300 s socket timeout for the synchronous request.
  - **Not verified live**: no Mistral key exists here.
- **Meta Muse Voice Transcribe is one synchronous multipart request
  (`meta_provider.py`, 2026-11-26; page read that day:
  https://dev.meta.ai/docs/speech-to-text).** `POST
  https://api.meta.ai/v1/asr/transcribe`, `Authorization: Bearer <key>`,
  `Accept: application/json`; multipart parts `request` (JSON, sent with
  `Content-Type: application/json`) and `audio` (the WAV). The page gives the
  `audio` part no content type; the app sends `audio/wav`. Engine `meta`, label
  "Meta (Muse Voice)", one model `muse-voice-transcribe-1.0` (the page's
  example value; the launch was 2026-09-01 per the research of 2026-11-24),
  key under `meta`, no region. Rules:
  - **The `request` part carries `model`, `mode`, `audioEncoding`, and
    optionally `languageBias` and `keywords`.** `mode` is sent as
    `PUSH_TO_TALK` (the page's default; `ENDPOINTING` and `DIARIZATION` are
    not used) and `audioEncoding` as `WAV`, so no vendor default decides. The
    answer is JSON: `transcript` (string), `sessionId`, `audioDurationMs`,
    `turns` (empty in PUSH_TO_TALK); `"transcript": ""` is a valid empty
    answer, any other shape is an error (`transcript_from_json(field=
    "transcript")`).
  - **Language is `languageBias`, a list of English language names** ("a list
    of language names, such as `["English", "French"]` ... steers recognition
    but does not force a language"). The page lists no ISO codes, so
    `config.META_LANGUAGE_NAMES` maps the app's code to the name; the page's
    25 languages are offered (`META_LANGUAGE_MODES`), Auto sends no field, a
    code outside the table is sent as Auto. The model is "trained on 70+
    languages" but only these 25 are validated, so no more are offered.
    Differs from the research: the research said "a `languageBias` hint"
    without the value format.
  - **The custom vocabulary is `keywords`** (the page: a list of terms that
    "biases recognition but does not guarantee exact spelling"), up to the
    app's 100 terms. The page states no limit on the list; **unverified**
    whether a long list is refused. `meta` is in
    `config.CUSTOM_VOCABULARY_ENGINES` and its summary sentence.
  - **WAV only, mono 16-bit, 16 or 24 kHz, 10 minutes, 32 MB.** The app's own
    16 kHz recording goes out byte for byte. An imported **16-bit PCM** WAV in
    another rate or in stereo is mixed down and resampled to 16 kHz mono
    (`_wav_for_meta`, the resampler the local models use) instead of coming
    back as a 400. **Anything else is refused before sending** with one
    sentence, "Meta (Muse Voice) takes 16-bit PCM WAV; <file> is <what it is>.
    Convert it to 16-bit PCM WAV first.": an 8-bit or 24-bit WAV ("8-bit WAV
    audio"), a WAV with a float or other non-PCM format, or a file that is not
    a WAV (MP3, M4A). The app ships no decoder for them and Meta takes nothing
    else. (Until 2026-11-26 this paragraph said 24-bit was converted; it was
    refused with the local models' wording, "Only 16-bit PCM WAV audio is
    supported by this local model", and a float WAV was called "not a WAV".)
  - **The Import Audio tab says it before Start** (2026-11-26, review): with
    Meta chosen as the Import Service and a file whose extension is not
    `.wav` selected, the status line under the service reads "Meta (Muse
    Voice) takes WAV files only; convert this file or pick another engine."
    (before any key hint) and Start stays off, until the file or the engine
    changes (`_import_file_issue`, `_import_start_allowed`,
    `_WAV_ONLY_IMPORT_ENGINES`). The check is the extension only: a `.wav`
    that is not 16-bit PCM is still refused at the start, with the sentence
    above. Every place that gives Start back goes through
    `_refresh_import_start_button` (a finished import and a credential
    failure used `bool(path)` and re-enabled it for Meta plus an mp3). A
    finished import redraws the selected-file line too, so a file that
    vanished meanwhile shows "Selected file does not exist" beside the
    disabled Start.
  - **The connection test has the 15 s bound** of the other key checks
    (`_KEY_CHECK_TIMEOUT_S`); it sends one second of silence, and a real part
    keeps the transcription timeout (up to 300 s).
  - **Parts**: `META_MAX_PART_SECONDS` 590 and `META_MAX_REQUEST_BYTES`
    32,000,000 through `remote_batch_part_limit("meta", ...)` and
    `transcribe_in_parts`. 590, not 600: the page does not say whether "10
    minutes" is inclusive. Socket timeout 300 s (the page gives no processing
    time; it says only that a request that "exceeded the server budget" is a
    500 to retry once).
  - **Errors** go through the shared readers: 401 and 429 have fixed
    sentences; any other status shows the vendor's message capped at 300
    characters (`read_http_error_detail`), an HTML page (a proxy's) by its
    title, never pasted. The page defines no JSON error body; the live 401 of
    2026-11-26 (below) is `{"sessionId": ..., "error": {"code":
    "invalid_api_key", "type": "authentication_error", "message":
    "Unauthorized"}}`, which the reader unwraps. The key is scrubbed from every
    message and every echoed body (`_scrub`, before the cut).
  - **Test** sends one second of digital silence through the request code:
    the page documents no endpoint that only checks a key, and "requests that
    fail before producing a transcript, and 429 requests, are not billed", so
    a wrong key costs nothing and a right one one second ($0.00005).
  - **Live, without a key (2026-11-26)**: an unauthenticated `POST` of a
    one-second WAV to the batch URL answered HTTP 401 with the body above, so
    the host and path exist. Nothing else is verified: **unverified live, no
    key**: that a real transcript has the documented shape, that `keywords`
    and `languageBias` are accepted as written, the `audio` content type, and
    German quality.
  - **Streaming is not built**: `wss://api.meta.ai/v1/asr/realtime` cannot be
    checked without a key; the protocol and the effort are in
    `docs/ROADMAP.md`.
  - **Revert**: remove `meta` from `VALID_ENGINES` and the entries beside the
    `META_*` constants; a saved `engine: "meta"` then falls to local.
- **Soniox is a polled batch job (`soniox_provider.py`, 2026-11-26).** Engine
  `soniox`, label "Soniox", key under `soniox`, key-only. Pages read
  2026-11-26 (all under https://docs.soniox.com/): `openapi.yaml` (paths,
  schemas, statuses, error bodies), `stt/async/async-transcription`,
  `stt/async/limits-and-quotas.mdx`, `stt/async/error-handling.mdx`,
  `api-reference/errors.mdx`, `api-reference.mdx` (base URLs),
  `data-residency.mdx`, `stt/models.mdx`, `stt/concepts/supported-languages.mdx`,
  `stt/concepts/context.mdx`. The older `soniox.com/docs/...` links answer 308 to
  these. **Live, without a key (2026-11-26)**: `GET /v1/models` without a key
  answers HTTP 401 on all four REST hosts (`api`, `api.eu`, `api.jp`,
  `api.in`), so the hosts and the route exist; a real transcript has never
  been seen (**unverified live, no key**). Protocol, in the order the code
  calls it, every request `Authorization: Bearer <key>` through
  `open_request`:
  1. `POST {base}/files`, multipart `file` -> 201 `{"id", "filename", "size",
     "created_at"}`. 2. `POST {base}/transcriptions`, JSON `model`
     (`stt-async-v6`), `file_id`, optional `language_hints` (list of ISO 639-1
     codes), `language_hints_strict`, `context` -> 201 `{"id", "status":
     "queued", ...}`. 3. `GET {base}/transcriptions/{id}` until `status` is
     `completed`, `error` or `failed`: the schema's enum is `queued`,
     `processing`, `completed`, `error`, while the error-handling reference
     says `failed`; both end the wait (a status added later is waited out). A
     failed job carries `error_type` and `error_message`
     (`invalid_audio_file`, `file_not_found`, `transcription_output_too_long`,
     a 402 budget type). 4. `GET {base}/transcriptions/{id}/transcript` ->
     `{"id", "text", "tokens": [...]}`; the app reads `text` (a string; `""`
     is silence) through `transcript_from_json`. 5. `DELETE
     {base}/transcriptions/{id}` and `DELETE {base}/files/{file_id}` (204).
     Base is `https://api.soniox.com/v1`, or `https://api.<region>.soniox.com/v1`.
  - **The vendor's error body** is `{"status_code", "error_type", "message",
    "validation_errors", "request_id", "more_info"}`; `read_http_error_detail`
    unwraps `message`. 401 and 403 say the key is invalid, expired or "from a
    project in another region than the one selected" (a key works in its own
    region's project only); 402 (balance, monthly or project budget) and 429
    (`limit_exceeded`: rate, file count, file size, transcription count, pending
    count) quote the vendor's sentence.
  - **What stays on Soniox's side** (the API keeps uploads and jobs until they
    are deleted, "automatically deleted after 30 days"; limits of 10 GB, 1,000
    files, 2,000 transcriptions make leftovers a real cost): the transcription
    and the file are deleted, best effort, as soon as the job ended with a
    transcript or an `error`. **The file must outlive the job** ("deleting it
    earlier fails the transcription with `file_not_found`"), so: a job that
    could not be created deletes its file; a wait that gave up (the 30 minute
    budget, a quit, a final HTTP answer while polling) deletes **nothing**,
    because the job may still complete; a completed job whose transcript could
    not be fetched keeps both, and the error names the job id and the 30 days.
    So **when the app quits during a job, that recording's audio and transcript
    stay on Soniox for up to 30 days**. **The deletions run after the text is
    returned**, on a daemon thread (`stt_app_soniox_cleanup`) with the same
    15 s bound per request: they cost the dictation nothing (a server that
    answered DELETE slowly had held a 0.2 s dictation back by 30 s). The
    price is that **a quit while the cleanup thread runs leaves whatever it had
    not deleted yet** (the transcription, the file, or both, in that order) on
    Soniox for up to 30 days, like a quit during a job. A refused deletion is only logged
    (`soniox_cleanup_failed`, key scrubbed), never an error of the dictation.
    What Soniox keeps of a transcript is documented twice: objects are
    deleted after 30 days, and a transcript is kept "for a limited window"
    whose length no page gives; the app does not rely on either figure.
  - **The loop is `_job_poll.poll_job`, the transcript fetch
    `fetch_with_retries`** (`SONIOX_BATCH_MAX_WAIT_S` 1800 s, one poll per
    second, three consecutive failed fetches, the shutdown flag, the job id in
    every message, 401/403/404 end the wait at once; a refusal of
    `open_request` during a poll names the job). A status answer that is not
    JSON or has no status is a failed fetch, as for Speechmatics.
  - **Language**: the 60 codes of the supported-languages page, all of them
    in both APIs, are `SONIOX_LANGUAGE_MODES` (plus Auto, which sends no hint).
    A chosen language is sent as `language_hints: [code]` with
    `language_hints_strict: true` ("rely more on language hints"), because
    choosing German in the app means German; **unverified live** how strict
    changes the result. Chinese is `zh`. **The vendor does not say whether
    `stt-async-v6` or `stt-rt-v6` accept the flag** (its models page lists it
    per model, v6 is not listed; a model without it answers 400 "Model does
    not support language_hints_strict"). Batch: a 400 whose message names
    the flag is repeated once without it (hints stay, one WARNING), nothing
    else is retried. Streaming does not retry: by the time the error frame
    arrives the audio already sent is gone, so a refusal fails the session
    with the vendor's sentence (recorded as unverified below).
  - **Custom vocabulary** is `context.terms` (up to 8,000 tokens, about
    10,000 characters for the whole context, else `invalid_request`):
    `soniox_provider.fit_terms` keeps the terms that fit 8,000 characters in
    order. `soniox` is in `config.CUSTOM_VOCABULARY_ENGINES`.
  - **Model**: `stt-async-v6` only (released 2026-11-03, $0.10/h on the
    pricing page of the research); `stt-async-v5` is deprecated and "will be
    removed on Dec 15, 2026", so it is not offered. The model setting is
    `soniox_model`; saved values outside the roster fall to the default.
  - **Region** `soniox_region`: `us` (default), `eu`, `jp`, `in`, read through
    `config.normalize_soniox_region`; hosts in `config.SONIOX_API_HOSTS`. The
    data-residency page guarantees that "audio and transcripts stay in the
    selected region for both processing and storage" and that content is never
    used for training; account and usage metadata "may be processed outside the
    selected region". Region access must be requested in the Console, a
    project is created in a region and its keys work there only, so **the
    default is the US host, which every account has** (the vendor names no
    default), and the EU label says what it needs, not only what it
    guarantees. Every region picker, this one included, sits **inside its
    provider's row** beside the key field; see `docs/agents/settings-dialog.md`.
  - **Parts**: Soniox's duration limit is 300 minutes per file (fixed), no
    per-file size limit is published, and the request is built in memory, so
    `SONIOX_MAX_PART_SECONDS` 3600 and `SONIOX_MAX_REQUEST_BYTES` 250,000,000
    through `remote_batch_part_limit("soniox", ...)`: a dictation never reaches
    it, a long import is cut at quiet points; each part is its own job.
  - **The key check is `GET {base}/models`** (documented, key-protected, spends
    no audio): 200 with a `models` list is "Connection OK", a 401/403 says the
    key is invalid or from another region, a proxy page or an answer without
    `models` is not a valid key. 15 s bound.
  - **Guards** (the binding rules of this file): every request through
    `open_request`; every JSON answer through `json_from_reply` /
    `transcript_from_json` / `missing_field_error` with `redact=key_redactor(key)`;
    an upload or job answer without an `id` is an error. Tests:
    `tests/test_soniox_provider.py`, and Soniox is an engine in
    `test_rest_reply_guard.py`, `test_rest_key_scrub.py` and
    `test_provider_redirects.py` (the shared fake is `_rest_fakes.soniox_job_reply`).
    The release check (`scripts/release_check_providers.py`) calls Soniox
    automatically (it iterates `VALID_ENGINES`) and reads `soniox_region` from the
    saved settings (`REAL_SETTINGS_FIELDS`), so the owner's paid check covers it.
  - **Unverified live**: everything above that only a key could show (the
    shapes of the real answers, `language_hints_strict`, whether a WAV needs
    a content type, the part limit, German quality).
  - **Revert**: remove `soniox` from `VALID_ENGINES` and the entries beside the
    `SONIOX_*` constants; a saved `engine: "soniox"` falls to local.
- **Soniox streaming is a WebSocket session (`soniox_stream.py`, 2026-11-26;
  `soniox` is in `STREAMING_ENGINES`, so its mode label stays "Streaming
  (Experimental)").** Pages read 2026-11-26:
  `https://docs.soniox.com/api-reference/stt/websocket-api.mdx`,
  `.../authentication/websocket-authentication.mdx`,
  `.../stt/rt/connection-keepalive.mdx`. **Unverified live, no key**: no
  connection was ever opened, the protocol below is the documentation's.
  - **Endpoint and key**: `wss://stt-rt.soniox.com/transcribe-websocket`
    (`stt-rt.eu.`, `stt-rt.jp.`, `stt-rt.in.` for the region, `SONIOX_REALTIME_HOSTS`).
    The key goes in the `Authorization: Bearer <key>` header of the handshake;
    the `api_key` field of the start message is deprecated (refused after
    2027-01-15) and is never sent, so the key is in no URL and no frame.
  - **Start message** (first text frame, sent on open): `model` `stt-rt-v6`
    (`SONIOX_REALTIME_MODEL`, always, whatever `soniox_model` says: the async
    model is not a realtime model), `audio_format` `pcm_s16le`, `sample_rate`
    16000, `num_channels` 1, and as in batch `language_hints` with
    `language_hints_strict` for a chosen language and `context.terms` for the
    custom vocabulary. The capture's blocks are raw PCM16 mono 16 kHz, so no
    conversion happens.
  - **Audio** goes as binary frames (at most 2,097,144 bytes each; a larger
    chunk is split). `push_audio_chunk` never blocks the PortAudio callback
    (`put_nowait`; a full queue fails the stream, only the controller's
    preconnect flush passes `block_timeout_s`). One sender thread writes audio
    and keepalive frames, so they never interleave.
  - **Text**: each message is `{"tokens": [{"text", "is_final"}, ...]}`. A
    final token arrives once and is kept; the non-final tokens of a message are
    the whole provisional tail and replace the previous tail. The callback gets
    finals plus tail, so the text only grows as words are confirmed (the
    controller's append-only insertion locks what it needs). `<end>` and `<fin>`
    are never text. A message with neither tokens nor an error changes nothing.
  - **Keepalive**: "If no message arrives within 20 s the stream ends with a 408
    `request_timeout`", and any client message resets it, so the sender writes
    `{"type": "keepalive"}` after 5 s without audio (a handshake stall or the
    stop's backlog wait). The vendor bills the full stream duration, so a bridged
    gap costs its length; past `AUDIO_INPUT_BUFFER_S + 10` seconds without audio
    the microphone is dead and the stream ends with a message naming that.
  - **Stop**: drain the sender, send the empty text frame, wait up to 10 s for
    `{"finished": true}` (a socket that closes ends the wait too), close. The text
    received so far is always returned; a missing `finished` is logged, not
    raised. An error before any text raises with the vendor's sentence; an
    error after text returns the text (a finished transcription is never
    discarded).
  - **Errors** are a message with `error_code`, `error_type`, `error_message`,
    after which the server closes: 401/403 say the key is invalid or from a
    project in another region than the Region on the Providers tab (the host
    in brackets), 402 says the balance or budget
    is used up, the rest quote the vendor's sentence with its type and code
    (408 `request_timeout`, 413 `max_duration_reached`, 429, 503). All go to the
    controller's error callback once, key scrubbed (plain and JSON-escaped); a
    certificate failure gets the shared guidance. **A refused handshake** is
    a sentence by its HTTP status (401/403 key refused, with the Region
    hint; 402 budget; 429 too many requests or sessions; 5xx the service;
    else "the service refused the connection (HTTP N)"), never the
    library's header dump.
  - **How websocket-client reports a close** (1.9.2, `_app.py`): `on_error`
    is called for EVERY close frame of the server, a normal one included,
    with a `WebSocketConnectionClosedException` that has `status_code` and
    `reason` (a lost connection is the same class without them), and only
    then `on_close`. The vendor closes the socket itself after `finished`,
    so that is the normal end of every stop. Rules: after `finished`, or
    while stopping, a close with code 1000/1001/none is not a failure and
    only ends the wait; before the stop a close is a failure worded "the
    connection was closed by the service (code N: reason)", never as an
    HTTP status; a socket error or close after the session's own close
    (a flag set just before it) is a non-event; a real failure during the
    stop (reset, abnormal code, send error) raises when there is no text,
    and with text returns it and logs one warning that the last words may
    be missing.
  - **Sessions are objects**: `SonioxStreamSession` is one session and the
    transcriber holds the current one, so a callback of an earlier session
    cannot touch a later one. A stop or abort during the handshake closes the
    socket and makes `start_stream` raise "stopped while connecting"; nothing
    stays open.
  - **Price** $0.12/hour of stream (pricing page of the research, read
    2026-11-26), against $0.10/hour async.
  - **Tests**: `tests/test_soniox_streaming.py` (a fake `websocket` module as
    for Deepgram) and the socket-path cases of `tests/test_sdk_key_scrub.py`.
  - **Unverified live**: the shape of real token messages, that `<end>` never
    appears without an endpoint request, the real close behaviour after the
    end-of-stream frame (the library's side was run against a local
    server, the vendor's was not), how fast `finished` arrives, whether
    `stt-rt-v6` accepts `language_hints_strict` (not retried in streaming),
    German quality.
- **OpenRouter is an engine of its own on the custom endpoint's code
  (`openrouter_provider.py`, 2026-11-26; owner's decision of the round: its
  own entry and key, so "Custom endpoint" stays free for something else).**
  Pages read that day: `openrouter.ai/docs/api/api-reference/stt/
  create-transcription.md`, `.../docs/guides/overview/multimodal/stt.md`,
  `.../docs/api-reference/limits`. `OpenRouterTranscriber` is a subclass of
  `CustomEndpointTranscriber` that changes class attributes only (engine id,
  name, model-list route, missing-key text, URL-advice sentences) and the key
  check; the request, the part splitting, the error readers, the key scrub and
  the redirect guard are that class's. Engine `openrouter`, label
  "OpenRouter", key under `openrouter`. Rules:
  - **Fixed base URL, transcription style only**: `POST
    https://openrouter.ai/api/v1/audio/transcriptions`, multipart `model`,
    `file`, optional `language`, `prompt`, `response_format=json`; the answer
    is `{text, usage}`. No editable URL, no chat style, no key command (the
    constructor passes `key_command=""` and the transcriptions mode), so none
    of the custom endpoint's fields is in the OpenRouter identity
    (`_transcriber_identity` reads them for `custom` only; pinned). Sentences
    that tell a custom-endpoint user to "check the base URL" are replaced by
    ones that do not point at a field that does not exist.
  - **Model is free text with a list** (default `openai/gpt-transcribe`,
    `config.DEFAULT_OPENROUTER_MODEL`, in the live list on 2026-11-26 at
    $0.27/h per the research). No roster in `config.py`: the research of
    2026-11-24 found Scribe v3 and STT-3.7 missing from OpenRouter's list a
    week or two after launch, and dead ids still in it. A blank model is the
    default. **No prices in the picker** (the list's price fields use
    inconsistent units: per second for most, per hour for MAI, tokens for
    Gemini).
  - **The model list is `GET /models?output_modalities=transcription`**
    (public; 30 models on 2026-11-26, shape `{"data": [{"id", "name",
    "architecture", "pricing", ...}], "total_count", "links"}`, no `mode`
    field), the plain `/models` being 469 chat models. The query is part of
    the class's `_models_path`, so the base-URL validation that refuses a
    query (right for the custom endpoint) is not in the way. **Both listed
    engines share one fetch**: `SettingsDialog._fetch_models` (worker thread,
    bounded: 15 s, 2 MB) builds the transcriber of the selected engine from a
    snapshot taken on the GUI thread; `settings_store.LISTED_MODEL_ENGINES`
    names them and `listed_models(settings, engine)` reads each one's saved
    list (`custom_models` + `custom_models_endpoint` for the custom endpoint,
    keyed by its URL; `openrouter_models` for OpenRouter, which has one URL).
    The note under the Model row and the pick of the placeholder and the
    Refresh tooltip are per engine, so a Refresh of one never paints under the
    other's picker. **Refresh needs no key**: the dialog builds the instance
    with `listing_only=True`, which sends no `Authorization` header unless a
    key was entered (the live GET of 2026-11-26 carried none and answered 200).
  - **The connection test is `GET /key`**, not the model list: the list is
    public, so any key would pass it. The limits page says "To check the rate
    limit or credits left on an API key, make a GET request to
    `https://openrouter.ai/api/v1/key`" with the key as Bearer; a success is a
    `data` object. It does not say what an invalid key returns, so any HTTP
    error is reported through the shared reader and a 200 without a `data`
    object is "unexpected". **Unverified live** (a valid key was never sent;
    the unauthenticated 401 below is not this route).
    **Credit (page read 2026-11-26)**: the `data` object documents
    `limit_remaining` ("Remaining credits for the key, or null if
    unlimited": the key's own cap) and `usage`, and no account balance. When
    `limit_remaining` is 0 or below the test says "the key is valid, but its
    own credit limit is used up" (the page names the `openrouter_key_limit`
    error for it). **A valid key on an account without credit still passes**
    and its first dictation fails with HTTP 402: the balance is in
    `GET /credits` (`total_credits`, `total_usage`), which the page says needs
    a management key, so the app cannot read it and says nothing about it.
  - **Parts: 300 s and 25,000,000 bytes** (`OPENROUTER_MAX_PART_SECONDS`, the
    chat style's value, and `OPENROUTER_MAX_REQUEST_BYTES`). The page says
    "Max 25 MB" for an upload, "Upstream providers time out after 60 seconds
    per request" and to "split recordings that take more than about a minute
    to process"; it states no processing time per minute of audio, so 300 s
    (9.6 MB of the app's WAV) is a judgement: a speech model needs a fraction
    of a minute for five. A 504 or 524 shows OpenRouter's own message; the
    413 sentence names the part size. Matches the research.
  - **Custom vocabulary is the multipart `prompt`**, comma-joined as the custom
    endpoint sends it. The create-transcription page lists the field ("Context
    to guide transcription on providers that support contextual prompting")
    and the guide says it is "forwarded on OpenAI, Azure OpenAI, Groq, and
    Together", mapped by Alibaba, used by AssemblyAI only with prompted
    pricing, and "other providers ignore it". So the Transcription tab's note
    says only some models behind OpenRouter use it. `keyterms[]` was not used:
    a model that cannot use it answers 400 ("Requests fail with 400 if the
    model can't use keyterms").
  - **Language** is the app's ISO code as `language` ("An ISO-639-1 code"), or
    no field for Auto; every code the app knows is offered, as for the custom
    endpoint.
  - **Data**: the Providers row's tooltip says that the audio is sent to
    OpenRouter, which forwards it to the vendor of the model picked. The
    research found the privacy page silent on audio; no region or retention
    claim is made.
  - **Live, without a key (2026-11-26)**: the model list answered 200 with 30
    models (reply saved in the lead's scratch folder, not in the repository);
    `openai/gpt-transcribe` is among them. An unauthenticated `POST` of a
    one-second WAV to `/audio/transcriptions` answered HTTP 401
    `{"error": {"message": "No cookie auth credentials found", "code": 401}}`,
    so host and path exist. **Unverified live, no key**: a real transcript, the
    `prompt` and `language` behaviour per model, the `/key` check, a part over
    60 s on a slow model.
  - **Revert**: remove `openrouter` from `VALID_ENGINES` and the entries beside
    the `OPENROUTER_*` constants, `openrouter_provider.py`, the factory branch
    and the settings fields; the custom endpoint is unchanged by it (the
    refactor commit only moved literals into class attributes).
- **A remote batch recording past its engine's limit goes out in parts, cut
  at quiet points (2026-09-27).** OpenAI/Groq refuse >25 MB (~13 min of
  16 kHz WAV). `config.remote_batch_part_limit(engine, model, api_mode)`
  is the single answer (engine seconds + byte cap, the custom endpoint's chat
  style tighter; vendor figures beside the constants; `model` is unused since
  the two token-capped OpenAI models went, on 2026-11-24, and kept so that no
  caller changes).
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
    Azure and the custom endpoint, and part of their runtime identity) and
    not found speech-free by the Silero check (`silero_vad.check_speech_wav`,
    both scans, whole part, run on the worker thread; 2026-10-03) -- or
    unmeasurable -- leaves `[no text returned for m:ss-m:ss]` in its place
    (adjacent gaps share one marker; start rounded down, end up, so a
    short tail never reads `3:00-3:00`) and logs `remote_audio_part_empty`
    at WARNING; a silent one is skipped. `transcript_has_gap`
    (`transcriber/base.py`, beside `gap_marker`) makes the controller keep
    such a recording's audio, so with `save_last_wav` off the stretch the
    marker names is not deleted with the rest (found by the review of
    a1e0b01). The dictation roads (a dictation or a retry, foreground or
    background, all through `_on_transcription_ready`) keep it in a copy in
    the recordings folder that the History entry links
    (`_keep_audio_of_a_delivery`, `docs/agents/controller-and-jobs.md`, made
    even with "Archive every recording", whose files a count prunes), and so
    does the import of the managed last recording (`transcribe_audio_file`);
    only where that copy could not be written or linked is the managed
    recording marked failed instead, which was the first design on every road
    and keeps the audio only until the next dictation replaces the slot
    (follow-up of the round-3 review, 2026-10-23). The startup check
    `main._complete_unless_gap` spares such a failed recording whenever its
    transcript has a gap: it finds the transcript in history and used to
    complete -- i.e. delete -- the kept recording on the next launch (review
    of 384f84f). Such a recording has no Retry (its overlay is Done); History
    reaches the linked copy and Import the managed recording.
    Failing used to throw away every other part's minutes of speech; skipping
    unmarked would hand back a transcript with a hole that reads complete,
    and the marker is the one channel that reaches the overlay, the document
    and history without a new controller path. Only a recording of which no
    part returned text while one held sound fails, the single request's
    "Empty model text is a failure" rule. The Silero
    check was added on 2026-10-03 as a second judge behind the level, not
    instead of it: a false "sound" costs a marker to delete, a false
    "silent" costs speech without a trace, so it turns "sound" into "silent"
    only when both scans of the whole part stay below the cut (and the
    provider already returned nothing for that part). A speech-run check
    (`vad.measure_longest_speech_run_s`) was considered and left out.
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
    so whether STT-3.7-Realtime streams there is open; the streaming
    data-zone page (read 2026-11-24) lists `streaming.eu.assemblyai.com`
    without a per-model note.
- **Fun-ASR host: something to watch (2026-11-25).** Alibaba's WebSocket guide
  now lists the Singapore endpoint as
  `wss://{WorkspaceId}.ap-southeast-1.maas.aliyuncs.com/api-ws/v1/inference`
  (a workspace-specific host, `{WorkspaceId}` being the user's own Workspace
  ID) and says to migrate from `dashscope-intl.aliyuncs.com` to it, without
  announcing a retirement of the old host. `FUNASR_WS_URL_INTL` still uses
  `dashscope-intl.aliyuncs.com`; changing it would need a new setting for the
  Workspace ID, so nothing was changed. If Fun-ASR requests start failing to
  connect, this is the first thing to check. **Unverified live.**
- **Deepgram offers `nova-3` only; Groq is unchanged (2026-11-24).**
  Deepgram's models page (read that day) still sells `nova-2` "for use cases
  with languages not yet supported by nova-3", but all 34 languages the app
  offered for nova-2 are in nova-3's list (compared against the page's
  tables), so nova-2 is removed and a saved choice moves to nova-3 with a
  notice. That also drops the `keywords` vocabulary parameter nova-2 needed;
  nova-3's `keyterm` is the only path. Nova-2's filler-word identification is
  not used by the app. Flux (`flux-general-en`, `flux-general-multi` with ten
  languages) is a conversational voice-agent model and not offered. Groq's
  speech-to-text page and deprecations page (read that day) list only
  `whisper-large-v3` and `whisper-large-v3-turbo` as current (the `-en`
  variants were retired 2025-08-23), which is what the app offers.
  - **Deepgram keyterm limit (2026-11-25, keyterm page).** "Key Terms are
    limited to 500 tokens per request"; an over-limit pre-recorded request
    or streaming connection returns "Keyterm limit exceeded". The app's
    100-term cap is not a token cap, so `deepgram_provider.fit_keyterms`
    keeps the leading terms whose estimate fits `DEEPGRAM_KEYTERM_TOKEN_BUDGET`
    (400, a 20 % margin) and drops the rest from the end, with one warning in
    the log per transcriber. Deepgram's tokenizer is not public; the estimate
    is one token per term plus one per three characters of each word, which
    over-counts common words. With 12-letter terms that keeps 80 of 100.
    The page also advises 20 to 50 terms in practice. **Unverified live**:
    the real tokenizer may count differently.
- **Deepgram streaming holds its socket open with a KeepAlive while no audio
  goes out (2026-10-17).** Since 2026-10-10 a starved machine may hand the
  capture's audio over up to 12 s late, so the socket is open and silent for
  that long. Vendor pages read 2026-10-17 (developers.deepgram.com/docs/):
  `audio-keep-alive`, `stt-troubleshooting-websocket-data-and-net-errors`,
  `finalize`, `close-stream`. Rules:
  - **Facts**: the message is the text frame `{"type": "KeepAlive"}` ("sending
    it as binary may result in incorrect handling"), no reply; "Send a
    `KeepAlive` message every 3-5 seconds to prevent the 10-second timeout
    that triggers a `NET-0001` error"; "If no audio data or `KeepAlive`
    messages are sent within a 10-second window, the connection will close
    with a `NET-0001` error" (WebSocket close 1011). A newer 1011 `NET-0002`
    (close reason `no_audio_timeout`) is a no-audio timeout, and the
    troubleshooting page's NET-0002 checklist says "Unlike NET-0001,
    KeepAlive messages do reset this timeout" (link markup omitted): the
    vendor's own evidence that a KeepAlive-only gap *after* audio has gone
    out holds. The page does not give the NET-0002 window's length (checked
    again 2026-10-17: no number appears on it). Not stated on any of the four
    pages: whether a KeepAlive may be sent while audio flows, and how
    KeepAlive meets `Finalize` or `CloseStream`.
  - **The sender thread writes it** (`_stream_send_worker`): its queue read
    waits at most `_STREAM_KEEPALIVE_INTERVAL_S` (3 s, the page's example and
    the low end of its 3-5 s, so a late sender thread still beats 10 s), and
    an empty wait is the signal. That keeps three properties without a lock
    or a clock: audio that keeps coming never reaches the interval, so no
    KeepAlive is sent while audio flows; a KeepAlive and an audio frame are
    written by the same thread and cannot overlap; and the sender barrier
    that precedes `Finalize` and `CloseStream` (the sentinel ends the loop)
    ends KeepAlives before those are sent. A timer thread would break the
    second and third; `_send_stream_control`'s helper thread was not reused
    because it gives up waiting after 1 s while its send may still be in
    flight. A failed KeepAlive fails the stream like a failed audio send; a
    session that was retired stops the loop instead of writing to its socket.
    The first idle moment of a session is logged at INFO.
  - **A session that has sent no audio yet gets one frame of digital silence
    instead of its first KeepAlive** (decided 2026-10-17 by the lead session,
    cheap enough not to wait for a live check). The two vendor pages disagree
    on whether KeepAlive alone holds a stream open before its first audio:
    the troubleshooting page's
    NET-0001 checklist says "you must send at least one audio message". When
    the sender's first 3 s wait runs out and no audio frame has gone out on
    the session, it writes `_STREAM_SILENCE_FRAME` -- PCM16 zeros, 0.1 s
    long, built from `AUDIO_SAMPLE_RATE`, `AUDIO_CHANNELS` and linear16's
    2 bytes per sample, the same constants as the connection URL's
    `sample_rate` / `channels` / `encoding` -- as a binary frame from the
    same sender thread, and KeepAlives from then on. That satisfies both
    readings for about 3 kB of billed audio (0.1 s) per start stall. Silence
    is never written once audio has gone out (the sender's `audio_sent`
    flag): a gap inside a dictation gets KeepAlives only, so nothing is ever
    inserted into speech. Deepgram's word timings follow the audio sent, not
    the wall clock (the Audio Keep Alive page's "Word Timings" section), so
    the 0.1 s only moves timestamps the app does not use.
  - **One gap is bridged for at most `_STREAM_KEEPALIVE_MAX_GAP_S` (30 s),
    then the sender ends the stream itself** (2026-10-17, lead's decision,
    revised the same day: the first version stopped writing and waited for
    Deepgram to close the socket). Nothing in the app notices a microphone
    that stops delivering after its first block: the first-callback watchdog
    watches only the first block, and `_on_audio_device_change_settled`
    defers its refresh while a recording runs (checked in `controller.py`
    and `audio_capture.py`, which registers no stream-finished callback).
    Before the KeepAlive, Deepgram's own close after ~10 s was the only
    alarm, shown as the red streaming error; an unbounded KeepAlive would
    have turned that into an overlay that says "Listening" until the hotkey.
    The bound is `AUDIO_INPUT_BUFFER_S` (20 s: PortAudio holds that much for
    a merely starved callback thread and delivers it as a burst, so a gap
    that short can be a slow microphone) plus a 10 s margin; the longest wait
    the app itself creates is 12 s (the start watchdog's hard limit, a stop's
    backlog wait), and past the buffer the audio is lost anyway. Counted in
    whole 3 s waits since the last real audio frame (no clock): ten
    KeepAlives go out, and the 11th empty wait, **33 s after the last block**,
    is the cap. There the sender logs a WARNING, records a
    `TranscriptionError` through `_record_stream_error(..., notify=True)` --
    the path a failed send takes -- and returns. The user reads "Deepgram
    streaming failed: The microphone delivered no audio for more than 30
    seconds, so the stream was ended." (the number is the constant), and the
    existing streaming-failure path runs (`docs/agents/streaming.md`, first
    entry): once, because the session's report latch allows one, and the
    sender is gone, so nothing follows it. The end is independent of
    Deepgram: ending by silence and waiting for the vendor's close would have
    made it depend on the NET-0002 window (not documented) or on a proxy that
    keeps the TCP connection open, and shown the vendor's close reason
    instead of the cause the app knows. There is no resuming: the stream is
    over at the cap, and the controller keeps the recording for Retry. The
    silence frame needs no cap of its own: it goes out only at the first
    empty wait of a session that has sent no audio, never again. (It does
    not reset the count, so a session that never gets a block would reach the
    same cap 33 s after its sender started; the capture's first-callback
    watchdog aborts such a start at its 12 s hard limit, long before.)
  - **Not verified live**: no Deepgram key exists here, so whether KeepAlive
    alone would have been enough is open; the silence frame makes the answer
    not matter for correctness (`known-limitations.md`, audio capture, has
    the optional check that would let the frame be dropped).
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
    An unresolvable name stays as typed for the "not found" message. The
    lookup order approximates CreateProcess's (`_createprocess_search_path`:
    the application's directory, `System32`, the Windows directory, then
    PATH); `shutil.which` searches the current directory first, where
    CreateProcess does so after the application's directory and not at all
    when `NoDefaultCurrentDirectoryInExePath` is set, so a same-named tool in
    the current directory can still win. A file `PATHEXT` finds that Windows
    cannot start (`.vbs`, `.js`, `.py`, `.msc`) is refused by
    name with "call its interpreter explicitly" (2026-10-03).
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
    the child's own output and exit code returned. A child that exited 0
    with nothing on stdout may have left its tool to print the token (`start
    /b`, `Start-Process` without `-Wait`), so it gets a longer grace,
    `config.CUSTOM_KEY_COMMAND_LATE_TOKEN_GRACE_S` (3 s; a token 2 s late is
    returned). After it the tree is ended and the empty output returned, so
    the error is "printed no token (exit code 0)" plus the helper's stderr
    tail, not "did not finish within 30 s" after the whole timeout with the
    token lock held (2026-10-03: `_PipeReader` threads collect the bytes, so
    "nothing yet" is told from "complete"; `communicate` could not). A wrapper
    that prints a notice and then lets its tool print the token is still
    returned as the notice.
    Without a job (a nested job forbids one) the orphan survives the
    `taskkill`, and the output collected so far is returned instead of a
    timeout. The job has no
    kill-on-close limit, so a helper that detached a daemon from its stdio
    leaves it running. POSIX does the same through `killpg` on the child's
    session, which reaches the group after the leader exited.
  - **The key and a command's token must be visible ASCII**
    (`_header_unsafe`, 2026-10-01): `http.client` refuses a header value
    with a line break and puts the whole value -- `Bearer <key>` -- into
    its `ValueError`, which reached the overlay and the log. Such a key is
    refused at construction without being echoed, and `_other_error`
    replaces any "invalid header" message.
  - **Redirects go through `_http_utils.RedirectGuard`** (2026-10-01, moved
    to the shared helper 2026-11-26, see "Every REST request that carries a
    key" above); tests patch the module's `open_request`.
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
    editable only for the engines of `LISTED_MODEL_ENGINES` (`custom` and, since
    2026-11-26, `openrouter`, whose own entry above describes what differs),
    "Refresh" lists `GET {base}/models` on a
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

