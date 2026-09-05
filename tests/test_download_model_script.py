"""The standalone download script must not delete another process's bytes."""

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load_download_model():
    """Import the script as a real module.

    `runpy.run_path` returns a *copy* of the executed namespace, so
    patching that dict does not reach the functions' own globals and the
    real implementation runs regardless.
    """
    spec = importlib.util.spec_from_file_location(
        "_stt_download_model_script", _SCRIPTS / "download_model.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(_SCRIPTS))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(_SCRIPTS))
    return module


def test_cancelling_while_waiting_for_the_slot_deletes_nothing(monkeypatch):
    """Ctrl+C during the cross-process wait must leave partials alone.

    The wait exists because another process owns the cache directory, so
    the incomplete files in it are that process's resume point. Deleting
    them makes a multi-gigabyte download the app has queued restart from
    zero -- the same failure the coordinator's explicit-interest rule was
    written to prevent inside one process.
    """
    module = _load_download_model()
    download_model = module.download_model
    cleaned = []

    def _never_gets_the_slot(model_name, model_dir, download, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(module, "run_coordinated_download", _never_gets_the_slot)
    monkeypatch.setattr(
        module,
        "cleanup_incomplete_model_download",
        lambda *a, **k: cleaned.append(a) or (0, 0, 0),
    )

    with pytest.raises(SystemExit) as excinfo:
        download_model("small", "D:/models")

    assert excinfo.value.code == 130
    assert cleaned == [], "cancelled while waiting, yet partial files were deleted"


def test_cancelling_our_own_download_still_cleans_up(monkeypatch):
    """The original behaviour must survive: our own partials are ours."""
    module = _load_download_model()
    download_model = module.download_model
    cleaned = []

    def _runs_then_interrupted(model_name, model_dir, download, **kwargs):
        download()

    def _download_interrupted(model_name, model_dir=""):
        raise KeyboardInterrupt

    monkeypatch.setattr(module, "run_coordinated_download", _runs_then_interrupted)
    monkeypatch.setattr(module, "download_model_snapshot", _download_interrupted)
    monkeypatch.setattr(
        module,
        "cleanup_incomplete_model_download",
        lambda *a, **k: cleaned.append(a) or (2, 4096, 0),
    )

    with pytest.raises(SystemExit) as excinfo:
        download_model("small", "D:/models")

    assert excinfo.value.code == 130
    assert cleaned == [("small", "D:/models")]


def test_a_partial_the_cleanup_could_not_remove_is_reported(monkeypatch, capsys):
    """The script has the same two counts as the Local tab's drain, and said
    "Removed 1 incomplete file" for a partial that was still on the disk."""
    module = _load_download_model()
    download_model = module.download_model

    def _runs_then_interrupted(model_name, model_dir, download, **kwargs):
        download()

    def _download_interrupted(model_name, model_dir=""):
        raise KeyboardInterrupt

    monkeypatch.setattr(module, "run_coordinated_download", _runs_then_interrupted)
    monkeypatch.setattr(module, "download_model_snapshot", _download_interrupted)
    monkeypatch.setattr(
        module, "cleanup_incomplete_model_download", lambda *a, **k: (1, 1000, 1)
    )

    with pytest.raises(SystemExit):
        download_model("small", "D:/models")

    err = capsys.readouterr().err
    assert "Removed 1 incomplete file (0.0 MB)." in err
    assert "1 incomplete file could not be removed (still in use)." in err


def _certificate_failure() -> Exception:
    """A real chained certificate error, as huggingface_hub delivers it."""
    import ssl

    cert = ssl.SSLCertVerificationError(
        "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
        "unable to get local issuer certificate"
    )
    try:
        raise cert
    except Exception as exc:  # build a real chain on purpose
        return exc


@pytest.mark.parametrize(
    ("label", "message_template"),
    [
        (
            "mirrored ONNX model, Hugging Face refused",
            "Model download for 'onnx-community/x' failed: {cause}. "
            "See docs/models.md.",
        ),
        (
            "both sources failed",
            "Model download for 'onnx-community/x' failed on Hugging Face "
            "({cause}) and on the ModelScope mirror (connection timed out).",
        ),
    ],
)
def test_a_certificate_failure_reaches_the_help_box_whatever_the_wording(
    monkeypatch, capsys, label, message_template
):
    """The guard must read the exception, not two hand-picked wordings.

    `format_model_download_error` has a mirrored and an unmirrored branch and
    the ONNX download path adds two more message shapes. Matching wordings
    meant one and the same corporate proxy produced the CA-bundle guidance and
    exit 2 for `--model small` and a bare "Download failed" with exit 1 for a
    mirrored ONNX model, which is the asymmetry the box exists to remove.
    """
    module = _load_download_model()
    cause = _certificate_failure()
    wrapped = RuntimeError(message_template.format(cause=cause))
    wrapped.__cause__ = cause

    assert "SSL certificate verification failed" not in str(wrapped), label
    assert "looked like a certificate error" not in str(wrapped), label

    def _explode(*_args, **_kwargs):
        raise wrapped

    monkeypatch.setattr(module, "run_coordinated_download", _explode)

    with pytest.raises(SystemExit) as excinfo:
        module.download_model("small", None)

    assert excinfo.value.code == 2, (
        f"{label}: a certificate failure exited {excinfo.value.code} instead "
        "of 2, so the user never saw the CA-bundle guidance"
    )
    assert "SSL CERTIFICATE ERROR" in capsys.readouterr().err


def test_the_help_box_does_not_claim_a_mirror_was_tried_when_there_is_none(capsys):
    """Telling the user "both sources were unreachable" was a falsehood.

    For a model in `MODELS_WITHOUT_MODELSCOPE_MIRROR` the mirror is never
    contacted -- the exception text that triggered the box says so itself --
    so the note contradicted the failure it was explaining.
    """
    module = _load_download_model()

    module._print_ssl_help("parakeet-tdt-0.6b-v3")
    unmirrored = capsys.readouterr().err
    assert "no ModelScope mirror" in unmirrored
    assert "both sources were unreachable" not in unmirrored

    module._print_ssl_help("small")
    mirrored = capsys.readouterr().err
    assert "both sources were unreachable" in mirrored


def test_the_manual_download_step_does_not_send_onnx_models_to_the_wrong_guide(
    capsys,
):
    """`docs/models.md` says manual browser import is CTranslate2 only.

    Pointing an ONNX model's manual-download step at it offered a workaround
    that the referenced instructions refuse, and the default model is now the
    one most likely to reach this box.
    """
    module = _load_download_model()

    module._print_ssl_help("parakeet-tdt-0.6b-v3")
    onnx = capsys.readouterr().err.split("4. MANUAL BROWSER DOWNLOAD:")[1]
    assert "Model Dir" in onnx
    assert "for how to arrange the files" not in onnx

    module._print_ssl_help("small")
    whisper = capsys.readouterr().err.split("4. MANUAL BROWSER DOWNLOAD:")[1]
    assert "for how to arrange the files" in whisper
