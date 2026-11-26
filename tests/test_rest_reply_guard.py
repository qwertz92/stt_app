"""Every REST engine refuses a 200 answer that is not a transcript.

A company proxy answers a blocked request with an HTML page and status 200,
and a gateway can answer 200 with JSON that holds no transcript. Read as the
text, either one was pasted into the user's document (OpenAI and ElevenLabs
returned the page's markup) or read as silence (Deepgram, Azure). The test
runs the same two bodies through each engine's real batch path against a fake
`open_request`; nothing reaches the network.
"""

from __future__ import annotations

import json

import pytest
from _rest_fakes import fake_response, json_response, wav_seconds

from stt_app.transcriber import azure_provider as azure_module
from stt_app.transcriber import custom_endpoint_provider as custom_module
from stt_app.transcriber import deepgram_provider as deepgram_module
from stt_app.transcriber import elevenlabs_provider as elevenlabs_module
from stt_app.transcriber import meta_provider as meta_module
from stt_app.transcriber import mistral_provider as mistral_module
from stt_app.transcriber import openai_provider as openai_module
from stt_app.transcriber import speechmatics_provider as speechmatics_module
from stt_app.transcriber.azure_provider import AzureLlmSpeechTranscriber
from stt_app.transcriber.base import TranscriptionError
from stt_app.transcriber.custom_endpoint_provider import CustomEndpointTranscriber
from stt_app.transcriber.deepgram_provider import DeepgramTranscriber
from stt_app.transcriber.elevenlabs_provider import ElevenLabsTranscriber
from stt_app.transcriber.meta_provider import MetaTranscriber
from stt_app.transcriber.mistral_provider import MistralTranscriber
from stt_app.transcriber.openai_provider import OpenAITranscriber
from stt_app.transcriber.openrouter_provider import OpenRouterTranscriber
from stt_app.transcriber.speechmatics_provider import SpeechmaticsTranscriber

_PROXY_PAGE = (
    "<!DOCTYPE html><html><head><title>Zscaler - Access Denied</title></head>"
    "<body>PASTED-MARKUP</body></html>"
)
_JSON_WITHOUT_TRANSCRIPT = json.dumps({"unexpected": "shape", "status": "ok"})

# (engine, module whose `open_request` is faked, name the error must carry,
# builder of the transcriber)
_ENGINES = [
    ("openai", openai_module, "OpenAI", lambda key: OpenAITranscriber(key)),
    ("mistral", mistral_module, "Mistral", lambda key: MistralTranscriber(key)),
    (
        "elevenlabs",
        elevenlabs_module,
        "ElevenLabs",
        lambda key: ElevenLabsTranscriber(key),
    ),
    ("deepgram", deepgram_module, "Deepgram", lambda key: DeepgramTranscriber(key)),
    (
        "azure",
        azure_module,
        "Azure",
        lambda key: AzureLlmSpeechTranscriber(
            key, endpoint="https://res.cognitiveservices.azure.com"
        ),
    ),
    (
        "speechmatics",
        speechmatics_module,
        "Speechmatics",
        lambda key: SpeechmaticsTranscriber(key, poll_interval_s=0.0),
    ),
    ("meta", meta_module, "Meta", lambda key: MetaTranscriber(key)),
    (
        "custom-transcriptions",
        custom_module,
        "Custom endpoint",
        lambda key: CustomEndpointTranscriber(
            api_key=key, endpoint="https://stt.example.com/v1", model="m"
        ),
    ),
    (
        "custom-chat",
        custom_module,
        "Custom endpoint",
        lambda key: CustomEndpointTranscriber(
            api_key=key,
            endpoint="https://stt.example.com/v1",
            model="m",
            api_mode="chat",
        ),
    ),
    ("openrouter", custom_module, "OpenRouter", lambda key: OpenRouterTranscriber(key)),
]


def _service(body: str):
    """Answers every request with `body`; Speechmatics' job is created and
    done first, so that its transcript fetch is what carries the body."""

    def answer(request, timeout=None, **_kwargs):
        url = request.full_url
        if "speechmatics" in url and request.get_method() == "POST":
            return json_response({"id": "job-1"}, status=201)
        if "speechmatics" in url and not url.endswith("/transcript?format=txt"):
            return json_response({"job": {"id": "job-1", "status": "done"}})
        return fake_response(body)

    return answer


@pytest.mark.parametrize(
    ("body", "shows"),
    [(_PROXY_PAGE, "HTML page"), (_JSON_WITHOUT_TRANSCRIPT, "")],
    ids=["html-page", "json-without-transcript"],
)
@pytest.mark.parametrize(
    ("engine", "module", "name", "build"), _ENGINES, ids=[e[0] for e in _ENGINES]
)
def test_a_200_answer_that_is_not_a_transcript_is_an_error(
    monkeypatch, engine, module, name, build, body, shows
):
    monkeypatch.setattr(module, "open_request", _service(body))

    with pytest.raises(TranscriptionError) as raised:
        build("k").transcribe_batch(wav_seconds(1.0))

    message = str(raised.value)
    assert name in message
    assert shows in message
    # Neither the page nor the foreign JSON is shown as if it were the answer.
    assert "PASTED-MARKUP" not in message
    assert "Expecting value" not in message
