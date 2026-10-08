"""Instagram publishing through the Graph API, Facebook Login path (Business or Creator account linked to a Page).

Behavior, checked against Meta's publishing documentation:
- Publishing is two steps: create a media container from a public JPEG URL, wait for FINISHED, then media_publish.
- Quota: 100 API-published posts per rolling 24 hours. Meta's content_publishing_limit reference says 50, so the
  live value from GET /{ig-user-id}/content_publishing_limit is used. The configured limit is only a fallback.
- Rate-limit error codes 4, 17, 32 and 613 are retried with backoff. Code 9 with subcode 2207042 means the
  post quota is used up for the day.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from config import settings
from integrations.net import ApiError, request

logger = logging.getLogger(__name__)

RATE_LIMIT_CODES = frozenset({4, 17, 32, 613})
QUOTA_CODE = 9
QUOTA_SUBCODE = 2207042
CONTAINER_READY = "FINISHED"
CONTAINER_FAILED = frozenset({"ERROR", "EXPIRED"})


class InstagramError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: int | None = None,
        subcode: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.subcode = subcode
        self.retryable = retryable

    @property
    def quota_exhausted(self) -> bool:
        return self.subcode == QUOTA_SUBCODE


@dataclass(frozen=True)
class PublishingQuota:
    used: int
    total: int

    @property
    def remaining(self) -> int:
        return max(0, self.total - self.used)


def is_configured() -> bool:
    return bool(settings.instagram_access_token and settings.instagram_user_id)


def _base_url() -> str:
    return f"{settings.instagram_graph_base_url.rstrip('/')}/{settings.graph_api_version}"


def _call(method: str, path: str, *, params: dict[str, Any] | None = None, data: dict[str, Any] | None = None) -> dict[str, Any]:
    if not is_configured():
        raise InstagramError("INSTAGRAM_ACCESS_TOKEN and INSTAGRAM_USER_ID must be set")
    try:
        response = request(
            "Instagram",
            method,
            f"{_base_url()}{path}",
            headers={"Authorization": f"Bearer {settings.instagram_access_token}"},
            params=params,
            data=data,
            timeout=60,
            retry_error_codes=RATE_LIMIT_CODES,
        )
    except ApiError as exc:
        raise InstagramError(str(exc), code=exc.code, subcode=exc.subcode, retryable=exc.retryable) from exc
    try:
        body = response.json()
    except ValueError as exc:
        raise InstagramError("Instagram returned a non-JSON response") from exc
    if not isinstance(body, dict):
        raise InstagramError("Unexpected Instagram response shape")
    return body


def get_quota() -> PublishingQuota:
    """Live quota from GET /{ig-user-id}/content_publishing_limit."""
    body = _call("GET", f"/{settings.instagram_user_id}/content_publishing_limit", params={"fields": "quota_usage,config"})
    items = body.get("data") or []
    item = items[0] if items and isinstance(items[0], dict) else {}
    config = item.get("config") if isinstance(item.get("config"), dict) else {}
    total = int(config.get("quota_total") or settings.instagram_daily_post_limit)
    used = int(item.get("quota_usage") or 0)
    return PublishingQuota(used=used, total=total)


def create_image_container(image_url: str, caption: str) -> str:
    body = _call("POST", f"/{settings.instagram_user_id}/media", data={"image_url": image_url, "caption": caption[:2200]})
    container_id = body.get("id")
    if not container_id:
        raise InstagramError("Instagram did not return a container id")
    return str(container_id)


def container_status(container_id: str) -> str:
    body = _call("GET", f"/{container_id}", params={"fields": "status_code"})
    return str(body.get("status_code") or "")


def wait_until_ready(container_id: str, *, attempts: int = 24, delay_seconds: float = 5.0) -> None:
    for _ in range(attempts):
        status = container_status(container_id)
        if status == CONTAINER_READY:
            return
        if status in CONTAINER_FAILED:
            raise InstagramError(f"Media container {container_id} ended with status {status}")
        time.sleep(delay_seconds)
    raise InstagramError(f"Media container {container_id} is still processing", retryable=True)


def publish_container(container_id: str) -> str:
    body = _call("POST", f"/{settings.instagram_user_id}/media_publish", data={"creation_id": container_id})
    media_id = body.get("id")
    if not media_id:
        raise InstagramError("Instagram did not return a media id")
    return str(media_id)


def publish_image(image_url: str, caption: str) -> str:
    """Publish one image post. Returns the Instagram media id."""
    if not image_url.startswith("https://"):
        raise InstagramError("Image URL must be a public HTTPS URL")
    container_id = create_image_container(image_url, caption)
    wait_until_ready(container_id)
    return publish_container(container_id)
