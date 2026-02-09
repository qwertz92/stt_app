import numpy as np

from tts_app.vad import EnergyVad


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
