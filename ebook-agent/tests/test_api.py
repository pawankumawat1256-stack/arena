"""HTTP API: authentication, agent queueing, webhooks, downloads, unsubscribe, and product rules."""

from __future__ import annotations

import re
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from config import settings
from core import pipeline
from db.database import SessionLocal
from db.models import BuyerAccess, MarketingPost, Sale, WebhookEvent
from main import app
from services import security
from tests import support

AUTH = {"X-API-Key": "test-api-key"}
WEBHOOK = f"/webhook/gumroad?token={settings.gumroad_webhook_token}"


@pytest.fixture()
def client():
    with TestClient(app) as test_client:
        yield test_client


def _ebook_payload() -> dict:
    return {"topic": "AI tools for solopreneurs", "audience": "Solo founders in India"}


def test_health_reports_ok(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_protected_routes_require_the_api_key(client):
    assert client.post("/ebooks", json=_ebook_payload()).status_code == 401
    assert client.post("/ebooks", json=_ebook_payload(), headers={"X-API-Key": "wrong"}).status_code == 401
    created = client.post("/ebooks", json=_ebook_payload(), headers=AUTH)
    assert created.status_code == 201
    assert created.json()["stage"] == "new"


def test_research_is_queued_and_returns_202(client, monkeypatch):
    queued: list[int] = []
    monkeypatch.setattr(pipeline, "execute_run", lambda run_id: queued.append(run_id))
    response = client.post("/research", json=_ebook_payload(), headers=AUTH)
    assert response.status_code == 202
    body = response.json()
    assert body["task"] == "research" and body["status"] == "queued"
    assert queued == [body["run_id"]]


def test_write_needs_an_outline_first(client):
    ebook_id = client.post("/ebooks", json=_ebook_payload(), headers=AUTH).json()["id"]
    response = client.post("/write", json={"ebook_id": ebook_id}, headers=AUTH)
    assert response.status_code == 409


def test_gumroad_webhook_rejects_missing_or_wrong_token(client):
    body = {"resource_name": "sale", "sale_id": "S-1"}
    assert client.post("/webhook/gumroad?token=wrong", data=body).status_code == 401
    assert client.post("/webhook/gumroad", data=body).status_code == 401


def test_gumroad_sale_ping_grants_access_and_is_idempotent(client, ebook, product, gumroad_sales, fake_gmail):
    gumroad_sales["S-1"] = dict(support.GUMROAD_SALE)
    body = {"resource_name": "sale", "sale_id": "S-1", "email": "buyer@example.com", "test": "false"}

    first = client.post(WEBHOOK, data=body)
    assert first.status_code == 200
    assert first.json()["duplicate"] is False
    assert len(fake_gmail) == 1
    with SessionLocal() as db:
        assert len(db.scalars(select(BuyerAccess)).all()) == 1
        assert db.scalars(select(Sale)).one().external_id == "S-1"
        assert db.scalars(select(WebhookEvent)).one().status == "processed"

    second = client.post(WEBHOOK, data=body)
    assert second.json()["duplicate"] is True
    assert len(fake_gmail) == 1


def test_gumroad_refund_ping_revokes_access(client, ebook, product, gumroad_sales, fake_gmail):
    gumroad_sales["S-2"] = dict(support.GUMROAD_SALE, id="S-2")
    client.post(WEBHOOK, data={"resource_name": "sale", "sale_id": "S-2", "email": "buyer@example.com"})
    gumroad_sales["S-2"] = dict(support.GUMROAD_SALE, id="S-2", refunded=True)
    response = client.post(WEBHOOK, data={"resource_name": "refund", "sale_id": "S-2", "email": "buyer@example.com"})
    assert response.status_code == 200
    with SessionLocal() as db:
        assert db.scalars(select(BuyerAccess)).one().revoked is True


def test_stripe_webhook_rejects_a_bad_signature(client, monkeypatch):
    monkeypatch.setattr(settings, "stripe_webhook_secret", "whsec_test_secret")
    response = client.post(
        "/webhook/stripe",
        content=b'{"id": "evt_1", "type": "checkout.session.completed"}',
        headers={"Stripe-Signature": "t=1700000000,v1=bad", "Content-Type": "application/json"},
    )
    assert response.status_code == 400


def test_unknown_download_token_is_404(client):
    assert client.get("/download/not-a-real-token").status_code == 404


def test_unsubscribe_requires_a_valid_signature(client):
    bad = client.get("/unsubscribe", params={"email": "reader@example.com", "sig": "0" * 64})
    assert bad.status_code == 400
    good = client.post(
        "/unsubscribe",
        params={"email": "reader@example.com", "sig": security.unsubscribe_signature("reader@example.com")},
    )
    assert good.status_code == 200
    assert "unsubscribed" in good.text.lower()


def test_kdp_product_above_the_price_ceiling_is_rejected(client, ebook):
    response = client.post(
        "/products",
        json={"ebook_id": ebook.id, "tier": "basic", "name": "Basic", "price_usd": "13.00", "platform": "kdp"},
        headers=AUTH,
    )
    assert response.status_code == 422


def test_product_file_must_live_inside_the_data_directory(client, ebook):
    response = client.post(
        "/products",
        json={
            "ebook_id": ebook.id,
            "tier": "pro",
            "name": "Toolkit",
            "price_usd": "19.00",
            "platform": "manual",
            "file_path": "/etc/hosts",
        },
        headers=AUTH,
    )
    assert response.status_code == 422


def test_manual_sale_is_counted_in_the_snapshot(client, ebook):
    today = datetime.now(timezone.utc).date().isoformat()
    created = client.post(
        "/sales/manual",
        json={"ebook_id": ebook.id, "platform": "kdp", "currency": "usd", "amount": "9.99", "fee": "3.49", "sold_on": today},
        headers=AUTH,
    )
    assert created.status_code == 201
    snapshot = client.get("/analytics/snapshot", params={"ebook_id": ebook.id, "days": 30}, headers=AUTH).json()
    assert snapshot["sales"]["paid_units"] == 1
    assert snapshot["sales"]["gross_usd"] == "9.99"
    assert snapshot["sales"]["net_usd"] == "6.50"


def test_post_must_have_an_image_before_it_is_scheduled(client, ebook, db):
    post = MarketingPost(ebook_id=ebook.id, caption="Caption", hashtags="", status="draft")
    db.add(post)
    db.commit()
    no_image = client.post(f"/posts/{post.id}/schedule", json={"scheduled_for": "2099-01-01T09:00:00+05:30"}, headers=AUTH)
    assert no_image.status_code == 409

    assert client.post(f"/posts/{post.id}/image", json={"image_url": "https://cdn.example.test/p.jpg"}, headers=AUTH).status_code == 200
    scheduled = client.post(f"/posts/{post.id}/schedule", json={"scheduled_for": "2099-01-01T09:00:00+05:30"}, headers=AUTH)
    assert scheduled.status_code == 200
    assert scheduled.json()["status"] == "scheduled"
    assert re.match(r"2099-01-01T03:30:00", scheduled.json()["scheduled_for"])


def test_product_and_lead_endpoints(client, ebook):
    created = client.post(
        "/products",
        json={"ebook_id": ebook.id, "tier": "basic", "name": "Basic", "price_usd": "9.99", "platform": "gumroad", "external_product_id": "gum-prod-9"},
        headers=AUTH,
    )
    assert created.status_code == 201
    listing = client.get("/products", params={"ebook_id": ebook.id}, headers=AUTH).json()
    assert [item["tier"] for item in listing] == ["basic"]

    duplicate_tier = client.post(
        "/products",
        json={"ebook_id": ebook.id, "tier": "basic", "name": "Basic again", "price_usd": "9.99", "platform": "manual"},
        headers=AUTH,
    )
    assert duplicate_tier.status_code == 409

    lead = client.post("/leads", json={"email": "new@example.com", "ebook_id": ebook.id, "source": "tally"}, headers=AUTH)
    assert lead.status_code == 201
    assert lead.json()["enrolled"] is True


def test_stripe_product_gets_a_storefront_link(client, ebook):
    created = client.post(
        "/products",
        json={"ebook_id": ebook.id, "tier": "bundle", "name": "Bundle", "price_usd": "24.00", "platform": "stripe"},
        headers=AUTH,
    )
    body = created.json()
    assert body["checkout_url"] == f"https://agent.example.test/buy/{body['id']}"


def test_scheduler_registers_every_job():
    import scheduler

    job_ids = {job.id for job in scheduler.build_scheduler().get_jobs()}
    assert job_ids == {
        "email-sequences",
        "publish-posts",
        "retry-webhooks",
        "gumroad-reconcile",
        "daily-analytics",
        "weekly-post-slots",
        "reap-stale-runs",
    }


def test_dashboard_script_renders_without_exceptions():
    from pathlib import Path

    from streamlit.testing.v1 import AppTest

    script = Path(__file__).resolve().parent.parent / "dashboard.py"
    app_test = AppTest.from_file(str(script), default_timeout=60).run()
    assert not app_test.exception
