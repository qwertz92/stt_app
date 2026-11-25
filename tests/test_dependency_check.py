from __future__ import annotations

import sys

import pytest

from stt_app import dependency_check as dc


def test_pins_are_read_without_extras_markers_or_comments(tmp_path):
    path = tmp_path / "requirements-win.txt"
    path.write_text(
        "# a comment\n"
        "PySide6==6.12.0\n"
        "onnx-asr[cpu,hub]==0.12.0\n"
        "pywin32==312 ; sys_platform == 'win32'\n"
        "-r other.txt\n"
        "unpinned>=1\n",
        encoding="utf-8",
    )

    assert dc.read_pins(path) == {
        "PySide6": "6.12.0",
        "onnx-asr": "0.12.0",
        "pywin32": "312",
    }


def test_only_the_pins_that_differ_are_reported_in_file_order():
    installed = {"a": "1.0", "b": "1.6.1", "c": None, "d": "2.0.0"}

    outdated = dc.find_outdated(
        {"a": "1.0.0", "b": "1.10.11", "c": "3", "d": "2.0.0"}, installed.get
    )

    assert [(item.name, item.installed, item.pinned) for item in outdated] == [
        ("b", "1.6.1", "1.10.11"),
        ("c", None, "3"),
    ]
    assert "not installed" in outdated[1].describe()


def test_a_newer_installed_version_is_not_reported_and_neither_is_garbage(caplog):
    installed = {"new": "2.0", "same": "1.0", "odd": "not-a-version", "old": "0.9"}

    with caplog.at_level("DEBUG", logger=dc.logger.name):
        outdated = dc.find_outdated(
            {"new": "1.0", "same": "1.0.0", "odd": "1.0", "old": "1.0"},
            installed.get,
        )

    assert [item.name for item in outdated] == ["old"]
    assert any("package_differs_from_pin name=new" in r.message for r in caplog.records)


def test_a_byte_order_mark_does_not_drop_the_first_pin(tmp_path):
    path = tmp_path / "requirements-win.txt"
    path.write_bytes(b"\xef\xbb\xbfPySide6==6.12.0\nsounddevice==0.5.6\n")

    assert dc.read_pins(path) == {"PySide6": "6.12.0", "sounddevice": "0.5.6"}


def test_the_command_is_uv_sync_for_the_uv_environment_only(tmp_path):
    (tmp_path / ".venv").mkdir()
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")

    assert dc.update_command(tmp_path, prefix=str(tmp_path / ".venv")) == "uv sync"
    # Not the checkout's own environment: the pip of the running interpreter,
    # never a bare `pip` that may belong to another Python on PATH.
    pip = '"C:\\Python312\\python.exe" -m pip install -r requirements-win.txt'
    assert (
        dc.update_command(
            tmp_path,
            prefix=str(tmp_path / "elsewhere"),
            executable="C:\\Python312\\python.exe",
        )
        == pip
    )
    (tmp_path / "uv.lock").unlink()
    assert (
        dc.update_command(
            tmp_path,
            prefix=str(tmp_path / ".venv"),
            executable="C:\\Python312\\python.exe",
        )
        == pip
    )


def test_the_default_command_names_the_running_interpreter(tmp_path):
    command = dc.update_command(tmp_path, prefix=str(tmp_path / "elsewhere"))

    assert command.startswith(f'"{sys.executable}" -m pip install -r ')


def test_the_message_names_the_packages_and_the_command():
    outdated = [dc.Outdated("assemblyai", "1.6.1", "1.10.11")]

    text = dc.outdated_packages_message(outdated, "uv sync")

    assert text.startswith("Python packages are out of date after an update: ")
    assert "assemblyai" in text and "uv sync" in text
    many = [dc.Outdated(f"p{i}", "1", "2") for i in range(8)]
    assert "and 3 more" in dc.outdated_packages_message(many, "uv sync")


def test_a_frozen_build_is_never_checked(monkeypatch):
    monkeypatch.setattr("sys.frozen", True, raising=False)

    assert dc.checkout_root() is None
    assert dc.check_checkout_packages() is None


def test_this_checkout_is_recognised_and_its_pin_file_is_found():
    root = dc.checkout_root()

    assert root is not None
    assert "assemblyai" in dc.read_pins(root / "requirements-win.txt")


def test_an_outdated_checkout_logs_one_warning_and_returns_the_command(
    monkeypatch, caplog
):
    stale = [dc.Outdated("assemblyai", "0.0.1", "9")]
    monkeypatch.setattr(dc, "find_outdated", lambda pins: stale)

    with caplog.at_level("WARNING", logger=dc.logger.name):
        result = dc.check_checkout_packages()

    assert result is not None
    outdated, command = result
    assert outdated == stale
    assert command == "uv sync" or "-m pip install -r requirements-win.txt" in command
    warnings = [r for r in caplog.records if "python_packages_out_of_date" in r.message]
    assert len(warnings) == 1
    assert "assemblyai (installed 0.0.1, pinned 9)" in warnings[0].message


def test_an_up_to_date_checkout_says_nothing(monkeypatch, caplog):
    monkeypatch.setattr(dc, "find_outdated", lambda pins: [])

    with caplog.at_level("WARNING", logger=dc.logger.name):
        assert dc.check_checkout_packages() is None

    assert not caplog.records


@pytest.mark.parametrize(
    ("installed", "minimum", "expected"),
    [
        ("1.10.11", "1.10.11", True),
        ("1.6.1", "1.10.11", False),
        ("1.10.2", "1.10.11", False),
        ("not-a-version", "1.0", False),
    ],
)
def test_version_at_least(installed, minimum, expected):
    assert dc.version_at_least(installed, minimum) is expected
