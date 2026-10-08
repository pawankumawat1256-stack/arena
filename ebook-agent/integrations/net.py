"""HTTP helper shared by the integrations: timeouts, retries with backoff, and readable errors."""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_BACKOFF_SECONDS = 60.0


class ApiError(RuntimeError):
    """A remote API call failed. `retryable` is True when trying again later may succeed."""

    def __init__(
        self,
        service: str,
        message: str,
        *,
        status: int | None = None,
        code: int | None = None,
        subcode: int | None = None,
        retryable: bool = False,
    ) -> None:
        self.service = service
        self.status = status
        self.code = code
        self.subcode = subcode
        self.retryable = retryable
        detail = f" {status}" if status else ""
        super().__init__(f"{service} error{detail}: {message}")


@dataclass(frozen=True)
class _ErrorInfo:
    message: str
    code: int | None
    subcode: int | None


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _error_info(response: httpx.Response) -> _ErrorInfo:
    try:
        body = response.json()
    except ValueError:
        return _ErrorInfo((response.text or response.reason_phrase)[:300], None, None)
    if isinstance(body, dict) and isinstance(body.get("error"), dict):  # Graph API style
        error = body["error"]
        return _ErrorInfo(
            str(error.get("message") or error.get("type") or "unknown error"),
            _as_int(error.get("code")),
            _as_int(error.get("error_subcode")),
        )
    if isinstance(body, dict):  # Notion and Gumroad style
        return _ErrorInfo(str(body.get("message") or body.get("code") or "unknown error"), _as_int(body.get("code")), None)
    return _ErrorInfo(str(body)[:300], None, None)


def _sleep_before_retry(response: httpx.Response | None, attempt: int) -> None:
    delay: float | None = None
    if response is not None:
        header = response.headers.get("Retry-After", "")
        if header.isdigit():
            delay = float(header)
    if delay is None:
        delay = min(MAX_BACKOFF_SECONDS, 2.0**attempt) + random.uniform(0, 0.5)
    time.sleep(min(delay, MAX_BACKOFF_SECONDS))


def request(
    service: str,
    method: str,
    url: str,
    *,
    retries: int = 3,
    timeout: float = 30.0,
    retry_error_codes: frozenset[int] = frozenset(),
    **kwargs: Any,
) -> httpx.Response:
    """Send one HTTP request. Retries network errors, 429, 5xx, and any error code in retry_error_codes."""
    attempt = 0
    while True:
        try:
            response = httpx.request(method, url, timeout=timeout, **kwargs)
        except httpx.TransportError as exc:
            if attempt >= retries:
                raise ApiError(service, f"network error after {attempt + 1} attempts: {exc}", retryable=True) from exc
            logger.warning("%s network error (%s); retrying", service, exc)
            _sleep_before_retry(None, attempt)
            attempt += 1
            continue

        if response.status_code < 400:
            return response

        info = _error_info(response)
        transient = response.status_code in RETRY_STATUSES or (info.code is not None and info.code in retry_error_codes)
        if transient and attempt < retries:
            logger.warning("%s returned %s (%s); retrying", service, response.status_code, info.message)
            _sleep_before_retry(response, attempt)
            attempt += 1
            continue
        raise ApiError(
            service,
            info.message,
            status=response.status_code,
            code=info.code,
            subcode=info.subcode,
            retryable=transient,
        )
