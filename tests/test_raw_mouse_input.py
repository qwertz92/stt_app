import ctypes

import pytest

from stt_app import raw_mouse_input

_RIDEV_INPUTSINK = 0x00000100
_RIDEV_REMOVE = 0x00000001


class _RawInputDataUser32:
    """`GetRawInputData` filling the buffer as the x64 SDK lays out RAWINPUT.

    The bytes are written at the SDK's offsets, not through the module's own
    structures, so a wrong structure layout reads the wrong field here as it
    would on Windows (header 24 bytes; `usButtonFlags` at 28, behind
    `usFlags` and two bytes of padding before the ULONG-aligned union).
    """

    def __init__(self, *, dw_type: int = 0, button_flags: int = 0, fail=False):
        self.dw_type = dw_type
        self.button_flags = button_flags
        self.fail = fail

    def GetRawInputData(self, _handle, command, data, size, header_size):
        assert command == 0x10000003  # RID_INPUT
        assert header_size == 24
        if self.fail:
            return 0xFFFFFFFF
        assert size._obj.value >= 48
        raw = bytearray(48)
        raw[0:4] = self.dw_type.to_bytes(4, "little")
        raw[4:8] = (48).to_bytes(4, "little")
        raw[28:30] = self.button_flags.to_bytes(2, "little")
        ctypes.memmove(ctypes.addressof(data._obj), bytes(raw), len(raw))
        return 48


@pytest.mark.parametrize(
    ("button_flags", "pressed"),
    [
        (0x0001, True),  # left down
        (0x0004, True),  # right down
        (0x0010, True),  # middle down
        (0x0040, True),  # X1 down
        (0x0100, True),  # X2 down
        (0x0002, False),  # left up
        (0x0008, False),  # right up
        (0x0000, False),  # a movement
        (0x0400, False),  # the wheel
    ],
)
def test_only_a_button_press_counts_as_a_press(monkeypatch, button_flags, pressed):
    user32 = _RawInputDataUser32(button_flags=button_flags)
    monkeypatch.setattr(raw_mouse_input, "_user32", lambda: user32)

    assert raw_mouse_input.mouse_button_pressed(0x1234) is pressed


@pytest.mark.parametrize(
    "user32",
    [
        _RawInputDataUser32(dw_type=1, button_flags=0x0001),  # a keyboard
        _RawInputDataUser32(fail=True),
    ],
    ids=["keyboard", "read-failed"],
)
def test_a_message_that_is_not_a_readable_mouse_event_is_no_press(monkeypatch, user32):
    monkeypatch.setattr(raw_mouse_input, "_user32", lambda: user32)

    assert raw_mouse_input.mouse_button_pressed(0x1234) is False


def test_the_watch_receives_presses_for_every_window_and_is_removed(
    raw_mouse_input_calls,
):
    # Without RIDEV_INPUTSINK, raw input reaches a window only while it is in
    # the foreground, and the overlay never is (WS_EX_NOACTIVATE).
    assert raw_mouse_input.watch_mouse_presses(0x5150) is True
    assert raw_mouse_input.stop_watching_mouse_presses() is True

    assert raw_mouse_input_calls.registrations == [
        (_RIDEV_INPUTSINK, 0x5150),
        (_RIDEV_REMOVE, None),
    ]
