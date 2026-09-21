"""Tests for SSL error detection, CA bundle resolution, find_cached_models,
model preloading, and API validation."""

from __future__ import annotations

import logging
import os
import stat
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from stt_app.config import MODEL_REPO_MAP
from stt_app.ssl_utils import (
    create_ssl_context,
    inject_system_trust_store,
    resolve_ca_bundle,
    sync_ca_bundle_env_vars,
)
from stt_app.transcriber.base import TranscriptionError
from stt_app.transcriber.local_faster_whisper import (
    LocalFasterWhisperTranscriber,
    _has_valid_model_snapshot,
    _is_ssl_error,
    cached_model_paths,
    cleanup_incomplete_model_download,
    delete_cached_model,
    download_destination_dir,
    download_model_snapshot,
    estimate_cached_model_bytes,
    find_cached_models,
    remove_orphaned_hub_partials,
)

# ---------------------------------------------------------------------------
# SSL error detection
# ---------------------------------------------------------------------------


class TestIsSSLError:
    def test_certificate_verify_failed(self):
        exc = Exception("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
        assert _is_ssl_error(exc) is True

    def test_unable_to_get_local_issuer(self):
        exc = Exception("unable to get local issuer certificate")
        assert _is_ssl_error(exc) is True

    def test_chained_ssl_error(self):
        cause = Exception("SSL: CERTIFICATE_VERIFY_FAILED")
        exc = Exception("Connection failed")
        exc.__cause__ = cause
        assert _is_ssl_error(exc) is True

    def test_non_ssl_error(self):
        exc = Exception("Connection refused")
        assert _is_ssl_error(exc) is False

    def test_self_signed_certificate(self):
        exc = Exception("self-signed certificate in certificate chain")
        assert _is_ssl_error(exc) is True

    def test_sslcertverificationerror_class_name(self):
        exc = Exception("SSLCertVerificationError: something failed")
        assert _is_ssl_error(exc) is True


# ---------------------------------------------------------------------------
# CA bundle resolution
# ---------------------------------------------------------------------------


def _write_valid_ca_bundle(path: Path) -> None:
    """Write one system CA certificate as a valid PEM bundle for SSL tests."""
    import base64
    import ssl

    ctx = ssl.create_default_context()
    certs = ctx.get_ca_certs(binary_form=True)
    if not certs:
        pytest.skip("No system CA certs available for test")
    pem = (
        "-----BEGIN CERTIFICATE-----\n"
        + base64.encodebytes(certs[0]).decode("ascii")
        + "-----END CERTIFICATE-----\n"
    )
    path.write_text(pem)


class TestResolveCABundle:
    def test_returns_none_when_no_env_vars(self, monkeypatch):
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
        monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
        assert resolve_ca_bundle() is None

    def test_returns_ssl_cert_file_when_set(self, tmp_path, monkeypatch):
        bundle = tmp_path / "ca-bundle.pem"
        _write_valid_ca_bundle(bundle)
        monkeypatch.setenv("SSL_CERT_FILE", str(bundle))
        monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
        assert resolve_ca_bundle() == str(bundle)

    def test_returns_requests_ca_bundle_when_set(self, tmp_path, monkeypatch):
        bundle = tmp_path / "ca-bundle.pem"
        _write_valid_ca_bundle(bundle)
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(bundle))
        assert resolve_ca_bundle() == str(bundle)

    def test_ssl_cert_file_takes_precedence(self, tmp_path, monkeypatch):
        bundle1 = tmp_path / "ssl-cert.pem"
        bundle2 = tmp_path / "requests-bundle.pem"
        _write_valid_ca_bundle(bundle1)
        _write_valid_ca_bundle(bundle2)
        monkeypatch.setenv("SSL_CERT_FILE", str(bundle1))
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(bundle2))
        assert resolve_ca_bundle() == str(bundle1)

    def test_ignores_nonexistent_path(self, monkeypatch):
        monkeypatch.setenv("SSL_CERT_FILE", "/nonexistent/path/ca.pem")
        monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
        assert resolve_ca_bundle() is None

    def test_ignores_empty_value(self, monkeypatch):
        monkeypatch.setenv("SSL_CERT_FILE", "")
        monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
        assert resolve_ca_bundle() is None

    def test_ignores_invalid_existing_file(self, tmp_path, monkeypatch):
        bundle = tmp_path / "not-a-ca-bundle.pem"
        bundle.write_text("cert")
        monkeypatch.setenv("SSL_CERT_FILE", str(bundle))
        monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
        assert resolve_ca_bundle() is None


class TestCreateSSLContext:
    def test_returns_none_when_no_bundle(self, monkeypatch):
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
        monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
        assert create_ssl_context() is None

    def test_returns_ssl_context_when_bundle_exists(self, tmp_path, monkeypatch):
        import ssl

        bundle = tmp_path / "ca-bundle.pem"
        _write_valid_ca_bundle(bundle)
        monkeypatch.setenv("SSL_CERT_FILE", str(bundle))
        result = create_ssl_context()
        assert result is not None
        assert isinstance(result, ssl.SSLContext)


class TestInjectSystemTrustStore:
    def test_returns_false_when_truststore_not_installed(self):
        with patch.dict("sys.modules", {"truststore": None}):
            # Force import failure by clearing cached module
            result = inject_system_trust_store()
            assert result is False

    def test_returns_false_on_exception(self):
        fake_mod = types.ModuleType("truststore")
        fake_mod.inject_into_ssl = MagicMock(side_effect=RuntimeError("boom"))
        with patch.dict("sys.modules", {"truststore": fake_mod}):
            result = inject_system_trust_store()
            assert result is False

    def test_returns_true_on_success(self):
        fake_mod = types.ModuleType("truststore")
        fake_mod.inject_into_ssl = MagicMock()
        with patch.dict("sys.modules", {"truststore": fake_mod}):
            result = inject_system_trust_store()
            assert result is True
            fake_mod.inject_into_ssl.assert_called_once()


class TestSyncCABundleEnvVars:
    def test_no_env_vars_set(self, monkeypatch):
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
        monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
        sync_ca_bundle_env_vars()
        assert os.environ.get("SSL_CERT_FILE", "") == ""
        assert os.environ.get("REQUESTS_CA_BUNDLE", "") == ""

    def test_copies_ssl_cert_file_to_requests_ca_bundle(self, tmp_path, monkeypatch):
        bundle = tmp_path / "ca.pem"
        _write_valid_ca_bundle(bundle)
        monkeypatch.setenv("SSL_CERT_FILE", str(bundle))
        monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
        sync_ca_bundle_env_vars()
        assert os.environ["REQUESTS_CA_BUNDLE"] == str(bundle)

    def test_copies_requests_ca_bundle_to_ssl_cert_file(self, tmp_path, monkeypatch):
        bundle = tmp_path / "ca.pem"
        _write_valid_ca_bundle(bundle)
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(bundle))
        sync_ca_bundle_env_vars()
        assert os.environ["SSL_CERT_FILE"] == str(bundle)

    def test_does_not_overwrite_existing(self, tmp_path, monkeypatch):
        b1 = tmp_path / "a.pem"
        b2 = tmp_path / "b.pem"
        _write_valid_ca_bundle(b1)
        _write_valid_ca_bundle(b2)
        monkeypatch.setenv("SSL_CERT_FILE", str(b1))
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(b2))
        sync_ca_bundle_env_vars()
        assert os.environ["SSL_CERT_FILE"] == str(b1)
        assert os.environ["REQUESTS_CA_BUNDLE"] == str(b2)

    def test_ignores_nonexistent_file(self, monkeypatch):
        monkeypatch.setenv("SSL_CERT_FILE", "/nonexistent/ca.pem")
        monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
        sync_ca_bundle_env_vars()
        assert os.environ.get("SSL_CERT_FILE", "") == ""
        assert os.environ.get("REQUESTS_CA_BUNDLE", "") == ""

    def test_removes_invalid_requests_ca_bundle(self, monkeypatch):
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/nonexistent/ca.pem")
        sync_ca_bundle_env_vars()
        assert os.environ.get("SSL_CERT_FILE", "") == ""
        assert os.environ.get("REQUESTS_CA_BUNDLE", "") == ""

    def test_removes_existing_invalid_ca_bundle(self, tmp_path, monkeypatch):
        bundle = tmp_path / "invalid.pem"
        bundle.write_text("cert")
        monkeypatch.setenv("SSL_CERT_FILE", str(bundle))
        monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
        sync_ca_bundle_env_vars()
        assert os.environ.get("SSL_CERT_FILE", "") == ""
        assert os.environ.get("REQUESTS_CA_BUNDLE", "") == ""


class TestFormatTranscriptionErrorSSL:
    """Test that _format_transcription_error detects SSL errors."""

    def test_ssl_error_produces_actionable_message(self):
        model = MagicMock()
        t = LocalFasterWhisperTranscriber(model_factory=lambda *a, **kw: model)
        cause = Exception("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
        exc = Exception("download failed")
        exc.__cause__ = cause
        msg = t._format_transcription_error(exc)
        assert "SSL" in msg
        assert "advanced-setup" in msg

    def test_non_ssl_error_unchanged(self):
        model = MagicMock()
        t = LocalFasterWhisperTranscriber(model_factory=lambda *a, **kw: model)
        exc = RuntimeError("out of memory")
        msg = t._format_transcription_error(exc)
        assert msg == "out of memory"

    def test_hub_error_message_is_case_insensitive(self):
        model = MagicMock()
        t = LocalFasterWhisperTranscriber(model_factory=lambda *a, **kw: model)
        exc = RuntimeError("HUB Snapshot failed due to Internet restrictions")
        msg = t._format_transcription_error(exc)
        assert "HuggingFace Hub is unreachable" in msg


# ---------------------------------------------------------------------------
# find_cached_models
# ---------------------------------------------------------------------------


class TestFindCachedModels:
    def _make_hf_cache(self, tmp_path: Path, model_short: str, repo_id: str):
        """Create a fake HF cache structure for a model."""
        folder_name = f"models--{repo_id.replace('/', '--')}"
        snapshot_dir = tmp_path / folder_name / "snapshots" / "abc123"
        snapshot_dir.mkdir(parents=True)
        (snapshot_dir / "config.json").write_text("{}")
        (snapshot_dir / "model.bin").write_bytes(b"\x00")
        (snapshot_dir / "tokenizer.json").write_text("{}")
        (snapshot_dir / "vocabulary.txt").write_text("hello")
        return snapshot_dir

    def test_finds_model_in_hf_cache(self, tmp_path):
        self._make_hf_cache(tmp_path, "small", "Systran/faster-whisper-small")
        with patch(
            "stt_app.transcriber.local_faster_whisper.default_hf_cache_dir",
            return_value=str(tmp_path),
        ):
            result = find_cached_models()
        assert "small" in result

    def test_finds_model_in_custom_dir(self, tmp_path):
        self._make_hf_cache(tmp_path, "tiny", "Systran/faster-whisper-tiny")
        with patch(
            "stt_app.transcriber.local_faster_whisper.default_hf_cache_dir",
            return_value="/nonexistent",
        ):
            result = find_cached_models(str(tmp_path))
        assert "tiny" in result

    def test_a_flat_folder_is_not_a_cached_faster_whisper_model(self, tmp_path):
        """The app always passes a size name, never a path.

        `WhisperModel` takes the flat-directory branch only for a
        `model_size_or_path` that *is* a directory; a size name goes to
        `snapshot_download`, which reads the `models--<repo>` layout alone.
        A flat folder therefore can never be loaded, and reporting it as
        cached hid the Download button behind a model the next dictation
        would still have to fetch.
        """
        flat_dir = tmp_path / "faster-whisper-base"
        flat_dir.mkdir()
        (flat_dir / "config.json").write_text("{}")
        (flat_dir / "model.bin").write_bytes(b"\x00")
        with patch(
            "stt_app.transcriber.local_faster_whisper.default_hf_cache_dir",
            return_value=str(tmp_path),
        ):
            result = find_cached_models()
        assert "base" not in result

    def test_returns_empty_when_no_models(self, tmp_path):
        # Both halves: `find_cached_models` hands the ONNX scan `model_dir`
        # unchanged, and `local_webgpu_asr._model_cache_dirs` then appends the
        # default cache through that module's own attribute. One function now
        # answers for both modules, but each module holds its own name for it,
        # so patching one does not patch the other -- and patching only the
        # faster-whisper one left this test relying on the suite's HF_HOME
        # isolation to keep the real cache out of the result.
        with (
            patch(
                "stt_app.transcriber.local_faster_whisper.default_hf_cache_dir",
                return_value=str(tmp_path),
            ),
            patch(
                "stt_app.transcriber.local_webgpu_asr.default_hf_cache_dir",
                return_value=str(tmp_path),
            ),
        ):
            result = find_cached_models()
        assert result == []

    def test_incomplete_model_not_returned(self, tmp_path):
        """Model dir missing model.bin should not be returned."""
        folder_name = "models--Systran--faster-whisper-small"
        snapshot_dir = tmp_path / folder_name / "snapshots" / "abc123"
        snapshot_dir.mkdir(parents=True)
        (snapshot_dir / "config.json").write_text("{}")
        # model.bin intentionally missing
        with patch(
            "stt_app.transcriber.local_faster_whisper.default_hf_cache_dir",
            return_value=str(tmp_path),
        ):
            result = find_cached_models()
        assert "small" not in result

    def test_multiple_models_found(self, tmp_path):
        self._make_hf_cache(tmp_path, "tiny", "Systran/faster-whisper-tiny")
        self._make_hf_cache(tmp_path, "small", "Systran/faster-whisper-small")
        with patch(
            "stt_app.transcriber.local_faster_whisper.default_hf_cache_dir",
            return_value=str(tmp_path),
        ):
            result = find_cached_models()
        assert "tiny" in result
        assert "small" in result
        # Should be in canonical order.
        assert result.index("tiny") < result.index("small")

    def test_a_configured_model_dir_hides_the_default_cache(self, tmp_path):
        """`download_root` is one cache root, not a search path.

        With a Model Dir configured, `snapshot_download(cache_dir=<Model Dir>)`
        never looks at `~/.cache/huggingface/hub`. Counting a copy there made
        the Local tab call the model installed while the first dictation
        silently downloaded it again -- and offline it could not be loaded at
        all.
        """
        hf_dir = tmp_path / "hf_cache"
        hf_dir.mkdir()
        custom_dir = tmp_path / "custom"
        custom_dir.mkdir()
        self._make_hf_cache(hf_dir, "small", "Systran/faster-whisper-small")
        self._make_hf_cache(custom_dir, "tiny", "Systran/faster-whisper-tiny")
        with patch(
            "stt_app.transcriber.local_faster_whisper.default_hf_cache_dir",
            return_value=str(hf_dir),
        ):
            result = find_cached_models(str(custom_dir))
        assert result == ["tiny"]

    def test_the_inventory_agrees_with_the_load_path_pre_fetch(self, tmp_path):
        """One directory decides both, so the two can never disagree.

        `_coordinated_download_if_missing` gates on `download_destination_dir`;
        while the inventory answered from a wider search, "installed" in the
        Local tab and "must download" at load time were two different answers
        about the same model.
        """
        custom_dir = tmp_path / "custom"
        custom_dir.mkdir()
        self._make_hf_cache(custom_dir, "tiny", "Systran/faster-whisper-tiny")
        with patch(
            "stt_app.transcriber.local_faster_whisper.default_hf_cache_dir",
            return_value=str(tmp_path / "nowhere"),
        ):
            reported = set(find_cached_models(str(custom_dir)))
        for model in ("tiny", "small"):
            destination = download_destination_dir(model, str(custom_dir))
            assert destination is not None
            assert (model in reported) is _has_valid_model_snapshot(
                destination, {"config.json", "model.bin"}
            )

    def test_does_not_iterate_entire_cache_root(self, tmp_path, monkeypatch):
        self._make_hf_cache(tmp_path, "small", "Systran/faster-whisper-small")
        original_iterdir = Path.iterdir

        def guarded_iterdir(path_self: Path):
            if path_self == tmp_path:
                raise AssertionError(
                    "find_cached_models should not enumerate the cache root"
                )
            return original_iterdir(path_self)

        monkeypatch.setattr(Path, "iterdir", guarded_iterdir)

        with (
            patch(
                "stt_app.transcriber.local_faster_whisper.default_hf_cache_dir",
                return_value=str(tmp_path),
            ),
            patch(
                "stt_app.transcriber.local_webgpu_asr.default_hf_cache_dir",
                return_value=str(tmp_path),
            ),
        ):
            result = find_cached_models()

        assert result == ["small"]


class TestEstimateCachedModelBytes:
    def _make_hf_cache(self, root: Path, repo_id: str) -> Path:
        folder_name = f"models--{repo_id.replace('/', '--')}"
        snapshot_dir = root / folder_name / "snapshots" / "abc123"
        snapshot_dir.mkdir(parents=True)
        (snapshot_dir / "config.json").write_text("{}")
        (snapshot_dir / "model.bin").write_bytes(b"\x00" * 10)
        return snapshot_dir

    def test_returns_zero_for_unknown_model(self, tmp_path):
        with patch(
            "stt_app.transcriber.local_faster_whisper.default_hf_cache_dir",
            return_value=str(tmp_path),
        ):
            assert estimate_cached_model_bytes("unknown") == 0

    def test_estimates_hf_cache_size(self, tmp_path):
        self._make_hf_cache(tmp_path, "Systran/faster-whisper-small")
        with patch(
            "stt_app.transcriber.local_faster_whisper.default_hf_cache_dir",
            return_value=str(tmp_path),
        ):
            size = estimate_cached_model_bytes("small")
        assert size >= 10

    def test_measures_the_configured_model_dir_not_the_default_cache(self, tmp_path):
        """A configured model dir is where the download lands, so progress must
        come from there even when a bigger copy sits in the default cache."""
        hf_dir = tmp_path / "hf_cache"
        hf_dir.mkdir()
        custom_dir = tmp_path / "custom"
        custom_dir.mkdir()
        self._make_hf_cache(hf_dir, "Systran/faster-whisper-small")
        self._make_hf_cache(custom_dir, "Systran/faster-whisper-small")
        # Make the *default* cache much larger than the download destination.
        big_blob = (
            hf_dir / "models--Systran--faster-whisper-small" / "blobs" / "big.bin"
        )
        big_blob.parent.mkdir(parents=True, exist_ok=True)
        big_blob.write_bytes(b"\x00" * 10_000)

        with patch(
            "stt_app.transcriber.local_faster_whisper.default_hf_cache_dir",
            return_value=str(hf_dir),
        ):
            size = estimate_cached_model_bytes("small", str(custom_dir))
        assert size < 10_000

    def test_ignores_foreign_hf_cache_copy_of_a_local_onnx_model(self, tmp_path):
        """A local ONNX download writes into the flat `local_dir`, never the
        sibling `models--<repo>` folder. An unrelated full-repo copy left there
        (e.g. fp32 weights pulled by a conversion experiment) must not be
        reported as this download's progress."""
        model_name = "granite-speech-4.1-2b"
        repo_id = MODEL_REPO_MAP[model_name]
        repo_basename = repo_id.rsplit("/", 1)[-1]

        foreign = (
            tmp_path
            / f"models--{repo_id.replace('/', '--')}"
            / "snapshots"
            / "abc123"
            / "fp32"
        )
        foreign.mkdir(parents=True)
        (foreign / "audio_encoder.onnx_data").write_bytes(b"\x00" * 5_000)

        destination = tmp_path / repo_basename / "onnx"
        destination.mkdir(parents=True)
        (destination / "audio_encoder_q4.onnx_data").write_bytes(
            b"\x00" * 700
        )

        with patch(
            "stt_app.transcriber.local_webgpu_asr.default_hf_cache_dir",
            return_value=str(tmp_path),
        ):
            size = estimate_cached_model_bytes(model_name)
        assert size == 700

    def test_falls_back_to_a_legacy_layout_when_the_destination_is_absent(
        self, tmp_path
    ):
        """A local ONNX model cached in the old `models--<repo>` layout is still
        resolved and loaded from there. Measuring only the flat destination
        reported 0 bytes and showed a 0% "Downloading" bar for a model that is
        fully present."""
        from stt_app.transcriber import local_webgpu_asr

        model_name = "cohere-transcribe-03-2026"
        repo_id = MODEL_REPO_MAP[model_name]
        legacy = (
            tmp_path
            / f"models--{repo_id.replace('/', '--')}"
            / "snapshots"
            / "abc123"
        )
        # A *complete* snapshot: an incomplete one must not count, because the
        # model would not load from it either.
        for relative in local_webgpu_asr._REQUIRED_FILES[model_name]:
            path = legacy / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"\x00" * 512)
        assert not (tmp_path / repo_id.rsplit("/", 1)[-1]).exists()

        with (
            patch(
                "stt_app.transcriber.local_webgpu_asr.default_hf_cache_dir",
                return_value=str(tmp_path),
            ),
            patch(
                "stt_app.transcriber.local_faster_whisper.default_hf_cache_dir",
                return_value=str(tmp_path),
            ),
        ):
            measured = estimate_cached_model_bytes(model_name)
        assert measured == 512 * len(local_webgpu_asr._REQUIRED_FILES[model_name])

    def test_foreign_copy_is_ignored_even_before_the_destination_exists(
        self, tmp_path
    ):
        """The combination that matters: destination absent *and* a foreign copy
        of the same repo present. Falling back to the largest candidate here
        reported a repo's fp32 conversion weights as this download's progress,
        which is the original 10078/2490 MB bug (found on the since-retired NAR
        variant)."""
        model_name = "granite-speech-4.1-2b"
        repo_id = MODEL_REPO_MAP[model_name]

        foreign = (
            tmp_path
            / f"models--{repo_id.replace('/', '--')}"
            / "snapshots"
            / "abc123"
            / "fp32"
        )
        foreign.mkdir(parents=True)
        (foreign / "audio_encoder.onnx_data").write_bytes(b"\x00" * 9_000)
        assert not (tmp_path / repo_id.rsplit("/", 1)[-1]).exists()

        with (
            patch(
                "stt_app.transcriber.local_webgpu_asr.default_hf_cache_dir",
                return_value=str(tmp_path),
            ),
            patch(
                "stt_app.transcriber.local_faster_whisper.default_hf_cache_dir",
                return_value=str(tmp_path),
            ),
        ):
            assert estimate_cached_model_bytes(model_name) == 0

    def test_does_not_double_count_snapshot_symlinks(self, tmp_path):
        repo_id = MODEL_REPO_MAP["small"]
        root = tmp_path / f"models--{repo_id.replace('/', '--')}"
        blobs = root / "blobs"
        blobs.mkdir(parents=True)
        blob = blobs / "deadbeef"
        blob.write_bytes(b"\x00" * 1_000)

        snapshot = root / "snapshots" / "abc123"
        snapshot.mkdir(parents=True)
        try:
            (snapshot / "model.bin").symlink_to(blob)
        except (OSError, NotImplementedError):
            pytest.skip("creating symlinks requires privileges on this platform")

        with patch(
            "stt_app.transcriber.local_faster_whisper.default_hf_cache_dir",
            return_value=str(tmp_path),
        ):
            size = estimate_cached_model_bytes("small")
        assert size == 1_000


def _short_path_name(path):
    """The 8.3 spelling Windows keeps for `path`, or the path itself."""
    import ctypes
    from ctypes import wintypes

    get_short = ctypes.windll.kernel32.GetShortPathNameW
    get_short.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    get_short.restype = wintypes.DWORD
    buffer = ctypes.create_unicode_buffer(1024)
    if not get_short(str(path), buffer, 1024):
        return str(path)
    return buffer.value


class TestDeleteCachedModel:
    def _make_hf_cache(self, root: Path, repo_id: str) -> Path:
        folder_name = f"models--{repo_id.replace('/', '--')}"
        snapshot_dir = root / folder_name / "snapshots" / "abc123"
        snapshot_dir.mkdir(parents=True)
        (snapshot_dir / "config.json").write_text("{}")
        (snapshot_dir / "model.bin").write_bytes(b"\x00")
        return root / folder_name

    def test_cached_model_paths_returns_existing_dirs(self, tmp_path):
        model_root = self._make_hf_cache(tmp_path, "Systran/faster-whisper-small")
        with patch(
            "stt_app.transcriber.local_faster_whisper.default_hf_cache_dir",
            return_value=str(tmp_path),
        ):
            paths = cached_model_paths("small")
        assert model_root in paths

    def test_delete_cached_model_removes_directories(self, tmp_path):
        model_root = self._make_hf_cache(tmp_path, "Systran/faster-whisper-small")
        with patch(
            "stt_app.transcriber.local_faster_whisper.default_hf_cache_dir",
            return_value=str(tmp_path),
        ):
            removed = delete_cached_model("small")
        assert removed >= 1
        assert model_root.exists() is False

    def test_cleanup_incomplete_download_preserves_complete_files(self, tmp_path):
        cache_root = tmp_path / "models--Systran--faster-whisper-small"
        incomplete = cache_root / "blobs" / "model.incomplete"
        complete = cache_root / "blobs" / "config"
        incomplete.parent.mkdir(parents=True)
        incomplete.write_bytes(b"partial")
        complete.write_bytes(b"complete")

        with patch(
            "stt_app.transcriber.local_faster_whisper._model_cache_dirs",
            return_value=[cache_root],
        ):
            removed_files, removed_bytes, left_files = cleanup_incomplete_model_download(
                "small"
            )

        assert removed_files == 1
        assert removed_bytes == len(b"partial")
        assert left_files == 0
        assert incomplete.exists() is False
        assert complete.read_bytes() == b"complete"

    @pytest.mark.skipif(os.name != "nt", reason="an open handle blocks unlink on Windows")
    def test_cleanup_reports_a_file_it_could_not_remove_as_still_there(self, tmp_path):
        """The size was counted before `unlink` and the file after, so a
        partial another program still held -- the killed download child in
        a kernel read, a virus scanner -- was reported as removed by size and
        as gone by count: "Removed 1 incomplete file (0.5 MB)" for 1,000
        bytes removed and 500,000 left, and "No incomplete files remained."
        with the file on the disk."""
        blobs = tmp_path / "models--Systran--faster-whisper-small" / "blobs"
        blobs.mkdir(parents=True)
        removable = blobs / "small.incomplete"
        removable.write_bytes(b"x" * 1000)
        locked = blobs / "locked.incomplete"
        locked.write_bytes(b"y" * 500_000)

        with (
            locked.open("rb"),
            patch(
                "stt_app.transcriber.local_faster_whisper._model_cache_dirs",
                return_value=[blobs.parent],
            ),
        ):
            outcome = cleanup_incomplete_model_download("small")

        assert tuple(outcome) == (1, 1000, 1)
        assert locked.exists(), "the held file was reported as removed"
        assert not removable.exists()

    def test_a_partial_another_program_is_deleting_is_not_reported_as_left(
        self, tmp_path, monkeypatch
    ):
        """A file being deleted by another program -- a virus scanner
        quarantining it, the killed download child tearing down -- refuses
        the unlink with "access denied" (WinError 5) and is gone a moment
        later, and was counted as still in use: measured with a racing
        deleter, 343 files "could not be removed" on an empty disk. Only
        a file that is still there is left: refused twice and gone, it is
        the other program's delete that landed."""
        blobs = tmp_path / "models--Systran--faster-whisper-small" / "blobs"
        blobs.mkdir(parents=True)
        partial = blobs / "small.incomplete"
        partial.write_bytes(b"x" * 1000)

        def _deleted_under_our_feet(self, missing_ok=False):
            if self.exists():
                os.remove(self)
            raise PermissionError(13, "Access is denied")

        monkeypatch.setattr(Path, "unlink", _deleted_under_our_feet)
        with patch(
            "stt_app.transcriber.local_faster_whisper._model_cache_dirs",
            return_value=[blobs.parent],
        ):
            outcome = cleanup_incomplete_model_download("small")

        assert tuple(outcome) == (0, 0, 0)
        assert not partial.exists()

    def test_a_partial_refused_once_is_removed_by_the_retry(self, tmp_path, monkeypatch):
        """A lock that was only transient -- the download child's handle
        closing as it dies -- refuses the first unlink and not the second;
        the retry removes the file and counts it as removed, with its size."""
        blobs = tmp_path / "models--Systran--faster-whisper-small" / "blobs"
        blobs.mkdir(parents=True)
        partial = blobs / "small.incomplete"
        partial.write_bytes(b"x" * 1000)
        real_unlink = Path.unlink
        refusals: list = []

        def _refuse_once(self, missing_ok=False):
            if not refusals:
                refusals.append(self)
                raise PermissionError(32, "The process cannot access the file")
            real_unlink(self, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "unlink", _refuse_once)
        with patch(
            "stt_app.transcriber.local_faster_whisper._model_cache_dirs",
            return_value=[blobs.parent],
        ):
            outcome = cleanup_incomplete_model_download("small")

        assert tuple(outcome) == (1, 1000, 0)
        assert refusals == [partial]
        assert not partial.exists()

    @pytest.mark.skipif(os.name != "nt", reason="an open handle blocks unlink on Windows")
    def test_a_model_dir_spelled_through_the_cache_counts_a_held_partial_once(
        self, tmp_path, monkeypatch
    ):
        """`_model_cache_dirs` dedupes by `Path` equality, which does not
        fold `..`: a Model Dir written as `<cache>/x/../hub` beside the
        default cache `<cache>/hub` listed the same directory twice, and
        one held partial was reported as two files still in use."""
        hub = tmp_path / "hub"
        blobs = hub / "models--Systran--faster-whisper-small" / "blobs"
        blobs.mkdir(parents=True)
        locked = blobs / "locked.incomplete"
        locked.write_bytes(b"y" * 10)
        monkeypatch.setattr(
            "stt_app.transcriber.local_faster_whisper.default_hf_cache_dir",
            lambda: str(hub),
        )
        spelled = str(tmp_path / "elsewhere" / ".." / "hub")

        with locked.open("rb"):
            outcome = cleanup_incomplete_model_download("small", spelled)

        assert tuple(outcome) == (0, 0, 1)

    def test_a_read_only_partial_is_removed_rather_than_called_in_use(self, tmp_path):
        """A read-only attribute -- a backup tool restored the partial, a
        copy carried it over -- refuses the unlink for good, and the file
        was reported as "still in use" on every cleanup while nothing held
        it. It is as unusable as any partial (the resume could not append
        to it either), so the attribute is cleared and the retry removes
        it, counted with its size."""
        blobs = tmp_path / "models--Systran--faster-whisper-small" / "blobs"
        blobs.mkdir(parents=True)
        partial = blobs / "small.incomplete"
        partial.write_bytes(b"x" * 1000)
        os.chmod(partial, stat.S_IREAD)
        with patch(
            "stt_app.transcriber.local_faster_whisper._model_cache_dirs",
            return_value=[blobs.parent],
        ):
            outcome = cleanup_incomplete_model_download("small")

        assert tuple(outcome) == (1, 1000, 0)
        assert not partial.exists()

    @pytest.mark.skipif(os.name != "nt", reason="8.3 short names are a Windows spelling")
    def test_a_model_dir_spelled_as_a_short_name_counts_a_held_partial_once(
        self, tmp_path, monkeypatch
    ):
        """`normpath` is lexical: `AVERYL~1` and `a very long directory name`
        name one directory on disk and were two search roots, so one held
        partial was reported as two files still in use -- the observable
        01adf24 fixed for `..`, reached through a spelling no lexical
        normaliser folds."""
        hub = tmp_path / "a very long directory name for eight dot three"
        blobs = hub / "models--Systran--faster-whisper-small" / "blobs"
        blobs.mkdir(parents=True)
        short = _short_path_name(hub)
        if short.lower() == str(hub).lower():
            pytest.skip("8.3 short names are disabled on this volume")
        locked = blobs / "locked.incomplete"
        locked.write_bytes(b"y" * 10)
        monkeypatch.setattr(
            "stt_app.transcriber.local_faster_whisper.default_hf_cache_dir",
            lambda: str(hub),
        )

        with locked.open("rb"):
            outcome = cleanup_incomplete_model_download("small", short)

        assert tuple(outcome) == (0, 0, 1)


# ---------------------------------------------------------------------------
# Preload model
# ---------------------------------------------------------------------------


class TestPreloadModel:
    def test_preload_calls_model_factory(self):
        model = MagicMock()
        factory_calls = []

        def factory(*args, **kwargs):
            factory_calls.append((args, kwargs))
            return model

        t = LocalFasterWhisperTranscriber(model_factory=factory)
        t.preload_model()
        assert len(factory_calls) == 1
        assert t.is_model_loaded

    def test_preload_raises_on_factory_error(self):
        def factory(*args, **kwargs):
            raise RuntimeError("download failed")

        t = LocalFasterWhisperTranscriber(model_factory=factory)
        with pytest.raises(RuntimeError, match="download failed"):
            t.preload_model()
        assert not t.is_model_loaded

    def test_preload_idempotent(self):
        model = MagicMock()
        call_count = 0

        def factory(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return model

        t = LocalFasterWhisperTranscriber(model_factory=factory)
        t.preload_model()
        t.preload_model()
        assert call_count == 1


# ---------------------------------------------------------------------------
# AssemblyAI SSL error detection
# ---------------------------------------------------------------------------


class TestAssemblyAISSLDetection:
    def test_ssl_error_in_transcribe_batch(self):
        from stt_app.transcriber.assemblyai_provider import AssemblyAITranscriber

        aai = types.ModuleType("assemblyai")
        aai.settings = MagicMock()

        class FakeConfig:
            def __init__(self, **kw):
                pass

        class FakeTranscriber:
            def submit(self, f, config=None):
                cause = Exception(
                    "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"
                )
                raise Exception("upload failed") from cause

        aai.TranscriptionConfig = FakeConfig
        aai.Transcriber = FakeTranscriber
        aai.TranscriptStatus = MagicMock()

        t = AssemblyAITranscriber(api_key="test-key", aai_module=aai)
        with pytest.raises(TranscriptionError, match="SSL"):
            t.transcribe_batch(b"\x00" * 100)


class TestAssemblyAITestConnection:
    def test_successful_connection(self):
        from unittest.mock import MagicMock

        from stt_app.transcriber.assemblyai_provider import AssemblyAITranscriber

        t = AssemblyAITranscriber(api_key="test-key")

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            ok, msg = t.test_connection()
        assert ok is True
        assert "OK" in msg

    def test_auth_failure(self):
        import urllib.error

        from stt_app.transcriber.assemblyai_provider import AssemblyAITranscriber

        t = AssemblyAITranscriber(api_key="bad-key")

        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.HTTPError(
                url="", code=401, msg="Unauthorized", hdrs={}, fp=None
            ),
        ):
            ok, msg = t.test_connection()
        assert ok is False
        assert "401" in msg

    def test_ssl_error_detected(self):
        from stt_app.transcriber.assemblyai_provider import AssemblyAITranscriber

        t = AssemblyAITranscriber(api_key="test-key")

        ssl_exc = Exception(
            "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"
        )
        with patch("urllib.request.urlopen", side_effect=ssl_exc):
            ok, msg = t.test_connection()
        assert ok is False
        assert "SSL" in msg


# ---------------------------------------------------------------------------
# Download script SSL handling
# ---------------------------------------------------------------------------


class TestDownloadScriptSSLDetection:
    def test_is_ssl_error_function(self):
        """The download script has its own _is_ssl_error — test it."""
        # We can import the function from local_faster_whisper since it's shared logic.
        from stt_app.transcriber.local_faster_whisper import _is_ssl_error

        exc = Exception("[SSL: CERTIFICATE_VERIFY_FAILED]")
        assert _is_ssl_error(exc) is True

        exc2 = Exception("timeout")
        assert _is_ssl_error(exc2) is False


class TestDownloadProgressMeasuresTheDestination:
    def _make_hf_cache(self, root: Path, repo_id: str, payload: bytes) -> Path:
        folder = f"models--{repo_id.replace('/', '--')}"
        snapshot = root / folder / "snapshots" / "abc123"
        snapshot.mkdir(parents=True)
        (snapshot / "config.json").write_text("{}")
        (snapshot / "model.bin").write_bytes(payload)
        return snapshot

    def test_a_copy_in_the_default_cache_is_not_this_download_s_progress(
        self, tmp_path
    ):
        """`WhisperModel(download_root=...)` reads one cache root.

        So with a Model Dir configured, a complete copy in the default Hugging
        Face cache is a different file set that this download will not use --
        and sizing it made the bar report a download that had not started as
        finished. This became reachable when the inventory stopped calling
        such a model installed, because until then its Download button was
        disabled.
        """
        default_cache = tmp_path / "hf"
        default_cache.mkdir()
        model_dir = tmp_path / "models"
        model_dir.mkdir()
        self._make_hf_cache(
            default_cache, "Systran/faster-whisper-tiny", b"0" * 4096
        )

        with patch(
            "stt_app.transcriber.local_faster_whisper.default_hf_cache_dir",
            return_value=str(default_cache),
        ):
            assert estimate_cached_model_bytes("tiny", str(model_dir)) == 0
            # And the same copy is measured when it *is* the destination.
            # 4096 of weights plus the two bytes of config.json.
            assert estimate_cached_model_bytes("tiny", "") == 4098

    def test_the_destination_is_measured_once_it_exists(self, tmp_path):
        model_dir = tmp_path / "models"
        model_dir.mkdir()
        self._make_hf_cache(
            model_dir, "Systran/faster-whisper-tiny", b"0" * 2048
        )

        with patch(
            "stt_app.transcriber.local_faster_whisper.default_hf_cache_dir",
            return_value=str(tmp_path / "nowhere"),
        ):
            assert estimate_cached_model_bytes("tiny", str(model_dir)) == 2050


# ---------------------------------------------------------------------------
# Housekeeping before a download starts
# ---------------------------------------------------------------------------


class TestOrphanedHubPartials:
    """huggingface_hub 1.32.0 never resumes a partial, so one left behind is
    dead weight that only freezes the percentage (see
    `remove_orphaned_hub_partials`)."""

    def test_both_incomplete_name_shapes_go(self, tmp_path):
        """1.8.0 wrote `<etag>.incomplete`, 1.32.0 `<etag>.<8 hex>.incomplete`.

        Either can be on the disk of a machine that has run both versions, and
        neither is read by a download made with the installed one.
        """
        blobs = tmp_path / "models--Systran--faster-whisper-small" / "blobs"
        blobs.mkdir(parents=True)
        (blobs / "aaa.incomplete").write_bytes(b"x" * 1_000)
        (blobs / "aaa.1a2b3c4d.incomplete").write_bytes(b"y" * 500)

        outcome = remove_orphaned_hub_partials("small", str(tmp_path))

        assert tuple(outcome) == (2, 1_500, 0)
        assert list(blobs.iterdir()) == []

    def test_the_flat_onnx_layout_is_cleared_too(self, tmp_path):
        """There huggingface_hub keeps its partials in its own bookkeeping
        folder under the `local_dir` it downloads into."""
        destination = tmp_path / "granite-4.0-1b-speech-ONNX"
        downloads = destination / ".cache" / "huggingface" / "download"
        downloads.mkdir(parents=True)
        orphan = downloads / "onnx" / "model_q4.onnx.9f8e7d6c.incomplete"
        orphan.parent.mkdir()
        orphan.write_bytes(b"x" * 2_000)

        outcome = remove_orphaned_hub_partials("granite-4.0-1b-speech", str(tmp_path))

        assert tuple(outcome) == (1, 2_000, 0)
        assert not orphan.exists()

    def test_a_modelscope_partial_and_the_finished_files_are_left_alone(self, tmp_path):
        """The mirror resumes its `*.ms-part` files, so removing one costs the
        bytes it already fetched; and a complete blob is the download's
        baseline, not its rubbish."""
        blobs = tmp_path / "models--Systran--faster-whisper-small" / "blobs"
        blobs.mkdir(parents=True)
        mirror_partial = blobs / "bbb.ms-part"
        mirror_partial.write_bytes(b"x" * 300)
        complete = blobs / "ccc"
        complete.write_bytes(b"y" * 700)

        outcome = remove_orphaned_hub_partials("small", str(tmp_path))

        assert tuple(outcome) == (0, 0, 0)
        assert mirror_partial.exists()
        assert complete.exists()

    def test_only_the_destination_is_touched_never_a_second_cache_root(self, tmp_path):
        """`cleanup_incomplete_model_download` sweeps every candidate root
        because the user asked for a cleanup. This runs on the way into a
        download, so it may only touch the directory that download writes."""
        model_dir = tmp_path / "models"
        default_cache = tmp_path / "hf"
        folder = "models--Systran--faster-whisper-small"
        for root in (model_dir, default_cache):
            (root / folder / "blobs").mkdir(parents=True)
            (root / folder / "blobs" / "aaa.incomplete").write_bytes(b"x" * 10)

        with patch(
            "stt_app.transcriber.local_faster_whisper.default_hf_cache_dir",
            return_value=str(default_cache),
        ):
            outcome = remove_orphaned_hub_partials("small", str(model_dir))

        assert tuple(outcome) == (1, 10, 0)
        assert not (model_dir / folder / "blobs" / "aaa.incomplete").exists()
        assert (default_cache / folder / "blobs" / "aaa.incomplete").exists()

    def test_a_destination_that_is_not_there_yet_is_not_an_error(self, tmp_path):
        assert tuple(remove_orphaned_hub_partials("small", str(tmp_path))) == (
            0,
            0,
            0,
        )
        assert tuple(remove_orphaned_hub_partials("no-such-model", "")) == (0, 0, 0)

    @pytest.mark.skipif(
        os.name != "nt", reason="an open handle blocks unlink on Windows"
    )
    def test_a_partial_that_cannot_be_removed_is_counted_and_not_raised(self, tmp_path):
        """A foreign tool really is fetching the same model into the same
        directory. The download still has to start."""
        blobs = tmp_path / "models--Systran--faster-whisper-small" / "blobs"
        blobs.mkdir(parents=True)
        held = blobs / "aaa.incomplete"
        held.write_bytes(b"x" * 400)

        with held.open("rb"):
            outcome = remove_orphaned_hub_partials("small", str(tmp_path))

        assert tuple(outcome) == (0, 0, 1)
        assert held.exists()


class TestDownloadStartHousekeeping:
    """What `download_model_snapshot` does before it asks for the first byte."""

    def _fake_hub(self, monkeypatch, calls, tmp_path, orphan: Path | None = None):
        import huggingface_hub

        def fake_snapshot_download(_repo_id, **_kwargs):
            calls.append("download")
            if orphan is not None:
                calls.append(f"orphan-on-disk:{orphan.exists()}")
            return str(tmp_path / "snapshot")

        monkeypatch.setattr(
            huggingface_hub, "snapshot_download", fake_snapshot_download
        )

    def test_the_orphan_is_gone_before_the_first_byte_is_asked_for(
        self, monkeypatch, tmp_path
    ):
        """Otherwise it is still there when the download starts, and the
        percentage stays at the high-water mark it describes until the real
        transfer passes it -- measured at 34 of 78 MB for four seconds after a
        kill at 33.7 MB, which for a 1.5 GB model killed at 80% is minutes."""
        blobs = tmp_path / "models--Systran--faster-whisper-small" / "blobs"
        blobs.mkdir(parents=True)
        orphan = blobs / "aaa.1a2b3c4d.incomplete"
        orphan.write_bytes(b"x" * 1_000)
        calls: list[str] = []
        self._fake_hub(monkeypatch, calls, tmp_path, orphan=orphan)

        download_model_snapshot("small", str(tmp_path))

        assert calls == ["download", "orphan-on-disk:False"]

    def test_a_cleanup_that_fails_does_not_fail_the_download(
        self, monkeypatch, tmp_path, caplog
    ):
        """It is housekeeping on the way in. A download that cannot start
        because a partial could not be listed would be a worse bug than the
        one this removes."""
        import stt_app.transcriber.local_faster_whisper as fw

        def explode(*_args):
            raise RuntimeError("no")

        calls: list[str] = []
        self._fake_hub(monkeypatch, calls, tmp_path)
        monkeypatch.setattr(fw, "_remove_partials_under", explode)

        with caplog.at_level(logging.WARNING, logger=fw.__name__):
            download_model_snapshot("small", str(tmp_path))

        assert calls == ["download"]
        assert "model_download_orphaned_partials_failed" in caplog.text

    def test_the_symlink_probe_is_settled_before_the_download_threads_race_it(
        self, monkeypatch, tmp_path
    ):
        """`are_symlinks_supported` caches `True` before it runs its test, so a
        second download thread asking inside that window calls `os.symlink` and
        dies with WinError 1314 -- which `_create_symlink` does not catch, so
        the whole `snapshot_download` fails. Measured against a zero-latency
        local Hub stand-in, three small files, a fresh cache per run: 11 of 12
        runs failed cold, 0 of 12 after one serial call here. The key is the
        storage folder, because that is the `commonpath` of a blob and its
        snapshot pointer, which is what `_create_symlink` asks with.
        """
        from huggingface_hub import file_download

        calls: list[str] = []
        probed: list[object] = []

        def fake_probe(cache_dir=None):
            calls.append("symlink-probe")
            probed.append(cache_dir)
            return False

        self._fake_hub(monkeypatch, calls, tmp_path)
        monkeypatch.setattr(file_download, "are_symlinks_supported", fake_probe)

        download_model_snapshot("small", str(tmp_path))

        assert calls == ["symlink-probe", "download"]
        assert [str(path) for path in probed] == [
            str(download_destination_dir("small", str(tmp_path)))
        ]

    def test_a_probe_that_is_gone_or_raises_does_not_fail_the_download(
        self, monkeypatch, tmp_path, caplog
    ):
        """An upstream rename must cost the mitigation, not the download."""
        from huggingface_hub import file_download

        import stt_app.transcriber.local_faster_whisper as fw

        calls: list[str] = []
        self._fake_hub(monkeypatch, calls, tmp_path)
        monkeypatch.delattr(file_download, "are_symlinks_supported")

        with caplog.at_level(logging.WARNING, logger=fw.__name__):
            download_model_snapshot("small", str(tmp_path))

        assert calls == ["download"]
        assert "hub_symlink_probe_unavailable" in caplog.text
