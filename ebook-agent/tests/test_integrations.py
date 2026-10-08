"""Integration clients with the network replaced by fakes: retries, Stripe, Instagram, Notion, and link re-issue."""

from __future__ import annotations

import re

import httpx
import pytest
from sqlalchemy import select

from config import settings
from db.models import BuyerAccess, Product, Sale
from integrations import gumroad, instagram, net, notion_client, stripe_client
from integrations.instagram import InstagramError, PublishingQuota
from services import buyers, sales
from tests import support


def _response(status: int, body: dict, method: str = "GET", url: str = "https://example.test/x") -> httpx.Response:
    return httpx.Response(status, json=body, request=httpx.Request(method, url))


# ---------- Shared HTTP helper ----------


def test_transient_status_codes_are_retried(monkeypatch):
    monkeypatch.setattr(net.time, "sleep", lambda _seconds: None)
    calls: list[str] = []

    def flaky(method, url, **_kwargs):
        calls.append(url)
        return _response(503, {"message": "busy"}, method, url) if len(calls) < 3 else _response(200, {"ok": True}, method, url)

    monkeypatch.setattr(net.httpx, "request", flaky)
    assert net.request("Test", "GET", "https://example.test/x", retries=3).json() == {"ok": True}
    assert len(calls) == 3


def test_listed_error_codes_are_retried_even_on_400(monkeypatch):
    monkeypatch.setattr(net.time, "sleep", lambda _seconds: None)
    calls = {"count": 0}

    def rate_limited_once(method, url, **_kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            return _response(400, {"error": {"message": "limit reached", "code": 4}}, method, url)
        return _response(200, {}, method, url)

    monkeypatch.setattr(net.httpx, "request", rate_limited_once)
    net.request("Instagram", "GET", "https://example.test/x", retry_error_codes=frozenset({4}))
    assert calls["count"] == 2


def test_other_client_errors_are_not_retried(monkeypatch):
    monkeypatch.setattr(net.httpx, "request", lambda method, url, **_kwargs: _response(400, {"error": {"message": "bad", "code": 100}}, method, url))
    with pytest.raises(net.ApiError) as caught:
        net.request("Instagram", "GET", "https://example.test/x")
    assert caught.value.code == 100
    assert caught.value.retryable is False


# ---------- Instagram ----------


def test_instagram_publish_creates_waits_and_publishes(monkeypatch):
    monkeypatch.setattr(settings, "instagram_user_id", "17841")
    monkeypatch.setattr(instagram.time, "sleep", lambda _seconds: None)
    seen: list[tuple[str, str]] = []

    def fake_call(method, path, *, params=None, data=None):
        seen.append((method, path))
        if method == "POST" and path == "/17841/media":
            return {"id": "container-1"}
        if method == "GET" and path == "/container-1":
            return {"status_code": "FINISHED"}
        if method == "POST" and path == "/17841/media_publish":
            return {"id": "media-77"}
        raise AssertionError(f"unexpected call {method} {path}")

    monkeypatch.setattr(instagram, "_call", fake_call)
    assert instagram.publish_image("https://cdn.example.test/p.jpg", "Caption") == "media-77"
    assert [path for _method, path in seen] == ["/17841/media", "/container-1", "/17841/media_publish"]


def test_instagram_quota_is_read_from_the_live_endpoint(monkeypatch):
    body = {"data": [{"quota_usage": 7, "config": {"quota_total": 100, "quota_duration": 86400}}]}
    monkeypatch.setattr(instagram, "_call", lambda *_args, **_kwargs: body)
    quota = instagram.get_quota()
    assert quota == PublishingQuota(used=7, total=100)
    assert quota.remaining == 93


def test_instagram_rejects_non_https_images():
    with pytest.raises(InstagramError):
        instagram.publish_image("http://cdn.example.test/p.jpg", "Caption")


# ---------- Gumroad registration ----------


def test_register_webhooks_skips_what_is_already_registered(monkeypatch):
    post_url = "https://agent.example.test/webhook/gumroad?token=x"
    existing = {"sale": [{"id": "sub-1", "post_url": post_url}]}
    subscribed: list[str] = []
    monkeypatch.setattr(gumroad, "list_subscriptions", lambda name: existing.get(name, []))
    monkeypatch.setattr(gumroad, "subscribe", lambda name, _url: subscribed.append(name) or {"success": True})
    results = gumroad.register_webhooks(post_url)
    assert results["sale"] == "already registered"
    assert results["refund"] == "registered"
    assert "sale" not in subscribed
    assert len(subscribed) == len(gumroad.RESOURCE_NAMES) - 1


# ---------- Stripe ----------


def _stripe_product(db, ebook) -> Product:
    product = Product(ebook_id=ebook.id, tier="bundle", name="Bundle", price_cents=2400, platform="stripe", active=True)
    db.add(product)
    db.commit()
    return product


def _checkout_session(product, *, livemode: bool = True) -> dict:
    return {
        "id": "cs_1",
        "payment_intent": "pi_1",
        "payment_status": "paid",
        "livemode": livemode,
        "amount_total": 2400,
        "currency": "usd",
        "created": 1790000000,
        "customer_details": {"email": "Buyer@Example.com", "address": {"country": "IN"}},
        "metadata": {"product_id": str(product.id), "ebook_id": str(product.ebook_id)},
    }


def test_stripe_checkout_grants_access_and_records_fees(db, ebook, fake_gmail, monkeypatch):
    product = _stripe_product(db, ebook)
    monkeypatch.setattr(stripe_client, "fetch_fee_and_net", lambda _intent: (150, 2250))
    sales.apply_stripe_checkout(db, session=_checkout_session(product))
    db.commit()
    record = db.scalars(select(Sale)).one()
    assert record.external_id == "pi_1"
    assert (record.fee_cents, record.net_cents, record.country) == (150, 2250, "IN")
    assert len(db.scalars(select(BuyerAccess)).all()) == 1
    assert len(fake_gmail) == 1


def test_stripe_full_refund_revokes_access_and_partial_does_not(db, ebook, fake_gmail, monkeypatch):
    product = _stripe_product(db, ebook)
    monkeypatch.setattr(stripe_client, "fetch_fee_and_net", lambda _intent: None)
    sales.apply_stripe_checkout(db, session=_checkout_session(product))
    db.commit()
    assert "Partial" in sales.apply_stripe_refund(db, charge={"refunded": False, "payment_intent": "pi_1"})
    db.commit()
    assert db.scalars(select(BuyerAccess)).one().revoked is False
    sales.apply_stripe_refund(db, charge={"refunded": True, "payment_intent": "pi_1"})
    db.commit()
    assert db.scalars(select(BuyerAccess)).one().revoked is True


def test_stripe_refund_before_checkout_is_retried_later(db):
    with pytest.raises(LookupError):
        sales.apply_stripe_refund(db, charge={"refunded": True, "payment_intent": "pi_missing"})


def test_stripe_test_mode_checkout_grants_nothing(db, ebook, fake_gmail, monkeypatch):
    product = _stripe_product(db, ebook)
    monkeypatch.setattr(stripe_client, "fetch_fee_and_net", lambda _intent: None)
    sales.apply_stripe_checkout(db, session=_checkout_session(product, livemode=False))
    db.commit()
    assert db.scalars(select(Sale)).one().is_test is True
    assert db.scalars(select(BuyerAccess)).all() == []
    assert fake_gmail == []


# ---------- Notion ----------


def test_notion_page_children_are_sent_in_batches_of_100(monkeypatch):
    monkeypatch.setattr(settings, "notion_api_key", "secret_test")
    monkeypatch.setattr(settings, "notion_parent_page_id", "parent-123")
    calls: list[tuple[str, int]] = []

    def fake_request(_service, method, _url, **kwargs):
        calls.append((method, len(kwargs["json"]["children"])))
        body = {"id": "page-9", "url": "https://notion.so/page-9"} if method == "POST" else {}
        return _response(200, body, method)

    monkeypatch.setattr(notion_client, "request", fake_request)
    markdown = "\n\n".join(f"Paragraph {index}." for index in range(250))
    page = notion_client.create_notion_page("Test Book", markdown)
    assert page["url"] == "https://notion.so/page-9"
    assert calls == [("POST", 100), ("PATCH", 100), ("PATCH", 50)]


# ---------- Access link re-issue ----------


def test_resend_replaces_the_old_link(db, ebook, product, fake_gmail):
    sales.apply_gumroad_sale(db, sale=dict(support.GUMROAD_SALE))
    db.commit()
    old_token = re.search(r"/download/([A-Za-z0-9_\-]+)", fake_gmail[-1]["text"]).group(1)
    sale_id = db.scalars(select(Sale)).one().id
    _grant, sent = buyers.resend_access(db, sale_id=sale_id)
    assert sent is True
    new_token = re.search(r"/download/([A-Za-z0-9_\-]+)", fake_gmail[-1]["text"]).group(1)
    assert new_token != old_token
    with pytest.raises(buyers.AccessDenied) as caught:
        buyers.consume_download(db, old_token)
    assert caught.value.reason == "not_found"
