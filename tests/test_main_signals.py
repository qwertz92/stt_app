import signal

from PySide6 import QtWidgets

from tts_app.main import _install_signal_handlers


def test_install_signal_handlers_registers_int_and_term(monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    registered = []

    def fake_signal(sig, handler):
        registered.append(sig)
        return handler

    monkeypatch.setattr(signal, "signal", fake_signal)

    timer = _install_signal_handlers(app)

    assert signal.SIGINT in registered
    assert signal.SIGTERM in registered
    assert timer.isActive() is True

    timer.stop()
