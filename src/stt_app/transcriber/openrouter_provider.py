"""OpenRouter's transcription endpoint as an engine of its own (batch only).

The custom endpoint's transcription style pointed at OpenRouter: `POST
https://openrouter.ai/api/v1/audio/transcriptions`, multipart with `model`,
`file`, an optional `language` and `prompt`, answered by JSON with a `text`
field. Nothing is editable (fixed URL, no chat style, no key command), so the
class is `CustomEndpointTranscriber` with its engine id, name, model-list route
and a few sentences changed; the request and the error handling are that
class's. Read on OpenRouter's pages on 2026-11-26 (`docs/api/api-reference/stt/
create-transcription.md`, `docs/guides/overview/multimodal/stt.md`,
`docs/api-reference/limits`); `docs/agents/remote-providers.md` quotes them.
"""

from __future__ import annotations

import json
import urllib.error

from ..config import (
    CUSTOM_API_MODE_TRANSCRIPTIONS,
    DEFAULT_CUSTOM_VOCABULARY,
    DEFAULT_LANGUAGE_MODE,
    DEFAULT_OPENROUTER_MODEL,
    DEFAULT_SILENCE_GATE_THRESHOLD,
    ENGINE_LABELS,
    OPENROUTER_API_BASE,
    OPENROUTER_MODELS_QUERY,
    remote_batch_part_limit,
)
from ._http_utils import DEFAULT_REDIRECT_HINT
from .base import TranscriptionError
from .custom_endpoint_provider import CustomEndpointTranscriber

_KEY_PATH = "/key"
_KEY_CHECK_TIMEOUT_S = 15
_MAX_KEY_RESPONSE_BYTES = 64 * 1024


class OpenRouterTranscriber(CustomEndpointTranscriber):
    _engine = "openrouter"
    _name = ENGINE_LABELS["openrouter"]
    _redirect_hint = DEFAULT_REDIRECT_HINT
    _upload_progress = (
        f"Uploading audio to {ENGINE_LABELS['openrouter']} and waiting for "
        "transcription..."
    )
    # The public list of models with a transcription output; the plain
    # `/models` is 469 chat models.
    _models_path = f"/models?{OPENROUTER_MODELS_QUERY}"
    _missing_key_message = (
        "OpenRouter API key is missing. Enter your key in Settings -> Providers."
    )
    # The address is fixed, so a wrong-URL hint would send the user to a field
    # that does not exist.
    _wrong_url_hint = (
        " OpenRouter's address is built in, so this is most likely a proxy or "
        "network problem."
    )
    _html_reply_hint = (
        "typically a proxy's sign-in or block page. OpenRouter's address is "
        "built in; check whether the proxy needs a sign-in."
    )
    _model_list_not_json_hint = " A proxy may be answering instead of OpenRouter."

    def __init__(
        self,
        api_key: str = "",
        model: str = DEFAULT_OPENROUTER_MODEL,
        language_mode: str = DEFAULT_LANGUAGE_MODE,
        custom_vocabulary: str = DEFAULT_CUSTOM_VOCABULARY,
        request_timeout_s: int = 120,
        silence_gate_threshold: float = DEFAULT_SILENCE_GATE_THRESHOLD,
        listing_only: bool = False,
    ) -> None:
        # The model list is public (a GET without any key answered 200 on
        # 2026-11-26), so Refresh works before a key is entered; such an
        # instance sends no `Authorization` header unless a key was given, and
        # is never used to transcribe.
        self._listing_only = listing_only
        super().__init__(
            api_key=api_key,
            endpoint=OPENROUTER_API_BASE,
            model=str(model or "").strip() or DEFAULT_OPENROUTER_MODEL,
            api_mode=CUSTOM_API_MODE_TRANSCRIPTIONS,
            key_command="",
            language_mode=language_mode,
            custom_vocabulary=custom_vocabulary,
            request_timeout_s=request_timeout_s,
            silence_gate_threshold=silence_gate_threshold,
        )

    def _request_too_large_hint(self) -> str:
        limit = remote_batch_part_limit(self._engine, self._model)
        return (
            f" OpenRouter, or a proxy in front of it, refuses a request this "
            f"large; the app sends parts of at most {limit.seconds:.0f} s or "
            f"{limit.max_bytes // 1_000_000} MB and cannot make them smaller."
        )

    def test_connection(self) -> tuple[bool, str]:
        """Whether OpenRouter accepts the key.

        Not the model list: it is public, so every key, a wrong one included,
        would pass. `GET /key` ("To check the rate limit or credits left on an
        API key", Bearer key, limits page) answers only for a valid key.
        """
        with self._errors_scrubbed():
            try:
                payload = self._send(
                    lambda token: self._request(_KEY_PATH, token, data=None),
                    timeout=_KEY_CHECK_TIMEOUT_S,
                    max_bytes=_MAX_KEY_RESPONSE_BYTES,
                )
            except urllib.error.HTTPError as exc:
                return False, str(self._http_error(exc, "key check"))
            except TranscriptionError as exc:
                return False, str(exc)
            except Exception as exc:
                return False, str(self._other_error(exc, "key check"))
        try:
            parsed = json.loads(payload.decode("utf-8", errors="replace"))
        except ValueError:
            parsed = None
        if not isinstance(parsed, dict) or not isinstance(parsed.get("data"), dict):
            return (
                False,
                f"{self._name}: the key check answered with something unexpected.",
            )
        return True, "Connection OK — API key is valid."
