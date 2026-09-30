# Remote providers: design decisions

Binding project rules, moved verbatim from `AGENTS.md` on 2026-09-30 so that
the always-loaded agent file stays small. Read this file before changing
`transcriber/*_provider.py`, remote batch splitting and provider error handling. Entries keep their original order, so "the entry above/below" still
refers to this file; "Known limitations" is `docs/agents/known-limitations.md`.
History is in `docs/learning-log.md`.

- **AssemblyAI pre-recorded model selection**: use the current `speech_models`
  parameter for batch/import requests. `universal-3-5-pro` is sent alone when
  selected; never silently add `universal-2` as a fallback. Legacy
  `universal-3-pro`/`best`/`nano` settings migrate to the current default and
  are not shown in the UI.
- **No remote batch wait may be unbounded, and the AssemblyAI poll must fetch
  a status, not call something that waits.** The SDK's
  `Transcript.wait_for_completion` is `while True:` around a status fetch
  with no bound of any kind, so a job the service leaves in `queued` never
  returns. That held the single `max_workers=1` transcription worker for the
  rest of the session *and* stopped the app from exiting, which is the half
  that is easy to miss: `ThreadPoolExecutor` registers an exit handler --
  through `threading._register_atexit`, not `atexit.register`, so a grep
  for the latter finds nothing -- that **joins** its worker threads, and
  `shutdown(wait=False,
  cancel_futures=True)` does not release one that has already started
  (measured -- the interpreter never exits). The leftover process still holds
  the single-instance lock, so the user cannot even restart the app.
  **The first fix bounded nothing, and this entry said it did for a month.**
  `_wait_for_transcript` polled with `Transcript.get_by_id`, which in SDK
  0.64.33 is `cls(transcript_id=...).wait_for_completion()` -- the very loop
  above -- so the deadline sat *around* an unbounded call: measured still
  blocked after 12.0 s on a 3.0 s budget, 46 polls inside one call.
  `_fetch_transcript` now goes through `api.get_transcript(http_client, id)`
  and `Transcript.from_response`, the one-request fetch the SDK's own loop is
  built from, and `_wait_for_transcript` loops over that against
  `ASSEMBLYAI_BATCH_MAX_WAIT_S`. Three properties are load-bearing: terminal
  status is the *positive* test, so a status this SDK version does not know
  is waited out rather than mistaken for a finished job; a submit that
  returns no transcript id fails at once instead of spending the whole budget
  fetching an empty id; and the poll deliberately does **not** honour the
  cancel check, because abandoning a transcript the service will finish would
  break "a finished transcription is never discarded". Individual HTTP calls
  were already bounded by the SDK's own `settings.http_timeout` (30 s); only
  the loop around them was not. Two tests guard the shape rather than the
  outcome: one runs the fetch against the *installed* SDK with a stub that
  raises on a second poll, so a fetch that waits fails instead of hanging the
  suite; the other's fake SDK poisons `get_by_id` outright.
- **A Fun-ASR final with no text ends the sentence with the last partial,
  and the receive loop gives up on a flood of frames that are not events.**
  `result-generated` with `sentence_end` true and empty `text` used to reset
  `current` after an `if text: append` that did nothing, so the partial that
  preceded it was gone and the transcript came back truncated *as a clean
  success* -- no error, no recovered-text suffix, because nothing had
  failed. Measured: partial 'Hallo', empty final, `task-finished` -> ''. A
  final with text still replaces the partials it refines. Separately,
  `_recv_event` skipped binary, non-JSON and non-object frames with no
  bound but the thirty-minute budget; a real socket blocks in `recv`, so
  only a flooding peer can make it spin, but then it pinned a core and the
  single transcription worker for the whole budget (measured 1.37 million
  receive calls in 0.31 s against an instant fake).
  `_MAX_UNUSABLE_FRAMES` consecutive unusable frames now fail the request
  with the text received so far. **The bound counts across the transcript
  loop, not per call.** The first version kept the counter inside
  `_recv_event`, which `_collect_transcript` calls once per *event*, so a
  peer alternating one JSON object that is not an event with any junk reset
  it every call and the spin was back -- an empty object, or an unknown
  event name, counts as unusable too. `_UnusableFrameBudget` is created per
  transcript and reset by `result-generated` (the two terminal events end
  the loop). **It is spent only on a frame classified as unusable** -- by
  `_recv_event` for what is not a JSON object, by the transcript loop for an
  object that is not one of its events. Spent on every frame *before*
  classification, it consumed the frame that would have reset it: exactly
  `_MAX_UNUSABLE_FRAMES` junk frames followed by a real `task-finished`
  reported the transcription as "1001 frames in a row that were not events"
  (measured). **A `result-generated` resets the bound only when it carried
  something.** The reset ran on the header name, before `_sentence_from`,
  so a flood of `result-generated` frames with no payload, a non-object
  payload or an empty non-final sentence spun for the whole thirty-minute
  budget with the bound in place (measured against an instant fake, 1.4
  million receive calls in 2 s); such a frame is spent like junk. The
  second version gated on `text or sentence_end`, and an empty *final*
  with no partial pending, a whitespace-only partial and a flat empty
  `output.text` all passed it -- each changes nothing, and each spun the
  same way (1.0-1.4 million in 3 s). The gate is "carries text, or ends a
  sentence that has one": the text is stripped first, an empty final with
  nothing pending is junk, and one closing a partial is an event. A
  documented heartbeat still resets it however many arrive: it is the
  server saying it is alive, on a parameter this provider asks for, and
  refusing to count it would fail a long recording on its own keepalive.
  **The frames of one request are bounded in total**
  (`_MAX_FRAMES_PER_REQUEST`, 1,000,000, counted by
  `_UnusableFrameBudget.received` on every frame `_recv_event` reads):
  the consecutive count is reset by *any* event, so the same partial
  repeated, two partials alternating, a real final after each thousand
  junk frames and heartbeats without end all ran to the thirty-minute
  deadline with a core and the single transcription worker pinned
  (measured against an instant fake: 0.9-1.4 million receive calls in
  3 s) -- "the one flood it does not bound is heartbeats", as this entry
  and the budget's docstring said for one round, named one of four. A
  thirty-minute recording at ten results a second plus a heartbeat a
  second is 20,000 frames; the bound is fifty times that, and an instant
  fake reaches it in 2.2-3.5 s depending on the machine (1,000,002
  receive calls, exact). The `task-started`
  wait and the
  connection test use budgets of their own, so the total is per budget,
  i.e. per transcript loop. **The transcript itself is bounded
  (`_MAX_TRANSCRIPT_CHARS`, 2,000,000).** The frame bound cannot cover a
  flood of *usable* finals -- each one is the service saying something --
  so `finalized` grew for the whole budget (measured: 111 MB of heap in
  2.5 s, about 80 GB over the budget -- not the 100 GB this entry first
  said, which that pair does not give). A recording that fills the budget
  at a fast 200 words per minute and 8 characters per word carries 48,000
  characters, so the cap is forty times the plausible maximum; the count is
  a running total, because a `sum()` per frame is quadratic in exactly the
  flood it exists for; it counts the join's separators, because the bound
  is on the transcript handed back and two million one-character finals
  returned 3,999,999 characters without them -- including the separator
  before a pending partial, which the first count left out, so a session
  ending on a partial handed back the cap plus one (measured with the cap
  at 40: 20 + 20 returned 41); and the failure carries the
  recovered-text suffix
  like every other exit. The `task-failed` detail is capped at 300
  characters like every HTTP provider's error body, and a nested error
  object in it is unwrapped through `_http_utils.nested_error_text` rather
  than `str()`-ed (see the nested-error entry). Fields are typed
  before they are trusted: a `text` that is not a string is "", and
  `sentence_end` is a sentence end only when it is `True`, because a number
  or a list there reached `str.strip` and `bool()` before. The receive loop
  also ends when `transcription_shutdown_requested()`. **A `result-generated`
  whose `sentence.heartbeat` is `True` is skipped** (`_is_heartbeat`): the
  vendor's Paraformer server-events page documents the field as "If true,
  you can skip this result (heartbeat packet)." and nothing more -- the
  `sentence_id` this entry once attributed to that page is on neither it
  nor the Fun-ASR page -- and this provider asks for heartbeats, so such
  packets are expected on a long pause; read as a
  sentence, one carrying `sentence_end` would close the pending partial
  early and the real final would then be appended a second time. Which
  fields a live heartbeat packet carries is unverified from here; it still
  resets the frame bound, because it is the server saying it is alive.
  The run-task also asks for `heartbeat: true`, which the Paraformer
  client-events page documents as optional, default false: "true: Keeps
  the connection alive when only silent audio is being sent. false
  (default): Even when silent audio is continuously sent, the connection
  times out and closes after a period of time." -- with, *before* those
  two sentences on the page, an "Important" callout: "Only Paraformer v2
  supports this parameter." (This entry once quoted the callout after
  them inside one pair of quotation marks and dropped "Whether to enable
  heartbeat packets. Default: false." without an ellipsis.) The Fun-ASR
  real-time page does mention `heartbeat`, and recommends it, in two
  sections that name no model: "Set a heartbeat to keep the connection
  alive: To maintain a long-lived connection with the server, set the
  heartbeat parameter to true. The connection to the server then stays
  open even when the audio contains no sound for a long time." and, in
  its FAQ, "Implement client-side reconnection and enable the heartbeat
  parameter (heartbeat=true) to prevent the connection from dropping when
  there is no audio for a long time." -- this entry said for one round
  that the page "lists no `heartbeat` at all"; both pages were re-read
  here. And this
  provider's upload is unpaced (a tight `send_binary` loop), so a pause in
  the recording is *sent* in milliseconds whatever its length. Whether the
  service ignores, honours or rejects the parameter is **unverified
  against the live service from here** -- a rejection would show as a
  `task-failed` on every request, and the CLOSE-frame handling is what
  bounds the damage if it is ignored. A duplicate finalized sentence is
  deliberately *not* de-duplicated: nothing shows the service re-delivers
  one, and a user can say the same sentence twice.
- **One failed AssemblyAI status fetch does not abort the batch wait.** A
  read timeout or a 5xx inside the poll used to propagate straight out of
  `_wait_for_transcript` into the generic `except Exception`, which reported
  it *without the transcript id* -- the one thing that would let the job be
  recovered -- while the service went on transcribing. Fetch failures are
  now retried across `ASSEMBLYAI_MAX_CONSECUTIVE_FETCH_FAILURES` (3)
  consecutive failures one polling interval apart, the count resets on a
  successful fetch, and the message names the id; a persistent fault (a
  revoked key answering 401 forever) therefore fails in under a minute
  instead of spending the budget. `_configure()` and `_get_aai()` moved
  inside `transcribe_batch`'s `try`, because everything else in there is
  wrapped and those two escaped as raw `AttributeError`s. Two test-fake
  rules learned here: the fake-clock guard sat inside the patched
  `time.sleep`, so a loop that stops sleeping makes it unreachable and hangs
  pytest instead of failing it (measured 23.5 million iterations in 2 s,
  guard never fired) -- the pending fake now also caps *fetches*, and one
  test pins the sleep between fetches; and `FakeStreamingClient.on`
  overwrote a handler where the real SDK appends to a list, which would have
  hidden an accumulate-vs-replace regression.
- **The batch poll ends when the app quits, and the real bound is the budget
  plus one request.** `ThreadPoolExecutor`'s exit handler joins its worker,
  so a poll that ignored shutdown held the interpreter for the rest of its
  30-minute budget after the tray icon was gone -- with the single-instance
  lock still held, so the app could not be restarted either.
  `transcriber/base.py` owns an app-wide `_SHUTDOWN` event
  (`request_transcription_shutdown`, set as the first statement of
  `DictationController.shutdown`); the AssemblyAI poll reads it at the top
  of its loop and inside `_sleep_between_fetches`, which sleeps in
  `ASSEMBLYAI_SHUTDOWN_POLL_S` slices, so a quit ends the wait within one
  slice and the error names the transcript id and its last status. The
  Fun-ASR receive loop reads the same flag. **The flag is once per process
  by design, so nothing in `src/` clears it, and `tests/conftest.py` resets
  it around every test** (`_reset_the_transcription_shutdown_flag`): nearly
  every controller test ends in `shutdown()`, and without the reset the flag
  leaked into every later provider test's loop -- 26 failures in the full
  suite that no single-file run showed, because no single file runs a
  controller test before a provider test. A fetch that returns an object
  without a `status` is a fetch failure, not a job "still waiting":
  `status = fetched.status` sits inside the retry `try`, where before it
  raised `AttributeError` past the whole wait. And the bound is
  `ASSEMBLYAI_BATCH_MAX_WAIT_S` plus one SDK `http_timeout` (30 s), because a
  request in flight when the deadline passes is not interrupted; the
  docstring states the bound that way rather than as the budget alone.
- **A nested provider error object is unwrapped, never `str()`-ed.**
  ElevenLabs' documented shape is `{"detail": {"message": ...}}` (read from
  the vendor's error page), and the key loop found `detail`, a dict, and
  handed the user Python dict syntax with the request id in it, capped
  mid-dict. `read_http_error_detail` tries the nested object's `message`,
  `detail`, `status` and `code` before falling back to the JSON text; a
  number or a list falls back to the JSON text as well, which reads better
  than one field. It is also **not idempotent** -- the body is a stream, a
  second call on the same `HTTPError` returns the status phrase -- and every
  call site calls it exactly once; do not add a second read. **The rule is
  not HTTP-only, and Fun-ASR broke it**: a `task-failed` header whose
  `error_message` is `{"message": "quota exceeded", "request_id": "abc"}`
  reached the user as Python dict syntax with the request id in it, capped
  mid-dict. `nested_error_text` is the one unwrapping order (`message`,
  `detail`, `status`, `code`) shared by `read_http_error_detail` and
  Fun-ASR's `_failure_detail`, which is reached through a parsed WebSocket
  frame and never through an `HTTPError`. That one takes *both* header
  fields: `error_message or error_code` let a blank message -- `"   "`, or
  `{"message": ""}` -- hide the code the service sent (measured: a bare
  "Fun-ASR task failed." for a header carrying `Throttling.RateQuota`), so
  the message's text is asked first, the code's second, and the JSON dump
  of an object with no text field last. It is deliberately uncapped: the
  HTTP readers cap at 300 and Fun-ASR at `_FAILURE_DETAIL_MAX_CHARS`, and a
  helper that capped as well would silently apply the tighter of the two.
  The last resort is `json.dumps(..., ensure_ascii=False)`, for the same
  reason the HTTP reader falls back to the body text.
- **A provider error message is never built from `HTTPError.reason`.** That is
  only the status phrase -- "Bad Request" -- and it drops the one part that
  says what to change: OpenAI's "Invalid file format", ElevenLabs' quota text,
  Deepgram's rejected parameter, AssemblyAI's "out of credits". Azure alone
  read the body; `_http_utils.read_http_error_detail` / `http_error_suffix` is
  the single reader for all five. The detail is capped at 300 characters so a
  provider cannot push an HTML error page into a dialog, and it falls back to
  the status phrase when the body is empty or its read raises.
- **Every REST call passes `create_ssl_context()`.** AssemblyAI's
  `test_connection` was the one that did not, so behind a TLS-intercepting
  proxy the connection test failed while transcription itself worked: the SDK
  goes through `requests`, which reads `REQUESTS_CA_BUNDLE`, and `urllib` does
  not. Its hand-written message then told the user to set exactly the variable
  that could not fix the call that had just failed; `format_ssl_error_message`
  is the shared text and names `SSL_CERT_FILE` too.
- **ElevenLabs batch model selection**: `scribe_v2` is the only supported model.
  ElevenLabs removed `scribe_v1` on 2026-07-09; legacy stored selections migrate
  to `scribe_v2` and the removed identifier must not be sent to the API.
- **The Azure model roster is Microsoft's table, and an Azure engine that was
  never configured moves to the current default once (schema 24)**
  (2026-09-19). "Azure LLM Speech" is the service and MAI-Transcribe the model
  behind it, and Microsoft offers no MAI API outside Azure, so MAI is not a
  second provider to add: the engine gained `mai-transcribe-2` (announced
  2026-09-03, 60 languages) as its default, keeps `mai-transcribe-1.5` (43
  languages; `zh` had been missing from the app's list) and keeps
  `mai-transcribe-1` selectable although Microsoft's page marks it "Deprecated
  on Aug 20, 2026" -- deprecated is not removed, whether the service still
  answers for it is not documented, and its label says so. Rules:
  - **Settings store the lower-case id; `AZURE_API_MODEL_NAMES` is what is
    sent.** Every example on Microsoft's page writes `MAI-Transcribe-2`, and
    whether the service compares case-insensitively is not documented, so the
    documented spelling goes out -- for the two older models as well, which
    were sent lower-case until now.
  - **The language lists are that page's table in the app's codes**
    (`AZURE_LOCALE_OVERRIDES`: `no` -> `nb`, `tl` -> `fil`), and a test
    compares the codes the provider would *send* with the documented set per
    model, in both directions. That comparison is what found the missing `zh`.
  - **Schema 24**: every file written before it carries `mai-transcribe-1.5`
    for everyone, so the stored value is not a choice; without an
    `azure_endpoint` the engine could never run, so nothing was chosen, and
    such a file adopts the default once. A file with an endpoint keeps its
    model, and so does every file saved from now on. It matters because of the
    price: Microsoft's announcement gives MAI-Transcribe-2 "$0.10 per hour as a
    limited-time offer until the end of the year" (2026; the price afterwards
    is not announced) against $0.36/hour for 1.5. An older build reading a
    schema-24 file does not know the id and falls back to its own default.
  - **Not verified against the live service.** No Azure resource was available
    when the integration was written or now; the keyring holds AssemblyAI and
    Groq keys only. The request follows the documented contract, and
    `docs/azure-llm-speech.md` says so at its top.
  - The service also offers diarization, word timestamps, a phrase list and a
    "clean" transcript style. The app sends none of them, so custom vocabulary
    stays unwired for Azure.
- **`gpt-transcribe` is OpenAI's current model, the app's default, and the
  only one of the four with a different request shape (schema 25,
  2026-09-21).** `POST /v1/audio/transcriptions` accepts five ids; four are
  offered, `gpt-4o-transcribe-diarize` is not, because the app has no speaker
  UI. OpenAI's deprecations page: `whisper-1`, `gpt-4o-transcribe`,
  `gpt-4o-mini-transcribe` and the diarize model were notified on 2026-08-26
  and are removed from the API on 2027-02-26, with `gpt-transcribe` as the
  replacement for this endpoint (`gpt-live-transcribe` is realtime sessions
  only and is not integrated). The three older models stay selectable and
  their labels carry that date. Rules:
  - **The request branches on `OPENAI_ARRAY_FIELD_MODELS`, not on the
    default.** `gpt-transcribe` sends `languages[]=<code>` and never the
    singular `language` (the guide: "languages replaces the singular language
    field. Don't send both fields.") and one `keywords[]` per vocabulary term;
    the three older models send `language` plus the comma-joined `prompt`
    exactly as before, pinned by a test that parses the encoded multipart
    body.
  - **A term holding `<`, `>`, CR or LF is dropped rather than sent**: "The
    API rejects the entire request when it encounters one of these
    characters", so one bracket would cost the whole dictation. The drop is
    logged once per request as a count at INFO and never as the terms, which
    are the user's own text. `parse_custom_vocabulary` splits on newlines but
    not on carriage returns, so the check is not redundant.
  - **The language list is the engine's for all four models**: the guide
    enumerates none for `gpt-transcribe`, only the code formats it accepts.
  - **Schema 25 mirrors the Azure rule with `has_openai_key` in the
    endpoint's place**: every file written before it carries
    `gpt-4o-mini-transcribe` for everyone, and without a stored key the engine
    could never run, so such a file adopts the current default once. A file
    that shows OpenAI was configured keeps its model, and so does every file
    saved from now on. Two accepted edges: an older build reading a schema-25
    file normalizes the unknown id to its own default, and a file saved while
    the keyring was unreadable carries `has_openai_key: false` and adopts the
    default although a key exists -- the cost is a switch to the recommended
    model, not a failure.
  - **Not verified against the live service**: no OpenAI key exists on the
    development machine. The request follows the guide's own curl example.
- **Soniox and Mistral's Voxtral API were evaluated and not added
  (2026-09-21).** The owner's rule: a new provider is worth its maintenance
  only when it is both more accurate and cheaper than what is offered. Read
  on Artificial Analysis' English leaderboard that day, MAI-Transcribe-2
  (2.0% at $0.10/h) and ElevenLabs Scribe v2 (2.2%) lead both; Soniox (3.8%,
  $0.10/h) and Voxtral Small (2.8%, $0.24/h) beat only OpenAI's older models
  and Deepgram Nova-3 on both axes. Soniox's $0.12/h realtime price is the
  one open argument, for streaming. `docs/provider-costs.md` and
  `docs/local-asr-model-candidates-2026.md` carry the figures and links.
  Free plans were checked the same day: Groq's free plan is the only
  recurring free quota among the integrated providers that covers daily
  dictation, and Azure's F0 tier does not cover LLM Speech ("Not applicable"
  on Microsoft's quotas page), which the docs had claimed.
- **A remote batch recording past its engine's limit goes out in parts, cut
  at quiet points (2026-09-27).** OpenAI, Groq and Azure LLM Speech take the
  whole recording in one request and refuse one past their limits -- OpenAI
  and Groq at 25 MB, about 13 minutes of the app's 16 kHz WAV -- or, for
  `gpt-4o-transcribe` and `gpt-4o-mini-transcribe`, whose model pages state
  2,000 output tokens, return a transcript cut short that reads like a
  complete one. `config.remote_batch_part_limit(engine, model)` is the single
  answer (a seconds bound and a byte cap per engine, a tighter seconds bound
  per model, the vendors' figures with their dates beside the constants;
  180 s for the two token-capped models, because fast speech in a language
  that tokenizes denser than English reaches 2,000 tokens inside five
  minutes), and
  `transcriber/_audio_parts.transcribe_in_parts` wraps the provider's own
  single-request method. Rules:
  - **A recording within both limits goes out untouched**: the very object,
    recognised from the WAV header alone, so a dictation of normal length
    sends the request it always sent (the byte cap is inclusive; a test pins
    the exact size). So does anything the shared WAV reader does not decode
    (an imported MP3, 24-bit PCM); the provider answers for it as before,
    and a file over the byte cap logs `remote_audio_not_split` with its
    reason, so a refusal that follows can be explained.
  - **Both bounds hold for every part.** The seconds are chosen for the app's
    16 kHz WAV, which reaches them first (a test pins it); an import at 44.1
    or 48 kHz carries up to three times the bytes per second, and the byte
    cap (`max_part_frames`) is what bounds its parts. A part is re-encoded as
    16-bit mono at the input's own rate, so a stereo import goes out as its
    mono mix.
  - **The cut is Granite CTC's splitter**, moved to
    `_pcm_audio.split_into_passes` with a `max_samples` bound beside
    `max_seconds`: the quietest 20 ms frame in the last 15 s of each window
    (at most half the window); the parts share no audio and concatenate back
    to a mono input exactly.
  - **A failed part names itself and carries what came before it**
    ("Transcribing part i of n failed: ..." plus the earlier parts' text
    through `recovered_text_suffix`, shared with Fun-ASR), never a joined
    transcript with a hole in it. **So does a part that comes back empty
    while it holds sound** (its loudest window at or above the silence
    gate's default threshold, or unmeasurable): a single request returning
    nothing is already a failure ("Empty model text is a failure"), and a
    part is minutes of speech -- 165-180 s at the smallest bound -- that the
    joined text would otherwise lose without a word, found by the review.
    A silent part's empty text is skipped, which is what a long pause in an
    imported meeting produces. The texts are joined with one space, the
    progress line names the running part, and the cancel hook is checked
    before every part, the first included: splitting a large import takes
    about a second per 265 MB.
  - **Split, not compressed**: an encoder would be a new dependency, and
    OpenAI does not accept FLAC.
  - Engines without an entry are sent whole: Deepgram (2 GB), ElevenLabs
    (3 GB / 10 h), AssemblyAI (2.2 GB / 10 h), and Fun-ASR, which streams.
- **Remote first-request diagnostics**: transcription workers log
  `transcription_timing` with initialization, transcription, and total
  durations. Groq reuses its SDK/HTTP client for the lifetime of the cached
  transcriber so later requests can reuse connections.
