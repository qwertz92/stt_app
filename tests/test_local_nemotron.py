from __future__ import annotations

import io
import json
import threading
import time
import wave

import pytest

from stt_app.config import (
    NEMOTRON_LANGUAGE_IDS,
    NEMOTRON_MODEL_SIZE,
    language_modes_for_selection,
    supports_streaming,
)
from stt_app.transcriber import local_nemotron
from stt_app.transcriber.base import TranscriptionCanceled, TranscriptionError
from stt_app.transcriber.local_nemotron import LocalNemotronTranscriber


class _FakeConfig:
    def __init__(self, path):
        self.path = path
        self.providers = []

    def clear_providers(self):
        self.providers.clear()

    def append_provider(self, provider):
        self.providers.append(provider)


class _FakeProcessor:
    def __init__(self):
        self.chunk_count = 0
        self.options = {}

    def set_option(self, key, value):
        self.options[key] = value

    def process(self, _samples):
        self.chunk_count += 1
        return self.chunk_count

    def flush(self):
        return None


class _FakeTokenizerStream:
    def decode(self, token):
        return {1: "hello", 2: " world"}.get(token, " more")


class _FakeTokenizer:
    def create_stream(self):
        return _FakeTokenizerStream()


class _FakeGenerator:
    def __init__(self):
        self.done = True
        self.token = 0
        self.runtime_options = {}

    def set_runtime_option(self, key, value):
        self.runtime_options[key] = value

    def set_inputs(self, token):
        self.token = token
        self.done = False

    def is_done(self):
        return self.done

    def generate_next_token(self):
        self.done = True

    def get_next_tokens(self):
        return [self.token]


class _FakeRuntime:
    def __init__(self, *, fail_dml=False):
        self.fail_dml = fail_dml
        self.configs = []
        self.generators = []

    def Config(self, path):
        config = _FakeConfig(path)
        self.configs.append(config)
        return config

    def Model(self, config):
        if self.fail_dml and config.providers == ["dml"]:
            raise RuntimeError("DML unavailable")
        return object()

    def StreamingProcessor(self, _model):
        return _FakeProcessor()

    def Tokenizer(self, _model):
        return _FakeTokenizer()

    def GeneratorParams(self, _model):
        return object()

    def Generator(self, _model, _params):
        generator = _FakeGenerator()
        self.generators.append(generator)
        return generator


def _write_snapshot(tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "genai_config.json").write_text(
        json.dumps({"model": {"sample_rate": 16000, "chunk_samples": 8960}}),
        encoding="utf-8",
    )
    return snapshot


def _wav_bytes(sample_count=17_920):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16000)
        wav_file.writeframes(b"\x01\x00" * sample_count)
    return buffer.getvalue()


def _transcriber(monkeypatch, tmp_path, runtime, **kwargs):
    snapshot = _write_snapshot(tmp_path)
    monkeypatch.setattr(
        "stt_app.transcriber.local_nemotron.resolve_cached_webgpu_model_path",
        lambda *_args: snapshot,
    )
    return LocalNemotronTranscriber(
        model_size=NEMOTRON_MODEL_SIZE,
        runtime_module=runtime,
        **kwargs,
    )


def test_batch_transcription_uses_dml_then_cpu_fallback(monkeypatch, tmp_path):
    runtime = _FakeRuntime(fail_dml=True)
    transcriber = _transcriber(
        monkeypatch,
        tmp_path,
        runtime,
        language_mode="de",
    )

    text = transcriber.transcribe_batch(_wav_bytes())

    assert text == "hello world"
    assert transcriber.runtime_device == "cpu"
    assert transcriber.runtime_details_text == "Fallback attempts: dml: DML unavailable"
    assert [config.providers for config in runtime.configs] == [["dml"], []]
    assert runtime.generators[0].runtime_options["lang_id"] == "9"


def test_a_cancel_stops_a_running_batch_transcription(monkeypatch, tmp_path):
    """Without a check inside the chunk loop, Cancel could not stop a running
    Nemotron transcription: it kept a core busy and held the single
    transcription worker for the whole recording."""
    runtime = _FakeRuntime()
    transcriber = _transcriber(monkeypatch, tmp_path, runtime, language_mode="de")
    # Four 560 ms chunks; cancel once the second one has been processed.
    audio = _wav_bytes(8_960 * 4)
    processed: list[int] = []
    original = transcriber._process_samples

    def counting(session, samples):
        processed.append(len(samples))
        return original(session, samples)

    transcriber._process_samples = counting  # type: ignore[method-assign]
    transcriber.set_cancel_check(lambda: len(processed) >= 2)

    with pytest.raises(TranscriptionCanceled):
        transcriber.transcribe_batch(audio)

    assert len(processed) == 2


def test_a_cancel_before_the_first_chunk_creates_no_session(monkeypatch, tmp_path):
    runtime = _FakeRuntime()
    transcriber = _transcriber(monkeypatch, tmp_path, runtime, language_mode="de")
    transcriber.set_cancel_check(lambda: True)

    with pytest.raises(TranscriptionCanceled):
        transcriber.transcribe_batch(_wav_bytes())

    assert runtime.generators == []


def test_a_raising_cancel_check_does_not_fail_a_nemotron_batch(monkeypatch, tmp_path):
    runtime = _FakeRuntime()
    transcriber = _transcriber(monkeypatch, tmp_path, runtime, language_mode="de")

    def broken() -> bool:
        raise ValueError("check exploded")

    transcriber.set_cancel_check(broken)
    assert transcriber.transcribe_batch(_wav_bytes()) == "hello world"


def test_true_streaming_emits_incremental_text(monkeypatch, tmp_path):
    runtime = _FakeRuntime()
    transcriber = _transcriber(monkeypatch, tmp_path, runtime)
    partials = []

    transcriber.start_stream(on_partial=partials.append)
    transcriber.push_audio_chunk(b"\x01\x00" * 8_960)
    text = transcriber.stop_stream()

    assert text == "hello"
    assert partials == ["hello"]
    assert runtime.generators[0].runtime_options["lang_id"] == "101"


def test_retired_stream_worker_cannot_consume_or_publish_into_next_session(
    monkeypatch,
    tmp_path,
):
    transcriber = _transcriber(monkeypatch, tmp_path, _FakeRuntime())
    entered = threading.Event()
    release = threading.Event()
    first_partials = []
    second_partials = []
    original_process = transcriber._process_stream_pcm

    def blocking_process(session, payload, run):
        if run.generation == 1:
            entered.set()
            assert release.wait(timeout=2)
        return original_process(session, payload, run)

    monkeypatch.setattr(transcriber, "_process_stream_pcm", blocking_process)
    monkeypatch.setattr(local_nemotron, "STREAMING_ABORT_JOIN_TIMEOUT_S", 0.01)

    transcriber.start_stream(on_partial=first_partials.append)
    with transcriber._stream_lock:
        retired_thread = transcriber._stream_thread
    assert retired_thread is not None
    transcriber.push_audio_chunk(b"\x01\x00" * 8_960)
    assert entered.wait(timeout=1)
    transcriber.abort_stream()

    transcriber.start_stream(on_partial=second_partials.append)
    transcriber.push_audio_chunk(b"\x01\x00" * 8_960)
    release.set()
    assert transcriber.stop_stream() == "hello"
    retired_thread.join(timeout=1)

    assert retired_thread.is_alive() is False
    assert first_partials == []
    assert second_partials == ["hello"]


def test_close_defers_runtime_teardown_until_retired_worker_exits(
    monkeypatch,
    tmp_path,
):
    transcriber = _transcriber(monkeypatch, tmp_path, _FakeRuntime())
    entered = threading.Event()
    release = threading.Event()
    original_process = transcriber._process_stream_pcm

    def blocking_process(session, payload, run):
        entered.set()
        assert release.wait(timeout=2)
        return original_process(session, payload, run)

    monkeypatch.setattr(transcriber, "_process_stream_pcm", blocking_process)
    monkeypatch.setattr(local_nemotron, "STREAMING_ABORT_JOIN_TIMEOUT_S", 0.01)

    transcriber.start_stream()
    with transcriber._stream_lock:
        retired_thread = transcriber._stream_thread
    assert retired_thread is not None
    transcriber.push_audio_chunk(b"\x01\x00" * 8_960)
    assert entered.wait(timeout=1)
    transcriber.abort_stream()
    transcriber.close()

    assert transcriber.is_model_loaded is True
    release.set()
    retired_thread.join(timeout=1)
    deadline = time.monotonic() + 1
    while transcriber.is_model_loaded:
        assert time.monotonic() < deadline
        time.sleep(0.001)

    assert retired_thread.is_alive() is False


def test_streaming_requires_active_session(monkeypatch, tmp_path):
    transcriber = _transcriber(monkeypatch, tmp_path, _FakeRuntime())

    with pytest.raises(TranscriptionError, match="not active"):
        transcriber.push_audio_chunk(b"\x01\x00")

    with pytest.raises(TranscriptionError, match="not active"):
        transcriber.stop_stream()


def test_preload_reports_directml_runtime(monkeypatch, tmp_path):
    transcriber = _transcriber(monkeypatch, tmp_path, _FakeRuntime())

    transcriber.preload_model()

    assert transcriber.is_model_loaded is True
    assert transcriber.runtime_device == "dml"
    assert "DirectML" in transcriber.runtime_status_text()


def test_nemotron_capabilities_include_streaming_auto_and_german():
    modes = language_modes_for_selection("local", NEMOTRON_MODEL_SIZE, "streaming")

    assert supports_streaming("local", NEMOTRON_MODEL_SIZE) is True
    assert modes == tuple(NEMOTRON_LANGUAGE_IDS)
    assert modes[0] == "auto"
    assert "de" in modes
    assert "bg" in modes
    assert NEMOTRON_LANGUAGE_IDS["it"] == 15
    assert NEMOTRON_LANGUAGE_IDS["ja"] == 10
    assert NEMOTRON_LANGUAGE_IDS["ko"] == 14
    assert NEMOTRON_LANGUAGE_IDS["no"] == 103
    assert NEMOTRON_LANGUAGE_IDS["vi"] == 33
    assert NEMOTRON_LANGUAGE_IDS["et"] == 60
    assert "el" not in modes


def test_nemotron_decode_strips_the_locale_marker_it_emits():
    """Wiring guard: the helper must actually be applied to decoded output.

    Testing `strip_language_tags` alone proves nothing about this engine -- the
    call can be removed from the decode path and that test stays green. This
    drives the real decode loop with a tokenizer that emits the marker exactly
    as the model does in automatic-language mode.
    """
    from stt_app.transcriber.local_nemotron import LocalNemotronTranscriber

    pieces = iter(["<de-DE>", " Hallo", " Welt"])

    class _Generator:
        def __init__(self):
            self.remaining = 3

        def is_done(self):
            return self.remaining <= 0

        def generate_next_token(self):
            self.remaining -= 1

        def get_next_tokens(self):
            return [object()]

    class _TokenizerStream:
        def decode(self, _token):
            return next(pieces, "")

    class _Session:
        def __init__(self):
            self.generator = _Generator()
            self.tokenizer_stream = _TokenizerStream()
            self.text = ""

    session = _Session()
    decoded = LocalNemotronTranscriber._decode_available(session)

    assert "<de-DE>" not in decoded, f"locale marker reached the transcript: {decoded!r}"
    assert "<de-DE>" not in session.text
    assert decoded.strip() == "Hallo Welt"


def test_decoding_never_rewrites_text_it_already_emitted():
    """A marker split across two chunks is left alone, on purpose.

    Removing it means rewriting text that was already handed to the partial
    callback and probably already pasted at the caret. The controller's
    locked prefix compares against exactly that text, so the rewrite makes
    `compute_stream_locked_prefix` unable to advance and live insertion
    freezes for the rest of the session while the overlay reports progress.
    A stray marker is cosmetic; losing the rest of the dictation is not.
    """
    from stt_app.transcriber.local_nemotron import LocalNemotronTranscriber

    class _Session:
        text = ""

    session = _Session()
    seen = []
    for chunk in (["Guten", " Tag <de-"], ["DE> heute", " ist"]):
        pieces = iter(chunk)
        remaining = {"n": len(chunk)}

        class _Generator:
            # Bound explicitly rather than closed over: the loop rebinds
            # `remaining` and `pieces` on every iteration, so a class that
            # captured them would read whichever chunk happened to be last.
            def __init__(self, state):
                self._state = state

            def is_done(self):
                return self._state["n"] <= 0

            def generate_next_token(self):
                self._state["n"] -= 1

            def get_next_tokens(self):
                return [object()]

        class _Tokenizer:
            def __init__(self, source):
                self._source = source

            def decode(self, _token):
                return next(self._source, "")

        session.generator = _Generator(remaining)
        session.tokenizer_stream = _Tokenizer(pieces)
        before = session.text
        LocalNemotronTranscriber._decode_available(session)
        seen.append(before)
        assert session.text.startswith(before), (
            f"already-emitted text was rewritten: {before!r} -> "
            f"{session.text!r}; the locked prefix can never advance again"
        )

    # The split marker survives -- accepted, and cheaper than the freeze.
    assert session.text == "Guten Tag <de-DE> heute ist"
