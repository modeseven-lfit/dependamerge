# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation
"""
Gerrit REST client with retry, timeout, and transient error handling.

This module provides a typed wrapper for Gerrit REST API calls with:
- Bounded retries using exponential backoff with jitter
- Request timeouts
- Transient error classification (HTTP 5xx/429 and network errors)
- XSSI guard stripping for Gerrit JSON responses

The client prefers pygerrit2 when available and falls back to urllib.

Usage:
    from dependamerge.gerrit.client import GerritRestClient, build_client

    client = build_client("gerrit.example.org", timeout=10.0)
    changes = client.get("/changes/?q=status:open&n=10")
"""

from __future__ import annotations

import base64
import json
import logging
import os
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urljoin, urlparse

log = logging.getLogger("dependamerge.gerrit.client")


# Optional pygerrit2 import
try:
    from pygerrit2 import GerritRestAPI as _PygerritRestApi
    from pygerrit2 import HTTPBasicAuth as _PygerritHttpAuth

    PYGERRIT2_AVAILABLE = True
except ImportError:
    _PygerritRestApi = None  # type: ignore[assignment, misc]
    _PygerritHttpAuth = None  # type: ignore[assignment, misc]
    PYGERRIT2_AVAILABLE = False


_MSG_PYGERRIT2_REQUIRED_AUTH: Final[str] = (
    "pygerrit2 is required for HTTP authentication"
)

_TRANSIENT_ERR_SUBSTRINGS: Final[tuple[str, ...]] = (
    "timed out",
    "temporarily unavailable",
    "temporary failure",
    "connection reset",
    "connection aborted",
    "broken pipe",
    "connection refused",
    "bad gateway",
    "service unavailable",
    "gateway timeout",
)

_RETRYABLE_HTTP_CODES: Final[frozenset[int]] = frozenset(
    {429, 500, 502, 503, 504}
)


class GerritRestError(RuntimeError):
    """Raised for non-retryable REST errors or exhausted retries."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        response_body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class GerritAuthError(GerritRestError):
    """Raised for authentication failures (401/403)."""


class GerritNotFoundError(GerritRestError):
    """Raised when a resource is not found (404)."""


@dataclass(frozen=True)
class _Auth:
    """Authentication credentials."""

    user: str
    password: str


def _mask_secret(s: str) -> str:
    """Mask a secret for logging, preserving first/last 2 chars."""
    if not s:
        return s
    if len(s) <= 4:
        return "****"
    return s[:2] + "*" * (len(s) - 4) + s[-2:]


def _is_transient_error(exc: Exception) -> bool:
    """Check if an exception represents a transient/retryable error."""
    exc_str = str(exc).lower()
    return any(sub in exc_str for sub in _TRANSIENT_ERR_SUBSTRINGS)


def _calculate_backoff(
    attempt: int,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    jitter: float = 0.5,
) -> float:
    """Calculate exponential backoff delay with jitter."""
    delay = min(base_delay * (2**attempt), max_delay)
    jitter_amount = delay * jitter * float(random.random())
    return float(delay + jitter_amount)


def _strip_xssi_guard(text: str) -> str:
    """
    Strip Gerrit's XSSI guard from JSON responses.

    Gerrit prepends ")]}'" to JSON responses to prevent JSON hijacking.
    This function removes that prefix if present.
    """
    if text.startswith(")]}'"):
        # Common patterns: ")]}'\n" or ")]}'\r\n"
        if text[4:6] == "\r\n":
            return text[6:]
        if text[4:5] == "\n":
            return text[5:]
        return text[4:]
    return text


def _json_loads(text: str) -> Any:
    """Parse JSON, providing clear error messages."""
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        msg = f"Failed to parse JSON response: {exc}"
        raise GerritRestError(msg) from exc


class GerritRestClient:
    """
    REST client for Gerrit with retry and timeout handling.

    This client provides methods for making authenticated requests to
    Gerrit's REST API with automatic retry on transient failures.

    If pygerrit2 is available, it uses that library for requests.
    Otherwise, it falls back to urllib with manual request construction.
    """

    def __init__(
        self,
        *,
        base_url: str,
        auth: tuple[str, str] | None = None,
        timeout: float = 10.0,
        max_attempts: int = 5,
    ) -> None:
        """
        Initialize the Gerrit REST client.

        Args:
            base_url: The base URL of the Gerrit server (e.g.,
                     "https://gerrit.example.org/").
            auth: Optional tuple of (username, password) for HTTP Basic auth.
            timeout: Request timeout in seconds.
            max_attempts: Maximum number of retry attempts for transient errors.
        """
        # Normalize base URL to end with '/'
        self._base_url: str = base_url.rstrip("/") + "/"
        self._timeout: float = float(timeout)
        self._max_attempts: int = int(max_attempts)
        self._auth: _Auth | None = None

        if auth and auth[0] and auth[1]:
            self._auth = _Auth(auth[0], auth[1])

        # Build pygerrit client if library is present
        self._client: Any = None
        if PYGERRIT2_AVAILABLE and _PygerritRestApi is not None:
            if self._auth is not None:
                if _PygerritHttpAuth is None:
                    raise GerritRestError(_MSG_PYGERRIT2_REQUIRED_AUTH)
                self._client = _PygerritRestApi(
                    url=self._base_url,
                    auth=_PygerritHttpAuth(
                        self._auth.user, self._auth.password
                    ),
                )
            else:
                self._client = _PygerritRestApi(url=self._base_url)

        log.debug(
            "GerritRestClient initialized: base_url=%s, timeout=%.1fs, "
            "max_attempts=%d, auth_user=%s, pygerrit2=%s",
            self._base_url,
            self._timeout,
            self._max_attempts,
            self._auth.user if self._auth else "(none)",
            "available" if PYGERRIT2_AVAILABLE else "not available",
        )

    @property
    def base_url(self) -> str:
        """Get the base URL of the Gerrit server."""
        return self._base_url

    @property
    def is_authenticated(self) -> bool:
        """Check if the client has authentication credentials."""
        return self._auth is not None

    def get(self, path: str) -> Any:
        """
        Perform an HTTP GET request.

        Args:
            path: The API path (e.g., "/changes/12345").

        Returns:
            The parsed JSON response.

        Raises:
            GerritRestError: On non-retryable errors or exhausted retries.
            GerritAuthError: On authentication failures.
            GerritNotFoundError: When the resource is not found.
        """
        return self._request_with_retry("GET", path)

    def post(self, path: str, data: Any | None = None) -> Any:
        """
        Perform an HTTP POST request.

        Args:
            path: The API path.
            data: Optional JSON-serializable data to send.

        Returns:
            The parsed JSON response.

        Raises:
            GerritRestError: On non-retryable errors or exhausted retries.
            GerritAuthError: On authentication failures.
        """
        return self._request_with_retry("POST", path, data=data)

    def put(self, path: str, data: Any | None = None) -> Any:
        """
        Perform an HTTP PUT request.

        Args:
            path: The API path.
            data: Optional JSON-serializable data to send.

        Returns:
            The parsed JSON response.

        Raises:
            GerritRestError: On non-retryable errors or exhausted retries.
            GerritAuthError: On authentication failures.
        """
        return self._request_with_retry("PUT", path, data=data)

    def delete(self, path: str) -> Any:
        """
        Perform an HTTP DELETE request.

        Args:
            path: The API path.

        Returns:
            The parsed JSON response (may be empty).

        Raises:
            GerritRestError: On non-retryable errors or exhausted retries.
            GerritAuthError: On authentication failures.
        """
        return self._request_with_retry("DELETE", path)

    def _request_with_retry(
        self,
        method: str,
        path: str,
        data: Any | None = None,
    ) -> Any:
        """Perform a request with automatic retry on transient failures."""
        last_exception: Exception | None = None

        for attempt in range(self._max_attempts):
            try:
                return self._request(method, path, data)
            except GerritAuthError:
                # Don't retry authentication failures
                raise
            except GerritNotFoundError:
                # Don't retry not found errors
                raise
            except GerritRestError as exc:
                last_exception = exc
                # Check if this is a retryable HTTP error
                if exc.status_code and exc.status_code in _RETRYABLE_HTTP_CODES:
                    if attempt < self._max_attempts - 1:
                        delay = _calculate_backoff(attempt)
                        log.warning(
                            "Gerrit REST %s %s failed (HTTP %d), "
                            "retrying in %.1fs (attempt %d/%d)",
                            method,
                            path,
                            exc.status_code,
                            delay,
                            attempt + 1,
                            self._max_attempts,
                        )
                        time.sleep(delay)
                        continue
                raise
            except Exception as exc:
                last_exception = exc
                # Check for transient network errors
                if _is_transient_error(exc):
                    if attempt < self._max_attempts - 1:
                        delay = _calculate_backoff(attempt)
                        log.warning(
                            "Gerrit REST %s %s failed (%s), "
                            "retrying in %.1fs (attempt %d/%d)",
                            method,
                            path,
                            exc,
                            delay,
                            attempt + 1,
                            self._max_attempts,
                        )
                        time.sleep(delay)
                        continue
                raise GerritRestError(
                    f"Gerrit REST {method} {path} failed: {exc}"
                ) from exc

        # Should not reach here, but just in case
        if last_exception:
            raise last_exception
        raise GerritRestError(f"Gerrit REST {method} {path} failed unexpectedly")

    def _request(
        self,
        method: str,
        path: str,
        data: Any | None = None,
    ) -> Any:
        """Perform a single HTTP request (no retry)."""
        if not path:
            raise ValueError("path is required")

        # Normalize path (ensure it doesn't double up with base_url)
        rel_path = path[1:] if path.startswith("/") else path

        # Use pygerrit2 for GET requests if available
        # Note: pygerrit2 automatically adds /a/ prefix when HTTPBasicAuth is configured
        if self._client is not None and method == "GET" and data is None:
            pygerrit_path = path if path.startswith("/") else f"/{path}"
            return self._request_via_pygerrit(pygerrit_path)

        # For urllib requests with authentication, Gerrit requires the /a/ prefix
        # https://gerrit-review.googlesource.com/Documentation/rest-api.html#authentication
        if self._auth is not None and not rel_path.startswith("a/"):
            rel_path = f"a/{rel_path}"

        url = urljoin(self._base_url, rel_path)

        # Fall back to urllib
        return self._request_via_urllib(method, url, path, data)

    def _request_via_pygerrit(self, path: str) -> Any:
        """Perform a GET request using pygerrit2."""
        log.debug("Gerrit REST GET via pygerrit2: %s", path)
        try:
            # pygerrit2.get expects a path starting with /
            api_path = path if path.startswith("/") else f"/{path}"
            return self._client.get(api_path)
        except Exception as exc:
            # pygerrit2 may raise various exceptions
            exc_str = str(exc).lower()
            if "401" in exc_str or "unauthorized" in exc_str:
                raise GerritAuthError(
                    f"Authentication failed: {exc}", status_code=401
                ) from exc
            if "403" in exc_str or "forbidden" in exc_str:
                raise GerritAuthError(
                    f"Access forbidden: {exc}", status_code=403
                ) from exc
            if "404" in exc_str or "not found" in exc_str:
                raise GerritNotFoundError(
                    f"Resource not found: {exc}", status_code=404
                ) from exc
            raise GerritRestError(f"Gerrit REST GET failed: {exc}") from exc

    def _request_via_urllib(
        self,
        method: str,
        url: str,
        path: str,
        data: Any | None = None,
    ) -> Any:
        """Perform a request using urllib."""
        headers = {"Accept": "application/json"}
        body_bytes: bytes | None = None

        if data is not None:
            headers["Content-Type"] = "application/json"
            body_bytes = json.dumps(data).encode("utf-8")

        if self._auth is not None:
            token = base64.b64encode(
                f"{self._auth.user}:{self._auth.password}".encode()
            ).decode("ascii")
            headers["Authorization"] = f"Basic {token}"

        # Validate URL scheme
        scheme = urlparse(url).scheme
        if scheme not in ("http", "https"):
            raise GerritRestError(f"Unsupported URL scheme: {scheme}")

        req = urllib.request.Request(
            url, data=body_bytes, method=method, headers=headers
        )

        log.debug(
            "Gerrit REST %s %s (auth=%s)",
            method,
            url,
            "yes" if self._auth else "no",
        )

        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                content = resp.read()
                text = content.decode("utf-8", errors="replace")
                text = _strip_xssi_guard(text)
                if not text.strip():
                    return {}
                return _json_loads(text)

        except urllib.error.HTTPError as http_exc:
            status = http_exc.code
            body = ""
            try:
                body = http_exc.read().decode("utf-8", errors="replace")
            except Exception as exc:
                log.debug("Failed to read HTTP error response body: %s", exc)

            if status == 401:
                raise GerritAuthError(
                    f"Authentication failed for {path}",
                    status_code=status,
                    response_body=body,
                ) from http_exc
            if status == 403:
                raise GerritAuthError(
                    f"Access forbidden for {path}",
                    status_code=status,
                    response_body=body,
                ) from http_exc
            if status == 404:
                raise GerritNotFoundError(
                    f"Resource not found: {path}",
                    status_code=status,
                    response_body=body,
                ) from http_exc

            raise GerritRestError(
                f"Gerrit REST {method} {path} failed with HTTP {status}",
                status_code=status,
                response_body=body,
            ) from http_exc

        except urllib.error.URLError as url_exc:
            raise GerritRestError(
                f"Gerrit REST {method} {path} failed: {url_exc.reason}"
            ) from url_exc

    def __repr__(self) -> str:
        """String representation for debugging."""
        masked = ""
        if self._auth is not None:
            masked = f"{self._auth.user}:{_mask_secret(self._auth.password)}@"
        return f"GerritRestClient(base_url='{masked}{self._base_url}')"


def build_client(
    host: str,
    *,
    base_path: str | None = None,
    timeout: float = 10.0,
    max_attempts: int = 5,
    username: str | None = None,
    password: str | None = None,
) -> GerritRestClient:
    """
    Build a GerritRestClient for a given host.

    This factory function constructs the appropriate base URL and reads
    authentication credentials from arguments or environment variables.

    Args:
        host: Gerrit hostname (without scheme).
        base_path: Optional base path (e.g., "infra"). If None, no base path.
        timeout: Request timeout in seconds.
        max_attempts: Maximum retry attempts for transient failures.
        username: HTTP username. Falls back to GERRIT_USERNAME or
                  GERRIT_HTTP_USER environment variables.
        password: HTTP password. Falls back to GERRIT_PASSWORD or
                  GERRIT_HTTP_PASSWORD environment variables.

    Returns:
        A configured GerritRestClient instance.
    """
    # Build base URL
    if base_path:
        base_url = f"https://{host}/{base_path.strip('/')}/"
    else:
        base_url = f"https://{host}/"

    # Resolve authentication from arguments or environment
    user = (
        (username or "").strip()
        or os.getenv("GERRIT_USERNAME", "").strip()
        or os.getenv("GERRIT_HTTP_USER", "").strip()
    )
    passwd = (
        (password or "").strip()
        or os.getenv("GERRIT_PASSWORD", "").strip()
        or os.getenv("GERRIT_HTTP_PASSWORD", "").strip()
    )

    auth: tuple[str, str] | None = None
    if user and passwd:
        auth = (user, passwd)

    return GerritRestClient(
        base_url=base_url,
        auth=auth,
        timeout=timeout,
        max_attempts=max_attempts,
    )


__all__ = [
    "GerritAuthError",
    "GerritNotFoundError",
    "GerritRestClient",
    "GerritRestError",
    "PYGERRIT2_AVAILABLE",
    "build_client",
]
