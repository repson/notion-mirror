"""HTTP client for the Notion API with rate limiting and retries.

Design:
- Global pacing: at most ``requests_per_second`` requests (below the ~3 req/s
  limit of personal plans). No concurrency: slower but deterministic and
  gentle with the API.
- Retries: 429 and 529 honoring ``Retry-After``; 5xx only on idempotent
  requests (GET/DELETE and read-only POSTs like search/query).
- 429 with ``public_api_request_blocked`` is not retried (restricted access).
- Detects incomplete search-index responses (``request_status``).
"""

from __future__ import annotations

import json
import logging
import random
import time

import httpx

log = logging.getLogger("notion-mirror")

_BASE_URL = "https://api.notion.com"
_RETRY_STATUSES = {429, 529}
_SERVER_ERROR_STATUSES = {500, 502, 503, 504}


class NotionAPIError(Exception):
    """An error returned by the API (or a network error after all retries)."""

    def __init__(self, status: int, code: str, message: str, retry_after: float | None = None):
        super().__init__(f"[{status}] {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.retry_after = retry_after


class NotionHTTP:
    def __init__(
        self,
        token: str,
        notion_version: str = "2026-03-11",
        requests_per_second: float = 2.5,
        max_attempts: int = 6,
        timeout: float = 120.0,
    ):
        self.requests = 0  # requests made (including retries), for the report
        self._max_attempts = max_attempts
        self._min_interval = 1.0 / requests_per_second
        self._last_call = 0.0
        self._client = httpx.Client(
            base_url=_BASE_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Notion-Version": notion_version,
            },
            timeout=timeout,
        )
        # Unauthenticated client for downloads (signed S3/CDN URLs).
        self._download_client = httpx.Client(timeout=timeout, follow_redirects=True)

    # ------------------------------------------------------------------ core

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        params: dict | None = None,
        files: dict | None = None,
        idempotent: bool = True,
    ) -> httpx.Response:
        """A request with global pacing, retries and ``Retry-After`` handling."""
        attempt = 0
        while True:
            attempt += 1
            self._pace()
            self.requests += 1
            try:
                response = self._client.request(
                    method, path, json=json_body, params=params, files=files
                )
            except httpx.TransportError as exc:
                if attempt >= self._max_attempts:
                    raise NotionAPIError(0, "network_error", str(exc)) from exc
                self._sleep_backoff(attempt, reason=f"network error: {exc}")
                continue

            if response.status_code in _RETRY_STATUSES:
                retry_after = self._retry_after(response)
                blocked = _is_blocked(response)
                if blocked or attempt >= self._max_attempts:
                    raise _error_from(response, retry_after)
                self._sleep_backoff(attempt, retry_after=retry_after, reason=str(response.status_code))
                continue

            if response.status_code in _SERVER_ERROR_STATUSES:
                if idempotent and attempt < self._max_attempts:
                    self._sleep_backoff(attempt, retry_after=self._retry_after(response),
                                        reason=str(response.status_code))
                    continue
                raise _error_from(response)

            return response

    def get_json(self, path: str, params: dict | None = None) -> dict:
        response = self.request("GET", path, params=params)
        _raise_for_status(response)
        return _json_of(response)

    def post_json(self, path: str, body: dict, idempotent: bool = False) -> dict:
        response = self.request("POST", path, json_body=body, idempotent=idempotent)
        _raise_for_status(response)
        return _json_of(response)

    def patch_json(self, path: str, body: dict) -> dict:
        response = self.request("PATCH", path, json_body=body, idempotent=False)
        _raise_for_status(response)
        return _json_of(response)

    # ------------------------------------------------------------ pagination

    def paginate_get(self, path: str, params: dict | None = None):
        """Iterates ``results`` of a GET endpoint paginated by cursor."""
        params = dict(params or {})
        params.setdefault("page_size", 100)
        while True:
            payload = self.get_json(path, params=params)
            self._check_request_status(payload)
            yield from payload.get("results", [])
            if not payload.get("has_more"):
                return
            params["start_cursor"] = payload.get("next_cursor")

    def paginate_post(self, path: str, base_body: dict, idempotent: bool = True):
        """Iterates ``results`` of a POST endpoint paginated by cursor (search/query)."""
        body = dict(base_body)
        body.setdefault("page_size", 100)
        while True:
            payload = self.post_json(path, body, idempotent=idempotent)
            self._check_request_status(payload)
            yield from payload.get("results", [])
            if not payload.get("has_more"):
                return
            body["start_cursor"] = payload.get("next_cursor")

    def _check_request_status(self, payload: dict) -> None:
        status = payload.get("request_status") or {}
        if status.get("type") == "incomplete":
            log.warning(
                "Notion returned INCOMPLETE results (%s): the index is limited. "
                "The rest will be picked up on the next run or with --force.",
                status.get("incomplete_reason"),
            )

    # ------------------------------------------------------------ downloads

    def download_to(self, url: str, dest) -> tuple[bool, str | None]:
        """Downloads a file URL (no auth) to ``dest`` (Path)."""
        for attempt in range(1, 5):
            try:
                with self._download_client.stream("GET", url) as response:
                    if response.status_code == 200:
                        with open(dest, "wb") as fh:
                            for chunk in response.iter_bytes(1024 * 1024):
                                fh.write(chunk)
                        content_type = response.headers.get("content-type")
                        return True, content_type
                    if 400 <= response.status_code < 500:
                        log.debug("Download failed (%s): %s", response.status_code, url)
                        return False, None
                    log.debug("Download retryable (%s): %s", response.status_code, url)
            except httpx.TransportError as exc:
                log.debug("Network error during download: %s", exc)
            time.sleep(min(2**attempt, 20) + random.uniform(0, 0.5))
        return False, None

    def upload_multipart(self, path: str, file_path, filename: str, content_type: str) -> dict:
        """Sends a file via multipart/form-data (the file_uploads/send endpoint)."""
        with open(file_path, "rb") as fh:
            response = self.request(
                "POST",
                path,
                files={"file": (filename, fh, content_type or "application/octet-stream")},
                idempotent=False,
            )
        _raise_for_status(response)
        return _json_of(response)

    # ------------------------------------------------------------- utilities

    def _pace(self) -> None:
        now = time.monotonic()
        wait = self._last_call + self._min_interval - now
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def _retry_after(self, response: httpx.Response) -> float | None:
        raw = response.headers.get("Retry-After")
        if raw is not None:
            try:
                return min(float(raw), 300.0)
            except ValueError:
                pass
        try:
            body = response.json()
            raw = (body.get("additional_data") or {}).get("retry_after")
            if raw is not None:
                return min(float(raw), 300.0)
        except (json.JSONDecodeError, ValueError, AttributeError):
            pass
        return None

    def _sleep_backoff(
        self, attempt: int, retry_after: float | None = None, reason: str = ""
    ) -> None:
        delay = retry_after if retry_after is not None else min(2**attempt, 30.0)
        delay += random.uniform(0, 0.25)
        log.warning("Retry %d after %.1fs (%s)", attempt, delay, reason or "backoff")
        time.sleep(delay)

    def close(self) -> None:
        self._client.close()
        self._download_client.close()


def _json_of(response: httpx.Response) -> dict:
    try:
        return response.json()
    except json.JSONDecodeError as exc:
        raise NotionAPIError(response.status_code, "invalid_json", str(exc)) from exc


def _raise_for_status(response: httpx.Response) -> None:
    if response.status_code >= 400:
        raise _error_from(response)


def _error_from(response: httpx.Response, retry_after: float | None = None) -> NotionAPIError:
    try:
        body = response.json()
    except json.JSONDecodeError:
        body = {}
    retry_after = retry_after or _header_retry_after(response)
    return NotionAPIError(
        response.status_code,
        body.get("code", "http_error"),
        body.get("message", response.reason_phrase or "error"),
        retry_after,
    )


def _header_retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return min(float(raw), 300.0)
    except ValueError:
        return None


def _is_blocked(response: httpx.Response) -> bool:
    try:
        body = response.json()
        reason = (body.get("additional_data") or {}).get("rate_limit_reason", "")
        return reason == "public_api_request_blocked"
    except (json.JSONDecodeError, AttributeError):
        return False
