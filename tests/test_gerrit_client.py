# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation
"""
Tests for Gerrit REST client module.

This module tests the GerritRestClient's initialization, request handling,
retry behavior, XSSI guard stripping, and authentication.
"""

import urllib.error
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

from dependamerge.gerrit.client import (
    PYGERRIT2_AVAILABLE,
    GerritAuthError,
    GerritNotFoundError,
    GerritRestClient,
    GerritRestError,
    _calculate_backoff,
    _is_transient_error,
    _mask_secret,
    _strip_xssi_guard,
    build_client,
)


class TestMaskSecret:
    """Tests for the _mask_secret helper function."""

    def test_empty_string(self):
        """Test masking empty string."""
        assert _mask_secret("") == ""

    def test_short_string(self):
        """Test masking short strings (4 or fewer chars)."""
        assert _mask_secret("abc") == "****"
        assert _mask_secret("abcd") == "****"

    def test_normal_string(self):
        """Test masking normal length strings."""
        assert _mask_secret("password123") == "pa*******23"
        assert _mask_secret("secret") == "se**et"

    def test_long_string(self):
        """Test masking long strings."""
        result = _mask_secret("verylongsecretpassword")
        assert result.startswith("ve")
        assert result.endswith("rd")
        assert "****" in result


class TestIsTransientError:
    """Tests for transient error detection."""

    def test_timeout_error(self):
        """Test that timeout errors are detected as transient."""
        exc = Exception("Connection timed out")
        assert _is_transient_error(exc) is True

    def test_connection_reset(self):
        """Test that connection reset is detected as transient."""
        exc = Exception("Connection reset by peer")
        assert _is_transient_error(exc) is True

    def test_service_unavailable(self):
        """Test that service unavailable is detected as transient."""
        exc = Exception("Service unavailable")
        assert _is_transient_error(exc) is True

    def test_non_transient_error(self):
        """Test that non-transient errors are not flagged."""
        exc = Exception("Invalid request")
        assert _is_transient_error(exc) is False


class TestCalculateBackoff:
    """Tests for backoff calculation."""

    def test_first_attempt(self):
        """Test backoff for first attempt."""
        delay = _calculate_backoff(0, base_delay=1.0, max_delay=30.0, jitter=0.0)
        assert delay == 1.0

    def test_exponential_growth(self):
        """Test that delay grows exponentially."""
        delay0 = _calculate_backoff(0, base_delay=1.0, max_delay=60.0, jitter=0.0)
        delay1 = _calculate_backoff(1, base_delay=1.0, max_delay=60.0, jitter=0.0)
        delay2 = _calculate_backoff(2, base_delay=1.0, max_delay=60.0, jitter=0.0)

        assert delay0 == 1.0
        assert delay1 == 2.0
        assert delay2 == 4.0

    def test_max_delay_cap(self):
        """Test that delay is capped at max_delay."""
        delay = _calculate_backoff(10, base_delay=1.0, max_delay=30.0, jitter=0.0)
        assert delay == 30.0

    def test_jitter_adds_randomness(self):
        """Test that jitter adds randomness to delay."""
        delays = [
            _calculate_backoff(1, base_delay=1.0, max_delay=30.0, jitter=0.5)
            for _ in range(10)
        ]
        # With jitter, not all delays should be exactly the same
        # (statistically very unlikely for 10 samples)
        _ = set(delays)
        # At minimum, delays should be >= base
        assert all(d >= 2.0 for d in delays)
        # With 0.5 jitter, delays should be <= base * 1.5
        assert all(d <= 3.0 for d in delays)


class TestStripXssiGuard:
    """Tests for XSSI guard stripping."""

    def test_with_xssi_guard_newline(self):
        """Test stripping XSSI guard with newline."""
        text = ')]}\'\\n{"key": "value"}'
        result = _strip_xssi_guard(text.replace("\\n", "\n"))
        assert result == '{"key": "value"}'

    def test_with_xssi_guard_crlf(self):
        """Test stripping XSSI guard with CRLF."""
        text = ')]}\'\r\n{"key": "value"}'
        result = _strip_xssi_guard(text)
        assert result == '{"key": "value"}'

    def test_with_xssi_guard_no_newline(self):
        """Test stripping XSSI guard without newline."""
        text = ")]}'[1, 2, 3]"
        result = _strip_xssi_guard(text)
        assert result == "[1, 2, 3]"

    def test_without_xssi_guard(self):
        """Test that text without XSSI guard is unchanged."""
        text = '{"key": "value"}'
        result = _strip_xssi_guard(text)
        assert result == '{"key": "value"}'

    def test_empty_string(self):
        """Test stripping from empty string."""
        assert _strip_xssi_guard("") == ""


class TestGerritRestClientInit:
    """Tests for GerritRestClient initialization."""

    def test_basic_init(self):
        """Test basic client initialization."""
        client = GerritRestClient(base_url="https://gerrit.example.org/")

        assert client.base_url == "https://gerrit.example.org/"
        assert client.is_authenticated is False

    def test_init_normalizes_base_url(self):
        """Test that base URL is normalized to end with slash."""
        client = GerritRestClient(base_url="https://gerrit.example.org")
        assert client.base_url == "https://gerrit.example.org/"

    def test_init_with_auth(self):
        """Test client initialization with authentication."""
        client = GerritRestClient(
            base_url="https://gerrit.example.org/",
            auth=("user", "password"),
        )

        assert client.is_authenticated is True

    def test_init_with_empty_auth(self):
        """Test that empty auth values are handled."""
        client = GerritRestClient(
            base_url="https://gerrit.example.org/",
            auth=("", ""),
        )

        assert client.is_authenticated is False

    def test_init_with_partial_auth(self):
        """Test that partial auth is treated as no auth."""
        client = GerritRestClient(
            base_url="https://gerrit.example.org/",
            auth=("user", ""),
        )

        assert client.is_authenticated is False

    def test_repr(self):
        """Test string representation."""
        client = GerritRestClient(base_url="https://gerrit.example.org/")
        repr_str = repr(client)
        assert "GerritRestClient" in repr_str
        assert "gerrit.example.org" in repr_str


class TestGerritRestClientRequests:
    """Tests for GerritRestClient request methods."""

    def test_get_empty_path_raises(self, monkeypatch):
        """Test that empty path raises GerritRestError."""
        # Disable pygerrit2 to use urllib path
        monkeypatch.setattr("dependamerge.gerrit.client.PYGERRIT2_AVAILABLE", False)
        monkeypatch.setattr("dependamerge.gerrit.client._PygerritRestApi", None)

        client = GerritRestClient(base_url="https://gerrit.example.org/")

        with pytest.raises(GerritRestError, match="path is required"):
            client.get("")

    @patch("urllib.request.urlopen")
    def test_get_success(self, mock_urlopen, monkeypatch):
        """Test successful GET request."""
        # Disable pygerrit2 to use urllib path
        monkeypatch.setattr("dependamerge.gerrit.client.PYGERRIT2_AVAILABLE", False)
        monkeypatch.setattr("dependamerge.gerrit.client._PygerritRestApi", None)

        # Mock response
        response_data = '{"key": "value"}'
        mock_response = MagicMock()
        mock_response.read.return_value = response_data.encode("utf-8")
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        client = GerritRestClient(base_url="https://gerrit.example.org/")
        result = client.get("/changes/12345")

        assert result == {"key": "value"}

    @patch("urllib.request.urlopen")
    def test_get_with_xssi_guard(self, mock_urlopen, monkeypatch):
        """Test GET request with XSSI guard in response."""
        # Disable pygerrit2 to use urllib path
        monkeypatch.setattr("dependamerge.gerrit.client.PYGERRIT2_AVAILABLE", False)
        monkeypatch.setattr("dependamerge.gerrit.client._PygerritRestApi", None)

        response_data = ')]}\'\n{"key": "value"}'
        mock_response = MagicMock()
        mock_response.read.return_value = response_data.encode("utf-8")
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        client = GerritRestClient(base_url="https://gerrit.example.org/")
        result = client.get("/changes/12345")

        assert result == {"key": "value"}

    @patch("urllib.request.urlopen")
    def test_get_empty_response(self, mock_urlopen, monkeypatch):
        """Test GET request with empty response body."""
        # Disable pygerrit2 to use urllib path
        monkeypatch.setattr("dependamerge.gerrit.client.PYGERRIT2_AVAILABLE", False)
        monkeypatch.setattr("dependamerge.gerrit.client._PygerritRestApi", None)

        mock_response = MagicMock()
        mock_response.read.return_value = b""
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        client = GerritRestClient(base_url="https://gerrit.example.org/")
        result = client.get("/changes/12345")

        assert result == {}

    @patch("urllib.request.urlopen")
    def test_post_with_data(self, mock_urlopen, monkeypatch):
        """Test POST request with JSON data."""
        # Disable pygerrit2 to use urllib path
        monkeypatch.setattr("dependamerge.gerrit.client.PYGERRIT2_AVAILABLE", False)
        monkeypatch.setattr("dependamerge.gerrit.client._PygerritRestApi", None)

        response_data = '{"success": true}'
        mock_response = MagicMock()
        mock_response.read.return_value = response_data.encode("utf-8")
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        client = GerritRestClient(base_url="https://gerrit.example.org/")
        result = client.post("/changes/12345/review", {"labels": {"Code-Review": 2}})

        assert result == {"success": True}
        # Verify the request was made with correct data
        call_args = mock_urlopen.call_args
        request = call_args[0][0]
        assert request.method == "POST"
        assert request.data is not None


class TestGerritRestClientErrors:
    """Tests for GerritRestClient error handling."""

    @patch("urllib.request.urlopen")
    def test_401_raises_auth_error(self, mock_urlopen, monkeypatch):
        """Test that 401 response raises GerritAuthError."""
        # Disable pygerrit2 to use urllib path
        monkeypatch.setattr("dependamerge.gerrit.client.PYGERRIT2_AVAILABLE", False)
        monkeypatch.setattr("dependamerge.gerrit.client._PygerritRestApi", None)

        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://gerrit.example.org/changes/",
            code=401,
            msg="Unauthorized",
            hdrs={},  # type: ignore[arg-type]
            fp=BytesIO(b"Authentication required"),
        )

        client = GerritRestClient(base_url="https://gerrit.example.org/")

        with pytest.raises(GerritAuthError) as exc_info:
            client.get("/changes/12345")

        assert exc_info.value.status_code == 401

    @patch("urllib.request.urlopen")
    def test_403_raises_auth_error(self, mock_urlopen, monkeypatch):
        """Test that 403 response raises GerritAuthError."""
        # Disable pygerrit2 to use urllib path
        monkeypatch.setattr("dependamerge.gerrit.client.PYGERRIT2_AVAILABLE", False)
        monkeypatch.setattr("dependamerge.gerrit.client._PygerritRestApi", None)

        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://gerrit.example.org/changes/",
            code=403,
            msg="Forbidden",
            hdrs={},  # type: ignore[arg-type]
            fp=BytesIO(b"Access denied"),
        )

        client = GerritRestClient(base_url="https://gerrit.example.org/")

        with pytest.raises(GerritAuthError) as exc_info:
            client.get("/changes/12345")

        assert exc_info.value.status_code == 403

    @patch("urllib.request.urlopen")
    def test_404_raises_not_found_error(self, mock_urlopen, monkeypatch):
        """Test that 404 response raises GerritNotFoundError."""
        # Disable pygerrit2 to use urllib path
        monkeypatch.setattr("dependamerge.gerrit.client.PYGERRIT2_AVAILABLE", False)
        monkeypatch.setattr("dependamerge.gerrit.client._PygerritRestApi", None)

        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://gerrit.example.org/changes/",
            code=404,
            msg="Not Found",
            hdrs={},  # type: ignore[arg-type]
            fp=BytesIO(b"Change not found"),
        )

        client = GerritRestClient(base_url="https://gerrit.example.org/")

        with pytest.raises(GerritNotFoundError) as exc_info:
            client.get("/changes/99999")

        assert exc_info.value.status_code == 404

    @patch("urllib.request.urlopen")
    def test_500_raises_rest_error(self, mock_urlopen, monkeypatch):
        """Test that 500 response raises GerritRestError."""
        # Disable pygerrit2 to use urllib path
        monkeypatch.setattr("dependamerge.gerrit.client.PYGERRIT2_AVAILABLE", False)
        monkeypatch.setattr("dependamerge.gerrit.client._PygerritRestApi", None)

        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://gerrit.example.org/changes/",
            code=500,
            msg="Internal Server Error",
            hdrs={},  # type: ignore[arg-type]
            fp=BytesIO(b"Server error"),
        )

        client = GerritRestClient(
            base_url="https://gerrit.example.org/",
            max_attempts=1,  # Disable retries for this test
        )

        with pytest.raises(GerritRestError) as exc_info:
            client.get("/changes/12345")

        assert exc_info.value.status_code == 500

    @patch("urllib.request.urlopen")
    def test_invalid_json_raises_error(self, mock_urlopen, monkeypatch):
        """Test that invalid JSON raises GerritRestError."""
        # Disable pygerrit2 to use urllib path
        monkeypatch.setattr("dependamerge.gerrit.client.PYGERRIT2_AVAILABLE", False)
        monkeypatch.setattr("dependamerge.gerrit.client._PygerritRestApi", None)

        mock_response = MagicMock()
        mock_response.read.return_value = b"not valid json"
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        client = GerritRestClient(base_url="https://gerrit.example.org/")

        with pytest.raises(GerritRestError, match="Failed to parse JSON"):
            client.get("/changes/12345")


class TestGerritRestClientRetry:
    """Tests for retry behavior."""

    @patch("time.sleep")
    @patch("urllib.request.urlopen")
    def test_retry_on_503(self, mock_urlopen, mock_sleep, monkeypatch):
        """Test that 503 errors trigger retry."""
        # Disable pygerrit2 to use urllib path
        monkeypatch.setattr("dependamerge.gerrit.client.PYGERRIT2_AVAILABLE", False)
        monkeypatch.setattr("dependamerge.gerrit.client._PygerritRestApi", None)

        # First call fails, second succeeds
        error = urllib.error.HTTPError(
            url="https://gerrit.example.org/changes/",
            code=503,
            msg="Service Unavailable",
            hdrs={},  # type: ignore[arg-type]
            fp=BytesIO(b"Try again"),
        )
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"key": "value"}'
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        mock_urlopen.side_effect = [error, mock_response]

        client = GerritRestClient(
            base_url="https://gerrit.example.org/",
            max_attempts=3,
        )
        result = client.get("/changes/12345")

        assert result == {"key": "value"}
        assert mock_urlopen.call_count == 2
        assert mock_sleep.call_count == 1

    @patch("time.sleep")
    @patch("urllib.request.urlopen")
    def test_no_retry_on_401(self, mock_urlopen, mock_sleep, monkeypatch):
        """Test that 401 errors do not trigger retry."""
        # Disable pygerrit2 to use urllib path
        monkeypatch.setattr("dependamerge.gerrit.client.PYGERRIT2_AVAILABLE", False)
        monkeypatch.setattr("dependamerge.gerrit.client._PygerritRestApi", None)

        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://gerrit.example.org/changes/",
            code=401,
            msg="Unauthorized",
            hdrs={},  # type: ignore[arg-type]
            fp=BytesIO(b"Auth required"),
        )

        client = GerritRestClient(
            base_url="https://gerrit.example.org/",
            max_attempts=3,
        )

        with pytest.raises(GerritAuthError):
            client.get("/changes/12345")

        assert mock_urlopen.call_count == 1
        assert mock_sleep.call_count == 0


class TestBuildClient:
    """Tests for the build_client factory function."""

    def test_build_client_basic(self):
        """Test building client with just hostname."""
        client = build_client("gerrit.example.org")

        assert "gerrit.example.org" in client.base_url
        assert client.is_authenticated is False

    def test_build_client_with_base_path(self):
        """Test building client with base path."""
        client = build_client("gerrit.example.org", base_path="infra")

        assert "gerrit.example.org/infra/" in client.base_url

    def test_build_client_with_credentials(self):
        """Test building client with explicit credentials."""
        client = build_client(
            "gerrit.example.org",
            username="testuser",
            password="testpass",
        )

        assert client.is_authenticated is True

    def test_build_client_from_env(self, monkeypatch):
        """Test building client with credentials from environment."""
        monkeypatch.setenv("GERRIT_USERNAME", "envuser")
        monkeypatch.setenv("GERRIT_PASSWORD", "envpass")

        client = build_client("gerrit.example.org")

        assert client.is_authenticated is True

    def test_build_client_env_fallback(self, monkeypatch):
        """Test credential fallback from GERRIT_HTTP_USER/PASSWORD."""
        monkeypatch.setenv("GERRIT_HTTP_USER", "httpuser")
        monkeypatch.setenv("GERRIT_HTTP_PASSWORD", "httppass")

        client = build_client("gerrit.example.org")

        assert client.is_authenticated is True

    def test_build_client_explicit_overrides_env(self, monkeypatch):
        """Test that explicit credentials override environment."""
        monkeypatch.setenv("GERRIT_USERNAME", "envuser")
        monkeypatch.setenv("GERRIT_PASSWORD", "envpass")

        client = build_client(
            "gerrit.example.org",
            username="explicit",
            password="credentials",
        )

        assert client.is_authenticated is True
        # We can't directly check which credentials were used,
        # but the test ensures no errors occur with override


class TestPygerrit2Integration:
    """Tests related to pygerrit2 availability."""

    def test_pygerrit2_available_is_boolean(self):
        """Test that PYGERRIT2_AVAILABLE is a boolean."""
        assert isinstance(PYGERRIT2_AVAILABLE, bool)

    def test_client_works_without_pygerrit2(self, monkeypatch):
        """Test that client works when pygerrit2 is not available."""
        # Simulate pygerrit2 not being available
        monkeypatch.setattr("dependamerge.gerrit.client.PYGERRIT2_AVAILABLE", False)
        monkeypatch.setattr("dependamerge.gerrit.client._PygerritRestApi", None)

        client = GerritRestClient(base_url="https://gerrit.example.org/")

        # Client should initialize successfully
        assert client.base_url == "https://gerrit.example.org/"
