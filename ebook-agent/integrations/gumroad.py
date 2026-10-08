"""Gumroad: read sales through the API, manage resource subscriptions, and parse Ping deliveries.

Verified against gumroad.com/ping and gumroad.com/api:
- Ping bodies are form-encoded and unsigned. A Ping is therefore a trigger: the sale is read back through the API.
- Delivery is at least once and unordered. Deduplicate on resource_name + sale_id (a refund shares its sale_id).
- Gumroad retries only on 499, 500, 502, 503 and 504, after 1, 3, 10 and 60 minutes.
- The endpoint has 5 seconds to answer, so the webhook stores the event and answers at once.

CLI (run once after deploy, and again if the public URL or token changes):
    python -m integrations.gumroad register
    python -m integrations.gumroad list
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any, Iterator
from urllib.parse import parse_qs, quote

from config import settings
from integrations.net import ApiError, request

logger = logging.getLogger(__name__)

API_BASE = "https://api.gumroad.com/v2"
RESOURCE_NAMES = (
    "sale",
    "refund",
    "cancellation",
    "subscription_ended",
    "subscription_restarted",
    "subscription_updated",
    "dispute",
    "dispute_won",
)
TRUE_VALUES = frozenset({"true", "1", "yes"})


class GumroadError(RuntimeError):
    """A Gumroad API call failed or is not configured."""


class PingError(ValueError):
    """A Ping or JSON webhook body is malformed or unsupported."""


@dataclass(frozen=True)
class PingEvent:
    resource_name: str
    sale_id: str | None
    subscription_id: str | None
    is_test: bool
    sale_timestamp: str | None


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _auth_headers() -> dict[str, str]:
    if not settings.gumroad_access_token:
        raise GumroadError("GUMROAD_ACCESS_TOKEN is not set")
    return {"Authorization": f"Bearer {settings.gumroad_access_token}"}


def _call(method: str, path: str, *, params: dict[str, Any] | None = None, data: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        response = request(
            "Gumroad",
            method,
            f"{API_BASE}{path}",
            headers=_auth_headers(),
            params=params,
            data=data,
            timeout=30,
        )
    except ApiError as exc:
        raise GumroadError(str(exc)) from exc
    try:
        body = response.json()
    except ValueError as exc:
        raise GumroadError("Gumroad returned a non-JSON response") from exc
    if not isinstance(body, dict):
        raise GumroadError("Unexpected Gumroad response shape")
    if body.get("success") is False:
        raise GumroadError(str(body.get("message") or "Gumroad reported failure"))
    return body


# ---------- Sales ----------


def get_sale(sale_id: str) -> dict[str, Any]:
    """Read one sale back through GET /v2/sales/:id (view_sales scope). This is the source of truth."""
    body = _call("GET", f"/sales/{quote(sale_id, safe='')}")
    sale = body.get("sale") if isinstance(body.get("sale"), dict) else body
    if not sale.get("id"):
        raise GumroadError(f"Sale {sale_id} was not found in this Gumroad account")
    return sale


def list_sales(
    *,
    after: str | None = None,
    before: str | None = None,
    product_id: str | None = None,
    email: str | None = None,
    page_key: str | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """One page of GET /v2/sales. Dates are YYYY-MM-DD. Returns (sales, next_page_key)."""
    params = {"after": after, "before": before, "product_id": product_id, "email": email, "page_key": page_key}
    body = _call("GET", "/sales", params={key: value for key, value in params.items() if value})
    sales = [item for item in (body.get("sales") or []) if isinstance(item, dict)]
    return sales, _clean(body.get("next_page_key"))


def iter_sales(*, after: str | None = None, before: str | None = None) -> Iterator[dict[str, Any]]:
    page_key: str | None = None
    while True:
        sales, page_key = list_sales(after=after, before=before, page_key=page_key)
        yield from sales
        if not page_key:
            return


# ---------- Resource subscriptions (webhook registration) ----------


def subscribe(resource_name: str, post_url: str) -> dict[str, Any]:
    if resource_name not in RESOURCE_NAMES:
        raise GumroadError(f"Unsupported resource_name: {resource_name}")
    return _call("PUT", "/resource_subscriptions", data={"resource_name": resource_name, "post_url": post_url})


def list_subscriptions(resource_name: str) -> list[dict[str, Any]]:
    body = _call("GET", "/resource_subscriptions", params={"resource_name": resource_name})
    return [item for item in (body.get("resource_subscriptions") or []) if isinstance(item, dict)]


def unsubscribe(subscription_id: str) -> dict[str, Any]:
    return _call("DELETE", f"/resource_subscriptions/{quote(subscription_id, safe='')}")


def webhook_url() -> str:
    if not settings.gumroad_webhook_token:
        raise GumroadError("GUMROAD_WEBHOOK_TOKEN is not set")
    base = settings.public_base_url.rstrip("/")
    return f"{base}/webhook/gumroad?token={quote(settings.gumroad_webhook_token, safe='')}"


def register_webhooks(post_url: str) -> dict[str, str]:
    """Subscribe post_url to every resource this app needs. Safe to run repeatedly."""
    results: dict[str, str] = {}
    for name in RESOURCE_NAMES:
        if any(item.get("post_url") == post_url for item in list_subscriptions(name)):
            results[name] = "already registered"
            continue
        subscribe(name, post_url)
        results[name] = "registered"
    return results


# ---------- Ping parsing ----------


def parse_ping(body: bytes) -> dict[str, Any]:
    """Parse a Ping body. Gumroad sends form-encoded data; a JSON object is also accepted."""
    stripped = body.strip()
    if not stripped:
        raise PingError("Empty request body")
    if stripped.startswith(b"{"):
        try:
            data = json.loads(stripped)
        except ValueError as exc:
            raise PingError("Malformed JSON body") from exc
        if not isinstance(data, dict):
            raise PingError("JSON body must be an object")
        return data
    try:
        text = stripped.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PingError("Body is not valid UTF-8") from exc
    fields = parse_qs(text, keep_blank_values=True)
    if not fields:
        raise PingError("No fields found in body")
    return {key: values[-1] for key, values in fields.items()}


def ping_event(payload: dict[str, Any]) -> PingEvent:
    resource = (_clean(payload.get("resource_name")) or "").lower()
    if resource not in RESOURCE_NAMES:
        raise PingError(f"Unsupported or missing resource_name: {resource or '(missing)'}")
    return PingEvent(
        resource_name=resource,
        sale_id=_clean(payload.get("sale_id")),
        subscription_id=_clean(payload.get("subscription_id")),
        is_test=(_clean(payload.get("test")) or "").lower() in TRUE_VALUES,
        sale_timestamp=_clean(payload.get("sale_timestamp")),
    )


def dedupe_key(event: PingEvent, body: bytes) -> str:
    if event.sale_id:
        return f"gumroad:{event.resource_name}:{event.sale_id}"
    digest = hashlib.sha256(body).hexdigest()[:24]
    return f"gumroad:{event.resource_name}:nosale:{digest}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage Gumroad resource subscriptions.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("register", help="Subscribe this app's webhook URL to every needed resource")
    commands.add_parser("list", help="Show current subscriptions for each resource")
    args = parser.parse_args()
    logging.basicConfig(level="INFO", format="%(levelname)s %(message)s")
    try:
        if args.command == "register":
            for name, result in register_webhooks(webhook_url()).items():
                print(f"{name}: {result}")
            return
        for name in RESOURCE_NAMES:
            for item in list_subscriptions(name):
                print(f"{name}: id={item.get('id')} post_url={item.get('post_url')}")
    except GumroadError as exc:
        raise SystemExit(f"Gumroad: {exc}") from exc


if __name__ == "__main__":
    main()
