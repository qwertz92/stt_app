from __future__ import annotations

import json
import urllib.error

from stt_app.update_checker import check_for_updates, is_newer_version


class _FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._raw = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self) -> bytes:
        return self._raw


def test_is_newer_version_compares_numeric_parts():
    assert is_newer_version("0.4.2", "0.4.1") is True
    assert is_newer_version("v0.10.0", "0.4.9") is True
    assert is_newer_version("v0.4.1", "0.4.1") is False
    assert is_newer_version("v0.4.0", "0.4.1") is False


def test_check_for_updates_reports_available_release():
    def fake_urlopen(request, timeout):
        assert request.full_url.endswith("/releases/latest")
        assert timeout == 5.0
        return _FakeResponse(
            {
                "tag_name": "v0.4.2",
                "html_url": "https://github.com/qwertz92/stt_app/releases/tag/v0.4.2",
            }
        )

    result = check_for_updates(current_version="0.4.1", urlopen=fake_urlopen)

    assert result.update_available is True
    assert result.latest_tag == "v0.4.2"
    assert result.latest_version == "0.4.2"
    assert result.error == ""


def test_check_for_updates_reports_up_to_date_release():
    def fake_urlopen(_request, timeout):
        assert timeout == 5.0
        return _FakeResponse({"tag_name": "v0.4.1"})

    result = check_for_updates(current_version="0.4.1", urlopen=fake_urlopen)

    assert result.update_available is False
    assert result.latest_tag == "v0.4.1"
    assert result.error == ""


def test_check_for_updates_reports_network_errors():
    def fake_urlopen(_request, timeout):
        assert timeout == 5.0
        raise urllib.error.URLError("offline")

    result = check_for_updates(current_version="0.4.1", urlopen=fake_urlopen)

    assert result.update_available is False
    assert "offline" in result.error
