"""No REST engine's error text or log shows its own API key.

A gateway may echo the credential it refused ("quota exceeded for <key>") in a
200 reply's `error` object or in a 4xx body; the text reaches the overlay, the
selectable error detail and the log. The key is also checked in the form a JSON
body writes it (a key holding `"` or `\\` appears escaped).
"""

from __future__ import annotations

import json
import logging

import pytest
from _rest_fakes import fake_response, http_error, json_response, wav_seconds
from test_rest_reply_guard import _ENGINES

from stt_app.transcriber.base import TranscriptionError

_KEYS = ["sk-live-0123456789abcdef", 'ab"cd\\ef-0123456789']
_ID = {"openrouter": "openrouter"}


def _json_form(key: str) -> str:
    return json.dumps(key)[1:-1]


def _fake(reply):
    """Every request answers `reply()`; Speechmatics' job is created and done
    first, so that its transcript fetch carries a 200 reply."""

    def answer(request, timeout=None, **_kwargs):
        url = request.full_url
        if not isinstance(reply, str):
            raise reply(url)
        if "speechmatics" in url and request.get_method() == "POST":
            return json_response({"id": "job-1"}, status=201)
        if "speechmatics" in url and not url.endswith("/transcript?format=txt"):
            return json_response({"job": {"id": "job-1", "status": "done"}})
        return fake_response(reply)

    return answer


def _assert_clean(text: str, key: str, caplog) -> None:
    assert key not in text
    assert _json_form(key) not in text
    assert key not in caplog.text
    assert _json_form(key) not in caplog.text


@pytest.mark.parametrize("key", _KEYS, ids=["plain", "escaped"])
@pytest.mark.parametrize(
    ("engine", "module", "name", "build"), _ENGINES, ids=[e[0] for e in _ENGINES]
)
def test_a_key_echoed_in_a_200_error_object_never_reaches_the_error_text(
    monkeypatch, caplog, engine, module, name, build, key
):
    caplog.set_level(logging.DEBUG)
    body = json.dumps({"error": {"message": f"quota exceeded for {key}"}})
    monkeypatch.setattr(module, "open_request", _fake(body))

    with pytest.raises(TranscriptionError) as raised:
        build(key).transcribe_batch(wav_seconds(1.0))

    _assert_clean(str(raised.value), key, caplog)


@pytest.mark.parametrize("key", _KEYS, ids=["plain", "escaped"])
@pytest.mark.parametrize(
    ("engine", "module", "name", "build"), _ENGINES, ids=[e[0] for e in _ENGINES]
)
def test_a_key_echoed_in_a_4xx_body_never_reaches_a_batch_or_a_key_check_text(
    monkeypatch, caplog, engine, module, name, build, key
):
    caplog.set_level(logging.DEBUG)
    body = json.dumps({"error": {"message": f"bad credential {key}"}})
    monkeypatch.setattr(
        module, "open_request", _fake(lambda url: http_error(url, 400, body))
    )

    with pytest.raises(TranscriptionError) as raised:
        build(key).transcribe_batch(wav_seconds(1.0))
    _assert_clean(str(raised.value), key, caplog)

    ok, message = build(key).test_connection()
    assert not ok
    _assert_clean(message, key, caplog)
