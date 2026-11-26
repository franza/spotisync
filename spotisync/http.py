"""A small JSON HTTP client with retries, backoff and token refresh."""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Protocol

import requests

log = logging.getLogger(__name__)

RETRY_STATUSES = {429, 500, 502, 503, 504}


class ApiError(Exception):
    """An HTTP error response from an API."""

    def __init__(self, method: str, url: str, status: int, body: str):
        self.method = method
        self.url = url
        self.status = status
        self.body = body
        super().__init__(f"{method} {url} -> HTTP {status}: {body[:500]}")


class TokenSource(Protocol):
    def access_token(self) -> str: ...
    def force_refresh(self) -> str: ...


def parse_retry_after(value: str | None, default: float) -> float:
    """Parse a Retry-After header, which is either seconds or an HTTP date."""
    if not value:
        return default
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return default
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


class ApiClient:
    """Send authenticated JSON requests to one API base URL.

    Retries 429 and 5xx responses and connection errors with backoff, and
    refreshes the access token once after a 401.
    """

    def __init__(
        self,
        base_url: str,
        tokens: TokenSource,
        *,
        headers: dict[str, str] | None = None,
        max_retries: int = 6,
        min_interval: float = 0.0,
        timeout: float = 30.0,
        session: requests.Session | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self._tokens = tokens
        self._session = session or requests.Session()
        self._session.headers.update(headers or {})
        self._max_retries = max_retries
        self._min_interval = min_interval
        self._timeout = timeout
        self._last_request = 0.0

    def url_for(self, path_or_url: str) -> str:
        if path_or_url.startswith(("http://", "https://")):
            return path_or_url
        return f"{self.base_url}/{path_or_url.lstrip('/')}"

    def _throttle(self) -> None:
        wait = self._min_interval - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def request(
        self,
        method: str,
        path_or_url: str,
        *,
        params: Any = None,
        json: Any = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """Send a request and return the decoded JSON body, or None if empty."""
        url = self.url_for(path_or_url)
        refreshed = False
        attempt = 0
        while True:
            self._throttle()
            req_headers = {"Authorization": f"Bearer {self._tokens.access_token()}"}
            req_headers.update(headers or {})
            try:
                resp = self._session.request(
                    method, url, params=params, json=json,
                    headers=req_headers, timeout=self._timeout,
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                if attempt >= self._max_retries:
                    raise
                delay = self._backoff(attempt)
                log.warning("%s %s failed (%s); retrying in %.1fs", method, url, exc, delay)
                time.sleep(delay)
                attempt += 1
                continue

            if resp.status_code == 401 and not refreshed:
                log.debug("401 from %s; refreshing token", url)
                self._tokens.force_refresh()
                refreshed = True
                continue

            if resp.status_code in RETRY_STATUSES and attempt < self._max_retries:
                delay = parse_retry_after(
                    resp.headers.get("Retry-After"), self._backoff(attempt)
                )
                log.warning(
                    "HTTP %s from %s %s; retrying in %.1fs",
                    resp.status_code, method, url, delay,
                )
                time.sleep(delay)
                attempt += 1
                continue

            if not resp.ok:
                raise ApiError(method, resp.url, resp.status_code, resp.text)

            if not resp.content or not resp.content.strip():
                return None
            return resp.json()

    @staticmethod
    def _backoff(attempt: int) -> float:
        return min(2.0 ** attempt, 60.0) + random.uniform(0, 0.5)

    def get(self, path_or_url: str, **kwargs: Any) -> Any:
        return self.request("GET", path_or_url, **kwargs)

    def post(self, path_or_url: str, **kwargs: Any) -> Any:
        return self.request("POST", path_or_url, **kwargs)
