import numpy as np

from stt_app.vad import EnergyVad


def test_vad_detects_speech_then_silence_stop():
    vad = EnergyVad(
        sample_rate=1000,
        energy_threshold=0.05,
        min_speech_ms=100,
        max_silence_ms=200,
    )

    quiet = np.zeros(50, dtype=np.float32)  # 50ms
    loud = np.ones(50, dtype=np.float32) * 0.5  # 50ms

    for _ in range(3):
        decision = vad.process_chunk(quiet)
        assert decision.speech_started is False
        assert decision.should_stop is False

    decision = vad.process_chunk(loud)
    assert decision.speech_started is False

    decision = vad.process_chunk(loud)
    assert decision.speech_started is True
    assert decision.should_stop is False

    for _ in range(3):
        decision = vad.process_chunk(quiet)
        assert decision.should_stop is False

    decision = vad.process_chunk(quiet)
    assert decision.should_stop is True


def test_vad_reset_clears_state():
    vad = EnergyVad(sample_rate=1000, energy_threshold=0.05)
    loud = np.ones(200, dtype=np.float32) * 0.4

    vad.process_chunk(loud)
    assert vad.has_detected_speech is True

    vad.reset()
    assert vad.has_detected_speech is False


def _wav_bytes_from_float(audio: np.ndarray, sample_rate: int = 16000) -> bytes:
    import io
    import wave

    pcm = (np.clip(audio, -1.0, 1.0) * 32767.0).astype(np.int16)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm.tobytes())
    return buffer.getvalue()


def test_peak_windowed_rms_detects_short_quiet_speech():
    from stt_app.vad import peak_windowed_rms_from_wav

    # 1 s of silence with a single 100 ms whisper-level burst: windowing must
    # report the burst level, which full-recording averaging would dilute.
    audio = np.zeros(16000, dtype=np.float32)
    audio[8000:9600] = 0.05

    peak = peak_windowed_rms_from_wav(_wav_bytes_from_float(audio))

    assert peak > 0.03
    full_rms = float(np.sqrt(np.mean(audio * audio)))
    assert full_rms < 0.02


def test_peak_windowed_rms_reports_silence_and_bad_input():
    from stt_app.vad import peak_windowed_rms_from_wav

    silence = np.zeros(16000, dtype=np.float32)
    assert peak_windowed_rms_from_wav(_wav_bytes_from_float(silence)) < 0.0005
    assert peak_windowed_rms_from_wav(b"") == 0.0
    assert peak_windowed_rms_from_wav(b"RIFF") == 0.0
