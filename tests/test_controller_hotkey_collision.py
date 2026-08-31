"""Re-registering the four global hotkeys must not collide with itself.

`HotkeyManager.register` unregisters only its *own* id, and Windows refuses a
combination another id already holds -- including one of this app's. So
re-registering the four in id order failed for any change that moved a
combination from a later id onto an earlier one, and Save-time validation
cannot see it: the *saved* set has no conflict, the collision exists only
during the re-registration.

Measured against the real `RegisterHotKey`, two ids on one thread: error 1409,
the first-registered hotkey left unregistered, and the combination it wanted
free the moment the later id gave it up. Only the recording hotkey recovers on
its own, through the reclaim timer -- the other three stay dead until restart.

`FakeWin32Registry` below is that behaviour and nothing else, so these tests
fail against a single-pass registration and pass against the release pass.
"""

from __future__ import annotations

import dataclasses

from conftest import FakeOverlay, FakeSettingsStore, make_controller

from stt_app.hotkey import HotkeyManager
from stt_app.settings_store import AppSettings

ERROR_HOTKEY_ALREADY_REGISTERED = 1409


class FakeWin32Registry:
    """One process-wide hotkey table, keyed the way Windows keys it."""

    def __init__(self) -> None:
        self.by_combination: dict[tuple[int, int], int] = {}
        self.by_id: dict[int, tuple[int, int]] = {}
        self.last_error = 0

    def register_hotkey(self, _hwnd, hotkey_id, modifiers, virtual_key):
        combination = (int(modifiers), int(virtual_key))
        holder = self.by_combination.get(combination)
        if holder is not None and holder != hotkey_id:
            self.last_error = ERROR_HOTKEY_ALREADY_REGISTERED
            return False
        previous = self.by_id.get(hotkey_id)
        if previous is not None:
            self.by_combination.pop(previous, None)
        self.by_combination[combination] = hotkey_id
        self.by_id[hotkey_id] = combination
        self.last_error = 0
        return True

    def unregister_hotkey(self, _hwnd, hotkey_id):
        combination = self.by_id.pop(hotkey_id, None)
        if combination is not None:
            self.by_combination.pop(combination, None)
        self.last_error = 0
        return True

    def get_last_error(self):
        return self.last_error

    def is_key_down(self, _virtual_key):
        return False

    def holder_of(self, hotkey_id):
        return self.by_id.get(hotkey_id)


def _controller_on(registry, settings):
    managers = {
        "hotkey_manager": HotkeyManager(api=registry, hotkey_id=1, hwnd=None),
        "cancel_hotkey_manager": HotkeyManager(api=registry, hotkey_id=2, hwnd=None),
        "show_overlay_hotkey_manager": HotkeyManager(
            api=registry, hotkey_id=3, hwnd=None
        ),
        "repaste_hotkey_manager": HotkeyManager(api=registry, hotkey_id=4, hwnd=None),
    }
    store = FakeSettingsStore(settings)
    controller, app = make_controller(
        settings_store=store,
        overlay=FakeOverlay(),
        **managers,
    )
    return controller, app, store


_BASE = AppSettings(
    hotkey="Ctrl+Alt+D",
    cancel_hotkey="Ctrl+Alt+K",
    show_overlay_hotkey="Ctrl+Alt+F11",
    repaste_hotkey="Ctrl+Alt+F12",
)


def test_swapping_two_optional_hotkeys_leaves_both_registered():
    """The show-overlay and re-paste hotkeys trade combinations.

    Neither is covered by the reclaim timer, so before the release pass the
    show-overlay hotkey stayed dead for the life of the app while the
    combination it asked for sat unused.
    """
    registry = FakeWin32Registry()
    controller, app, store = _controller_on(registry, _BASE)
    controller.refresh_hotkey_registration()
    assert controller._show_overlay_hotkey_registration_ok is True
    assert controller._repaste_hotkey_registration_ok is True

    store._settings = dataclasses.replace(
        _BASE,
        show_overlay_hotkey=_BASE.repaste_hotkey,
        repaste_hotkey=_BASE.show_overlay_hotkey,
    )
    controller.reload_settings(re_register_hotkey=True)

    assert controller._show_overlay_hotkey_registration_ok is True, (
        controller._show_overlay_hotkey_notice
    )
    assert controller._repaste_hotkey_registration_ok is True
    assert registry.holder_of(3) is not None, "the show-overlay hotkey is unregistered"
    assert registry.holder_of(4) is not None
    assert registry.holder_of(3) != registry.holder_of(4)
    controller.shutdown()
    _ = app


def test_taking_over_a_cleared_hotkeys_combination_still_registers():
    """Clear the re-paste hotkey and give the overlay its combination.

    A likelier move than a swap -- "put F12 on the overlay instead" -- and the
    saved set has no conflict at all, so validation passes and the collision
    lives entirely inside the re-registration.
    """
    registry = FakeWin32Registry()
    controller, app, store = _controller_on(registry, _BASE)
    controller.refresh_hotkey_registration()

    store._settings = dataclasses.replace(
        _BASE,
        show_overlay_hotkey=_BASE.repaste_hotkey,
        repaste_hotkey="",
    )
    controller.reload_settings(re_register_hotkey=True)

    assert controller._show_overlay_hotkey_registration_ok is True, (
        controller._show_overlay_hotkey_notice
    )
    assert registry.holder_of(3) is not None, "the show-overlay hotkey is unregistered"
    assert registry.holder_of(4) is None, "the cleared hotkey still holds a combination"
    controller.shutdown()
    _ = app


def test_the_recording_hotkey_takes_over_the_cancel_combination():
    """The same collision one id lower, where a fallback would hide it.

    The recording hotkey does recover eventually through the reclaim timer, so
    what this pins is that it never has to: it registers the combination the
    user asked for, on the save itself, instead of dropping to a fallback the
    user did not choose.
    """
    registry = FakeWin32Registry()
    controller, app, store = _controller_on(registry, _BASE)
    controller.refresh_hotkey_registration()

    store._settings = dataclasses.replace(
        _BASE, hotkey=_BASE.cancel_hotkey, cancel_hotkey=_BASE.hotkey
    )
    controller.reload_settings(re_register_hotkey=True)

    assert controller._hotkey_registration_ok is True, controller._hotkey_notice
    assert controller._active_hotkey == _BASE.cancel_hotkey
    assert controller._cancel_hotkey_registration_ok is True
    controller.shutdown()
    _ = app


def test_shutdown_still_releases_every_hotkey():
    """`shutdown` shares the release pass, so it has to keep doing its job."""
    registry = FakeWin32Registry()
    controller, app, _store = _controller_on(registry, _BASE)
    controller.refresh_hotkey_registration()
    assert len(registry.by_id) == 4

    controller.shutdown()

    assert registry.by_id == {}, "a hotkey survived shutdown"
    _ = app


def test_a_manager_that_cannot_be_released_does_not_stop_the_others():
    """A failed `UnregisterHotKey` keeps its combination; the rest still go.

    Reported by whichever registration then collides, which is the behaviour
    that existed before the release pass and must survive it.
    """

    class _StubbornRegistry(FakeWin32Registry):
        def unregister_hotkey(self, hwnd, hotkey_id):
            if hotkey_id == 2:
                self.last_error = 5
                return False
            return super().unregister_hotkey(hwnd, hotkey_id)

    registry = _StubbornRegistry()
    controller, app, _store = _controller_on(registry, _BASE)
    controller.refresh_hotkey_registration()
    assert len(registry.by_id) == 4

    controller._release_all_global_hotkeys()

    assert set(registry.by_id) == {2}, "releasing stopped at the failing manager"
    controller.shutdown()
    _ = app
