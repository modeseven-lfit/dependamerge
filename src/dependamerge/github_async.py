# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple, Union, AsyncIterator

import httpx
from aiolimiter import AsyncLimiter
from tenacity import (
    AsyncRetrying,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)

__all__ = [
    "GitHubAsync",
    "RateLimitError",
    "SecondaryRateLimitError",
    "GraphQLError",
]

GITHUB_API = "https://api.github.com"
GITHUB_GQL = "https://api.github.com/graphql"


class RateLimitError(Exception):
    """Raised when the primary GitHub API rate limit is reached."""


class SecondaryRateLimitError(Exception):
    """Raised when GitHub's secondary rate limit (abuse detection) triggers."""


class GraphQLError(Exception):
    """Raised for GraphQL errors returned by GitHub."""


class RetryableError(Exception):
    """Internal exception to signal tenacity that a retry should occur."""


def _now() -> float:
    return time.time()


def _is_secondary_rate_limited(body_text: str) -> bool:
    text = body_text.lower()
    # GitHub may return messages like:
    # "You have exceeded a secondary rate limit. Please wait a few minutes..."
    # Or "abuse detection mechanism"
    return "secondary rate limit" in text or "abuse detection" in text


def _is_primary_rate_limited(body_text: str) -> bool:
    text = body_text.lower()
    return "api rate limit exceeded" in text


def _is_transient_graphql_error(errors: Any) -> bool:
    try:
        # The structure is usually a list of dicts with "message".
        message_blob = json.dumps(errors).lower()
    except Exception:
        message_blob = str(errors).lower()
    # Heuristics for retryable GraphQL responses
    return any(
        needle in message_blob
        for needle in [
            "rate limit",  # may appear in graphql errors as well
            "something went wrong",  # generic GH error
            "timeout",
            "internal server error",
            "network timeout",
        ]
    )


def _is_retryable_status(status: int) -> bool:
    # Treat common transient statuses as retryable.
    return status in (429, 502, 503, 504)


async def _maybe_await(cb: Optional[Callable[..., Union[None, Awaitable[None]]]], *args, **kwargs) -> None:
    if cb is None:
        return
    result = cb(*args, **kwargs)
    if asyncio.iscoroutine(result):
        await result


class GitHubAsync:
    """
    Asynchronous GitHub API client with:
    - httpx AsyncClient for HTTP/2 support and connection pooling
    - Bounded concurrency via asyncio.Semaphore
    - Request rate limiting via aiolimiter.AsyncLimiter (RPS cap)
    - Robust retry with tenacity on transient errors and rate limits
    - Helpers for GraphQL and REST endpoints used by dependamerge
    """

    def __init__(
        self,
        token: Optional[str] = None,
        *,
        api_url: str = GITHUB_API,
        graphql_url: str = GITHUB_GQL,
        max_concurrency: int = 20,
        requests_per_second: float = 8.0,
        timeout: float = 20.0,
        user_agent: str = "dependamerge/async-client",
        verify: Union[bool, str] = True,
        proxies: Optional[Dict[str, str]] = None,
        logger: Optional[logging.Logger] = None,
        on_rate_limited: Optional[Callable[[float], Union[None, Awaitable[None]]]] = None,
        on_rate_limit_cleared: Optional[Callable[[], Union[None, Awaitable[None]]]] = None,
        on_metrics: Optional[Callable[[int, float], Union[None, Awaitable[None]]]] = None,
    ):
        """
        Initialize the async client.

        Args:
            token: GitHub token. If None, reads from GITHUB_TOKEN env var.
            api_url: Base REST API URL (set to your GHE base if needed).
            graphql_url: GraphQL endpoint URL.
            max_concurrency: Max concurrent in-flight requests.
            requests_per_second: Max requests per second (token bucket).
            timeout: Per-request timeout (seconds).
            user_agent: User-Agent header.
            verify: TLS verify flag or path to CA bundle.
            proxies: Optional httpx proxies mapping.
            logger: Optional logger for client messages.
            on_rate_limited: Callback invoked with reset_epoch when primary limit hit.
            on_rate_limit_cleared: Callback invoked when resuming after rate limit.
        """
        self.token = token or os.getenv("GITHUB_TOKEN")
        if not self.token:
            raise ValueError("GitHub token is required. Set GITHUB_TOKEN.")

        self.api_url = api_url.rstrip("/")
        self.graphql_url = graphql_url
        self._max_concurrency = max_concurrency
        self.semaphore = asyncio.Semaphore(self._max_concurrency)
        self._base_rps = requests_per_second
        self._current_rps = requests_per_second
        self.limiter = AsyncLimiter(max_rate=self._current_rps, time_period=1.0)
        self.log = logger or logging.getLogger(__name__)
        self._timeout = timeout

        self.on_rate_limited = on_rate_limited
        self.on_rate_limit_cleared = on_rate_limit_cleared
        self.on_metrics = on_metrics

        # Error tracking for adaptive throttling
        self._error_history: list[tuple[float, str]] = []  # List of (timestamp, error_type) tuples
        self._error_window = 300  # 5 minutes
        self._last_retry_after: Optional[float] = None
        self._adaptive_delay = 0.0
        self._last_adaptive_update: Optional[float] = None

        mounts = None
        if proxies:
            mounts = {}
            if "http" in proxies and proxies["http"]:
                mounts["http://"] = httpx.AsyncHTTPTransport(proxy=proxies["http"])
            if "https" in proxies and proxies["https"]:
                mounts["https://"] = httpx.AsyncHTTPTransport(proxy=proxies["https"])
        self._client = httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": user_agent,
            },
            http2=True,
            timeout=timeout,
            verify=verify,
            mounts=mounts,
        )

    async def __aenter__(self) -> "GitHubAsync":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close underlying httpx client."""
        await self._client.aclose()

    # --------------------------
    # Core request functionality
    # --------------------------

    def _parse_rate_limit_headers(self, r: httpx.Response) -> Tuple[int, int, Optional[float]]:
        """
        Parse GitHub rate limit headers.

        Returns:
            (remaining, limit, reset_epoch)
        """
        remaining = int(r.headers.get("X-RateLimit-Remaining", "1"))
        limit = int(r.headers.get("X-RateLimit-Limit", "60"))
        reset = r.headers.get("X-RateLimit-Reset")
        reset_epoch = float(reset) if reset else None
        return remaining, limit, reset_epoch

    async def _sleep_until(self, reset_epoch: float) -> None:
        now = _now()
        delay = max(0.0, reset_epoch - now)
        if delay > 0:
            await _maybe_await(self.on_rate_limited, reset_epoch)
            try:
                await asyncio.sleep(delay)
            finally:
                await _maybe_await(self.on_rate_limit_cleared)

    @retry(
        reraise=True,
        stop=stop_after_attempt(6),
        wait=wait_random_exponential(multiplier=0.5, max=10.0),
        retry=retry_if_exception_type(
            (httpx.TransportError, httpx.ReadTimeout, RetryableError, SecondaryRateLimitError)
        ),
    )
    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        """
        Low-level request with concurrency limit, RPS limit, and retry handling.
        Handles primary/secondary rate limits and transient statuses.
        """
        async with self.semaphore:
            async with self.limiter:
                r = await self._client.request(method, url, **kwargs)

        # 401 should not be retried (bad credentials)
        if r.status_code == 401:
            r.raise_for_status()

        # Primary rate limit: examine headers and body
        if r.status_code == 403:
            # Parse body defensively
            body_text: str
            try:
                body_text = r.text or ""
            except Exception:
                body_text = ""

            remaining, _, reset_epoch = self._parse_rate_limit_headers(r)

            # Secondary rate limit (abuse detection)
            if _is_secondary_rate_limited(body_text):
                retry_after = r.headers.get("Retry-After")
                if retry_after:
                    # Sleep the advised duration and signal retry
                    try:
                        delay = float(retry_after)
                        self._last_retry_after = delay
                        # Apply adaptive throttling based on Retry-After
                        self._apply_retry_after_throttling(delay)
                    except Exception:
                        delay = 5.0
                    self.log.warning("Secondary rate limit hit. Sleeping for %ss", delay)
                    await asyncio.sleep(max(0.0, delay))
                else:
                    # Fallback wait when no explicit Retry-After
                    delay = 10.0
                    self.log.warning("Secondary rate limit hit. Sleeping fallback %ss", delay)
                    await asyncio.sleep(delay)

                # Track error for adaptive throttling
                self._track_error("secondary_rate_limit")
                raise SecondaryRateLimitError("Secondary rate limit encountered")

            # Primary rate limit exhausted
            if remaining == 0 or _is_primary_rate_limited(body_text):
                # Check for Retry-After header on 429 responses
                retry_after = r.headers.get("Retry-After")
                if retry_after:
                    try:
                        delay = float(retry_after)
                        self._last_retry_after = delay
                        self.log.warning("Primary rate limit with Retry-After: %ss", delay)
                        await asyncio.sleep(max(0.0, delay))
                        self._apply_retry_after_throttling(delay)
                    except Exception:
                        pass
                elif reset_epoch:
                    self.log.warning("Primary rate limit exhausted. Waiting until reset: %s", reset_epoch)
                    await self._sleep_until(reset_epoch)
                else:
                    # If no reset header, backoff and retry
                    self.log.warning("Primary rate limit suspected without reset header; backing off")
                    await asyncio.sleep(5.0)

                # Track error for adaptive throttling
                self._track_error("primary_rate_limit")
                raise RetryableError("Primary rate limit reset waited; retrying")

        # Retryable transient statuses
        if _is_retryable_status(r.status_code):
            # Check for Retry-After on 429 or 503 responses
            retry_after = r.headers.get("Retry-After")
            if retry_after:
                try:
                    delay = float(retry_after)
                    self._last_retry_after = delay
                    self.log.debug("HTTP %s with Retry-After: %ss", r.status_code, delay)
                    await asyncio.sleep(max(0.0, delay))
                    self._apply_retry_after_throttling(delay)
                except Exception:
                    pass

            self._track_error("transient_error")
            self.log.debug("Retryable HTTP status %s received", r.status_code)
            raise RetryableError(f"Transient HTTP status: {r.status_code}")

        # All other errors -> raise
        r.raise_for_status()

        # Apply adaptive delay based on recent error patterns
        if self._adaptive_delay > 0:
            await asyncio.sleep(self._adaptive_delay)

        # Dynamic concurrency and RPS tuning based on latest headers and error history
        try:
            remaining, limit, reset_epoch = self._parse_rate_limit_headers(r)
            error_rate = self._get_recent_error_rate()

            # More aggressive throttling if we have recent errors or low rate limit remaining
            if limit > 0:
                remaining_ratio = remaining / max(1, limit)
                should_throttle = remaining_ratio < 0.1 or error_rate > 0.1

                if should_throttle:
                    # Reduce concurrency but keep a floor of 2
                    throttle_factor = 0.3 if error_rate > 0.2 else 0.5
                    new_concurrency = max(2, int(self._max_concurrency * throttle_factor))
                    if new_concurrency != self._max_concurrency:
                        self._max_concurrency = new_concurrency
                        self.semaphore = asyncio.Semaphore(self._max_concurrency)

                    # Reduce RPS but keep a floor of 1
                    new_rps = max(1.0, self._current_rps * throttle_factor)
                    if abs(new_rps - self._current_rps) >= 0.5:
                        self._current_rps = new_rps
                        self.limiter = AsyncLimiter(max_rate=self._current_rps, time_period=1.0)
            else:
                # Gradually increase limits when healthy, up to configured base values
                if self._max_concurrency < 20:
                    self._max_concurrency = min(20, self._max_concurrency + 1)
                    self.semaphore = asyncio.Semaphore(self._max_concurrency)
                if self._current_rps < self._base_rps:
                    self._current_rps = min(self._base_rps, self._current_rps + 1.0)
                    self.limiter = AsyncLimiter(max_rate=self._current_rps, time_period=1.0)
        except Exception:
            # Tuning is best-effort; never fail the request on tuning errors
            pass
        # Push current metrics to progress tracker (if provided)
        try:
            await _maybe_await(getattr(self, "on_metrics", None), self._max_concurrency, float(self._current_rps))
        except Exception:
            # Metrics reporting is best-effort
            pass
        return r

    # -------------
    # Public helpers
    # -------------

    async def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        r = await self._request("GET", f"{self.api_url}{path}", params=params)
        return r.json()  # type: ignore[no-any-return]

    async def post(self, path: str, json: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        r = await self._request("POST", f"{self.api_url}{path}", json=json)
        if r.status_code == 204:
            return {}
        return r.json()  # type: ignore[no-any-return]

    async def put(self, path: str, json: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        r = await self._request("PUT", f"{self.api_url}{path}", json=json)
        if r.status_code == 204:
            return {}
        return r.json()  # type: ignore[no-any-return]

    async def graphql(self, query: str, variables: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Execute a GraphQL query with retry for transient GraphQL errors.

        Note: HTTP-level issues are handled by _request's retry. Here we add
        retry for 200 OK responses that include GraphQL-level transient errors.
        """
        payload = {"query": query, "variables": variables or {}}

        async for attempt in AsyncRetrying(
            reraise=True,
            stop=stop_after_attempt(5),
            wait=wait_random_exponential(multiplier=0.5, max=10.0),
            retry=retry_if_exception_type((RetryableError, httpx.TransportError, httpx.ReadTimeout)),
        ):
            with attempt:
                r = await self._request("POST", self.graphql_url, json=payload)
                data = r.json()
                if "errors" in data and data["errors"]:
                    # Retry on transient errors, otherwise raise
                    if _is_transient_graphql_error(data["errors"]):
                        self.log.debug("Transient GraphQL error encountered; retrying: %s", data["errors"])
                        raise RetryableError("Transient GraphQL error")
                    # Non-transient; raise detailed error
                    raise GraphQLError(json.dumps(data["errors"]))
                if "data" not in data:
                    # Unexpected shape; treat as transient
                    self.log.debug("GraphQL response missing 'data'; retrying")
                    raise RetryableError("Malformed GraphQL response")
                return data["data"]  # type: ignore[no-any-return]

        # Should not be reached due to reraise=True; keep mypy happy
        raise GraphQLError("GraphQL request failed after retries")

    # -----------------------
    # GitHub operation helpers
    # -----------------------

    async def approve_pull_request(self, owner: str, repo: str, number: int, body: str) -> None:
        """
        Approve a pull request.

        REST: POST /repos/{owner}/{repo}/pulls/{pull_number}/reviews
        """
        await self.post(
            f"/repos/{owner}/{repo}/pulls/{number}/reviews",
            json={"event": "APPROVE", "body": body},
        )

    async def merge_pull_request(self, owner: str, repo: str, number: int, merge_method: str = "merge") -> bool:
        """
        Merge a pull request.

        REST: PUT /repos/{owner}/{repo}/pulls/{pull_number}/merge
        """
        data = await self.put(
            f"/repos/{owner}/{repo}/pulls/{number}/merge",
            json={"merge_method": merge_method},
        )
        # The API returns {"merged": true/false, ...}
        return bool(data.get("merged", False))

    async def update_branch(self, owner: str, repo: str, number: int) -> None:
        """
        Update a PR branch with the base branch.

        REST: PUT /repos/{owner}/{repo}/pulls/{pull_number}/update-branch
        """
        await self.put(f"/repos/{owner}/{repo}/pulls/{number}/update-branch")

    # -----------------------
    # Optional REST pagination
    # -----------------------

    async def get_paginated(
        self,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        per_page: int = 100,
        max_pages: Optional[int] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        """
        Iterate through a paginated REST collection.

        Yields JSON arrays/items for each page. Caller can flatten as needed.
        """
        page = 1
        while True:
            q = dict(params or {})
            q.update({"per_page": per_page, "page": page})
            r = await self._request("GET", f"{self.api_url}{path}", params=q)
            data = r.json()
            if not data:
                return
            yield data
            page += 1
            if max_pages and page > max_pages:
                return
            # Stop when Link header doesn't include 'rel="next"'
            link = r.headers.get("Link", "")
            if 'rel="next"' not in link:
                return

    # -----------------------
    # Error tracking and adaptive throttling
    # -----------------------

    def _track_error(self, error_type: str) -> None:
        """Track an error for adaptive throttling calculations."""
        current_time = _now()
        self._error_history.append((current_time, error_type))

        # Clean old entries outside the error window
        cutoff = current_time - self._error_window
        self._error_history = [(t, e) for t, e in self._error_history if t > cutoff]

    def _get_recent_error_rate(self) -> float:
        """Calculate the error rate in the recent window."""
        if not self._error_history:
            return 0.0

        current_time = _now()
        cutoff = current_time - self._error_window
        recent_errors = [e for t, e in self._error_history if t > cutoff]

        # Estimate request rate (very rough heuristic)
        # Assume we made approximately len(history) * 10 requests in the window
        estimated_requests = max(len(recent_errors) * 10, 1)
        return len(recent_errors) / estimated_requests

    def _apply_retry_after_throttling(self, retry_after_seconds: float) -> None:
        """Apply adaptive throttling based on Retry-After header values."""
        # If we're getting Retry-After frequently, add adaptive delay
        if retry_after_seconds > 30:
            # Long retry-after suggests we're hitting limits hard
            self._adaptive_delay = min(5.0, retry_after_seconds * 0.1)
        elif retry_after_seconds > 10:
            # Medium retry-after suggests moderate pressure
            self._adaptive_delay = min(2.0, retry_after_seconds * 0.05)
        else:
            # Short retry-after is normal, minimal delay
            self._adaptive_delay = min(1.0, retry_after_seconds * 0.02)

        # Gradually reduce adaptive delay over time
        if self._last_adaptive_update is not None:
            time_since_update = _now() - self._last_adaptive_update
            if time_since_update > 60:  # Reduce delay after 1 minute
                self._adaptive_delay *= 0.8

        self._last_adaptive_update = _now()
