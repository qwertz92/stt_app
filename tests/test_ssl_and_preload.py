"""Tests for SSL error detection, CA bundle resolution, find_cached_models,
model preloading, and API validation."""

from __future__ import annotations

import os
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tts_app.ssl_utils import (
    create_ssl_context,
    inject_system_trust_store,
    resolve_ca_bundle,
    sync_ca_bundle_env_vars,
)
from tts_app.transcriber.base import TranscriptionError
from tts_app.transcriber.local_faster_whisper import (
    LocalFasterWhisperTranscriber,
    _is_ssl_error,
    cached_model_paths,
    delete_cached_model,
    estimate_cached_model_bytes,
    find_cached_models,
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


class TestResolveCABundle:
    def test_returns_none_when_no_env_vars(self, monkeypatch):
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
        monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
        assert resolve_ca_bundle() is None

    def test_returns_ssl_cert_file_when_set(self, tmp_path, monkeypatch):
        bundle = tmp_path / "ca-bundle.pem"
        bundle.write_text("-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n")
        monkeypatch.setenv("SSL_CERT_FILE", str(bundle))
        monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
        assert resolve_ca_bundle() == str(bundle)

    def test_returns_requests_ca_bundle_when_set(self, tmp_path, monkeypatch):
        bundle = tmp_path / "ca-bundle.pem"
        bundle.write_text("-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n")
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(bundle))
        assert resolve_ca_bundle() == str(bundle)

    def test_ssl_cert_file_takes_precedence(self, tmp_path, monkeypatch):
        bundle1 = tmp_path / "ssl-cert.pem"
        bundle2 = tmp_path / "requests-bundle.pem"
        bundle1.write_text("cert1")
        bundle2.write_text("cert2")
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


class TestCreateSSLContext:
    def test_returns_none_when_no_bundle(self, monkeypatch):
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
        monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
        assert create_ssl_context() is None

    def test_returns_ssl_context_when_bundle_exists(self, tmp_path, monkeypatch):
        # Use a real PEM file (system cert) if available, otherwise skip
        import ssl
        bundle = tmp_path / "ca-bundle.pem"
        # Create a minimal valid PEM by using the default context's certs
        ctx = ssl.create_default_context()
        certs = ctx.get_ca_certs(binary_form=True)
        if not certs:
            pytest.skip("No system CA certs available for test")
        # Write one cert as PEM
        import base64
        pem = (
            "-----BEGIN CERTIFICATE-----\n"
            + base64.encodebytes(certs[0]).decode()
            + "-----END CERTIFICATE-----\n"
        )
        bundle.write_text(pem)
        monkeypatch.setenv("SSL_CERT_FILE", str(bundle))
        result = create_ssl_context()
        assert result is not None
        assert isinstance(result, ssl.SSLContext)


class TestInjectSystemTrustStore:
    def test_returns_false_when_truststore_not_installed(self):
        import importlib

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
        bundle.write_text("cert")
        monkeypatch.setenv("SSL_CERT_FILE", str(bundle))
        monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
        sync_ca_bundle_env_vars()
        assert os.environ["REQUESTS_CA_BUNDLE"] == str(bundle)

    def test_copies_requests_ca_bundle_to_ssl_cert_file(self, tmp_path, monkeypatch):
        bundle = tmp_path / "ca.pem"
        bundle.write_text("cert")
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(bundle))
        sync_ca_bundle_env_vars()
        assert os.environ["SSL_CERT_FILE"] == str(bundle)

    def test_does_not_overwrite_existing(self, tmp_path, monkeypatch):
        b1 = tmp_path / "a.pem"
        b2 = tmp_path / "b.pem"
        b1.write_text("cert1")
        b2.write_text("cert2")
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
            "tts_app.transcriber.local_faster_whisper._default_hf_cache_dir",
            return_value=str(tmp_path),
        ):
            result = find_cached_models()
        assert "small" in result

    def test_finds_model_in_custom_dir(self, tmp_path):
        self._make_hf_cache(tmp_path, "tiny", "Systran/faster-whisper-tiny")
        with patch(
            "tts_app.transcriber.local_faster_whisper._default_hf_cache_dir",
            return_value="/nonexistent",
        ):
            result = find_cached_models(str(tmp_path))
        assert "tiny" in result

    def test_finds_flat_model_dir(self, tmp_path):
        flat_dir = tmp_path / "faster-whisper-base"
        flat_dir.mkdir()
        (flat_dir / "config.json").write_text("{}")
        (flat_dir / "model.bin").write_bytes(b"\x00")
        with patch(
            "tts_app.transcriber.local_faster_whisper._default_hf_cache_dir",
            return_value=str(tmp_path),
        ):
            result = find_cached_models()
        assert "base" in result

    def test_returns_empty_when_no_models(self, tmp_path):
        with patch(
            "tts_app.transcriber.local_faster_whisper._default_hf_cache_dir",
            return_value=str(tmp_path),
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
            "tts_app.transcriber.local_faster_whisper._default_hf_cache_dir",
            return_value=str(tmp_path),
        ):
            result = find_cached_models()
        assert "small" not in result

    def test_multiple_models_found(self, tmp_path):
        self._make_hf_cache(tmp_path, "tiny", "Systran/faster-whisper-tiny")
        self._make_hf_cache(tmp_path, "small", "Systran/faster-whisper-small")
        with patch(
            "tts_app.transcriber.local_faster_whisper._default_hf_cache_dir",
            return_value=str(tmp_path),
        ):
            result = find_cached_models()
        assert "tiny" in result
        assert "small" in result
        # Should be in canonical order.
        assert result.index("tiny") < result.index("small")

    def test_both_hf_and_custom_dir(self, tmp_path):
        hf_dir = tmp_path / "hf_cache"
        hf_dir.mkdir()
        custom_dir = tmp_path / "custom"
        custom_dir.mkdir()
        self._make_hf_cache(hf_dir, "small", "Systran/faster-whisper-small")
        self._make_hf_cache(custom_dir, "tiny", "Systran/faster-whisper-tiny")
        with patch(
            "tts_app.transcriber.local_faster_whisper._default_hf_cache_dir",
            return_value=str(hf_dir),
        ):
            result = find_cached_models(str(custom_dir))
        assert "tiny" in result
        assert "small" in result


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
            "tts_app.transcriber.local_faster_whisper._default_hf_cache_dir",
            return_value=str(tmp_path),
        ):
            assert estimate_cached_model_bytes("unknown") == 0

    def test_estimates_hf_cache_size(self, tmp_path):
        self._make_hf_cache(tmp_path, "Systran/faster-whisper-small")
        with patch(
            "tts_app.transcriber.local_faster_whisper._default_hf_cache_dir",
            return_value=str(tmp_path),
        ):
            size = estimate_cached_model_bytes("small")
        assert size >= 10

    def test_uses_larger_of_duplicate_locations(self, tmp_path):
        hf_dir = tmp_path / "hf_cache"
        hf_dir.mkdir()
        custom_dir = tmp_path / "custom"
        custom_dir.mkdir()
        self._make_hf_cache(hf_dir, "Systran/faster-whisper-small")
        self._make_hf_cache(custom_dir, "Systran/faster-whisper-small")
        # Make custom location larger.
        big_blob = (
            custom_dir
            / "models--Systran--faster-whisper-small"
            / "blobs"
            / "big.bin"
        )
        big_blob.parent.mkdir(parents=True, exist_ok=True)
        big_blob.write_bytes(b"\x00" * 100)

        with patch(
            "tts_app.transcriber.local_faster_whisper._default_hf_cache_dir",
            return_value=str(hf_dir),
        ):
            size = estimate_cached_model_bytes("small", str(custom_dir))
        assert size >= 100


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
            "tts_app.transcriber.local_faster_whisper._default_hf_cache_dir",
            return_value=str(tmp_path),
        ):
            paths = cached_model_paths("small")
        assert model_root in paths

    def test_delete_cached_model_removes_directories(self, tmp_path):
        model_root = self._make_hf_cache(tmp_path, "Systran/faster-whisper-small")
        with patch(
            "tts_app.transcriber.local_faster_whisper._default_hf_cache_dir",
            return_value=str(tmp_path),
        ):
            removed = delete_cached_model("small")
        assert removed >= 1
        assert model_root.exists() is False


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
        from tts_app.transcriber.assemblyai_provider import AssemblyAITranscriber

        aai = types.ModuleType("assemblyai")
        aai.settings = MagicMock()

        class FakeSpeechModel:
            best = "best"

        class FakeConfig:
            def __init__(self, **kw):
                pass

        class FakeTranscriber:
            def transcribe(self, f, config=None):
                cause = Exception(
                    "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"
                )
                raise Exception("upload failed") from cause

        aai.SpeechModel = FakeSpeechModel
        aai.TranscriptionConfig = FakeConfig
        aai.Transcriber = FakeTranscriber
        aai.TranscriptStatus = MagicMock()

        t = AssemblyAITranscriber(api_key="test-key", aai_module=aai)
        with pytest.raises(TranscriptionError, match="SSL"):
            t.transcribe_batch(b"\x00" * 100)


class TestAssemblyAITestConnection:
    def test_successful_connection(self):
        from tts_app.transcriber.assemblyai_provider import AssemblyAITranscriber
        from unittest.mock import MagicMock

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
        from tts_app.transcriber.assemblyai_provider import AssemblyAITranscriber
        import urllib.error

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
        from tts_app.transcriber.assemblyai_provider import AssemblyAITranscriber

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
        from tts_app.transcriber.local_faster_whisper import _is_ssl_error

        exc = Exception("[SSL: CERTIFICATE_VERIFY_FAILED]")
        assert _is_ssl_error(exc) is True

        exc2 = Exception("timeout")
        assert _is_ssl_error(exc2) is False
