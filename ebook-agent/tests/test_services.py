"""Services: sales state, buyer access, email sequences, downloads, Instagram quota, metrics, and webhook helpers."""

from __future__ import annotations

import re
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from config import settings, utcnow
from db.models import (
    BuyerAccess,
    Click,
    EmailLog,
    MarketingPost,
    Sale,
    SequenceEnrollment,
)
from integrations import gmail_client, gumroad, instagram, notion_client, stripe_client
from integrations.instagram import InstagramError, PublishingQuota
from services import buyers, marketing, sales, security
from tests import support

# ---------- Security ----------


def test_unsubscribe_signature_round_trip_and_tamper_check():
    signature = security.unsubscribe_signature("Reader@Example.com")
    assert security.verify_signature(security.unsubscribe_message("reader@example.com"), signature)
    assert not security.verify_signature(security.unsubscribe_message("other@example.com"), signature)
    assert not security.verify_signature(security.unsubscribe_message("reader@example.com"), None)


def test_token_hashes_are_sha256_and_tokens_are_unique():
    token = security.new_token()
    assert len(security.hash_token(token)) == 64
    assert security.new_token() != token


def test_api_key_check_fails_closed_when_unset(monkeypatch):
    monkeypatch.setattr(settings, "api_key", "")
    with pytest.raises(HTTPException) as caught:
        security.require_api_key("anything")
    assert caught.value.status_code == 503


def test_api_key_check_rejects_wrong_key():
    with pytest.raises(HTTPException) as caught:
        security.require_api_key("wrong")
    assert caught.value.status_code == 401


# ---------- Gumroad parsing ----------


def test_parse_ping_accepts_form_and_json_bodies():
    payload = gumroad.parse_ping(b"resource_name=sale&sale_id=S-1&email=a%40b.com&test=true")
    event = gumroad.ping_event(payload)
    assert event.resource_name == "sale"
    assert event.sale_id == "S-1"
    assert event.is_test
    assert payload["email"] == "a@b.com"
    assert gumroad.ping_event(gumroad.parse_ping(b'{"resource_name": "refund", "sale_id": "S-2"}')).resource_name == "refund"


def test_parse_ping_rejects_bad_bodies():
    with pytest.raises(gumroad.PingError):
        gumroad.parse_ping(b"")
    with pytest.raises(gumroad.PingError):
        gumroad.parse_ping(b"{not json")
    with pytest.raises(gumroad.PingError):
        gumroad.ping_event({"resource_name": "bogus"})


def test_dedupe_key_combines_resource_and_sale_id():
    refund = gumroad.ping_event({"resource_name": "refund", "sale_id": "S-1"})
    sale = gumroad.ping_event({"resource_name": "sale", "sale_id": "S-1"})
    assert gumroad.dedupe_key(refund, b"") == "gumroad:refund:S-1"
    assert gumroad.dedupe_key(refund, b"") != gumroad.dedupe_key(sale, b"")


def test_stripe_signature_is_checked(monkeypatch):
    monkeypatch.setattr(settings, "stripe_webhook_secret", "whsec_test_secret")
    with pytest.raises(stripe_client.StripeError):
        stripe_client.verify_webhook(b'{"id": "evt_1", "type": "charge.refunded"}', "t=1700000000,v1=deadbeef")
    with pytest.raises(stripe_client.StripeError):
        stripe_client.verify_webhook(b"{}", None)


# ---------- Sales ----------


def test_webhook_store_is_idempotent(db):
    first, is_new = sales.store_webhook_event(db, source="gumroad", dedupe_key="gumroad:sale:S-9", event_type="sale", payload={"a": 1})
    again, is_new_again = sales.store_webhook_event(db, source="gumroad", dedupe_key="gumroad:sale:S-9", event_type="sale", payload={"a": 1})
    assert is_new is True and is_new_again is False
    assert first.id == again.id


def test_sale_grants_access_once_and_enrolls_onboarding(db, ebook, product, fake_gmail):
    sales.apply_gumroad_sale(db, sale=dict(support.GUMROAD_SALE))
    db.commit()
    sales.apply_gumroad_sale(db, sale=dict(support.GUMROAD_SALE))
    db.commit()

    assert len(db.scalars(select(BuyerAccess)).all()) == 1
    assert len(fake_gmail) == 1
    assert "/download/" in fake_gmail[0]["text"]
    assert fake_gmail[0]["subject"] == "Your download: Test Book"
    sale = db.scalars(select(Sale)).one()
    assert sale.buyer_email == "buyer@example.com"
    assert sale.net_cents == 849
    enrollment = db.scalars(select(SequenceEnrollment)).one()
    assert enrollment.sequence == "onboarding" and enrollment.status == "active"


def test_refund_revokes_access_and_a_late_sale_ping_cannot_restore_it(db, ebook, product, fake_gmail):
    sales.apply_gumroad_sale(db, sale=dict(support.GUMROAD_SALE))
    db.commit()
    sales.apply_gumroad_sale(db, sale=dict(support.GUMROAD_SALE, refunded=True), resource_name="refund")
    db.commit()
    assert db.scalars(select(BuyerAccess)).one().revoked is True

    sales.apply_gumroad_sale(db, sale=dict(support.GUMROAD_SALE))
    db.commit()
    assert db.scalars(select(BuyerAccess)).one().revoked is True
    assert len(fake_gmail) == 1


def test_refund_that_arrives_before_its_sale_grants_nothing(db, ebook, product, fake_gmail):
    sales.apply_gumroad_sale(db, sale=dict(support.GUMROAD_SALE, refunded=True), resource_name="refund")
    db.commit()
    record = db.scalars(select(Sale)).one()
    assert record.is_refunded is True
    assert db.scalars(select(BuyerAccess)).all() == []
    assert fake_gmail == []


def test_unmapped_gumroad_product_is_an_error(db, ebook, fake_gmail):
    with pytest.raises(LookupError):
        sales.apply_gumroad_sale(db, sale=dict(support.GUMROAD_SALE, product_id="unknown-product"))


def test_failed_webhook_is_recorded_and_retried_later(db, ebook, product, gumroad_sales, fake_gmail):
    event, _ = sales.store_webhook_event(
        db,
        source="gumroad",
        dedupe_key="gumroad:sale:S-1",
        event_type="sale",
        payload={"resource_name": "sale", "sale_id": "S-1"},
    )
    db.commit()
    sales.process_webhook_event(event.id)  # the sale does not exist yet, so the read-back fails
    db.refresh(event)
    assert event.status == "failed"
    assert event.attempts == 1
    assert "not found" in event.last_error

    gumroad_sales["S-1"] = dict(support.GUMROAD_SALE)
    assert sales.retry_failed_webhooks() == 1
    db.refresh(event)
    assert event.status == "processed"
    assert len(fake_gmail) == 1


def test_metrics_exclude_tests_and_refunds_from_revenue(db, ebook, product):
    now = utcnow()
    db.add_all(
        [
            Sale(ebook_id=ebook.id, product_id=product.id, platform="gumroad", external_id="A", amount_cents=999, fee_cents=150, net_cents=849, currency="usd", sold_at=now),
            Sale(ebook_id=ebook.id, product_id=product.id, platform="gumroad", external_id="B", amount_cents=999, fee_cents=150, net_cents=849, currency="usd", sold_at=now, is_refunded=True),
            Sale(ebook_id=ebook.id, product_id=product.id, platform="gumroad", external_id="C", amount_cents=999, fee_cents=150, net_cents=849, currency="usd", sold_at=now, is_test=True),
            Sale(ebook_id=ebook.id, platform="kdp", external_id="D", amount_cents=39900, fee_cents=10000, net_cents=29900, currency="inr", sold_at=now),
            Click(ebook_id=ebook.id, channel="instagram"),
        ]
    )
    db.commit()
    snapshot = sales.ebook_metrics(db, ebook_id=ebook.id, days=30)
    assert snapshot["sales"]["paid_units"] == 1
    assert snapshot["sales"]["refunded_units"] == 1
    assert snapshot["sales"]["gross_usd"] == "9.99"
    assert snapshot["sales"]["net_usd"] == "8.49"
    assert snapshot["sales"]["non_usd_units_not_included"] == 1
    assert snapshot["traffic"]["clicks"] == 1
    assert snapshot["traffic"]["sales_per_100_clicks"] is None


# ---------- Downloads ----------


def _token_from_access_email(fake_gmail) -> str:
    match = re.search(r"/download/([A-Za-z0-9_\-]+)", fake_gmail[-1]["text"])
    assert match, "access email does not contain a download link"
    return match.group(1)


def _book_file(name: str = "test-book.pdf"):
    path = settings.data_dir / "ebooks" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-1.4 test content")
    return path


def test_download_is_counted_and_limited(db, ebook, product, fake_gmail):
    product.file_path = str(_book_file())
    db.commit()
    sales.apply_gumroad_sale(db, sale=dict(support.GUMROAD_SALE))
    db.commit()
    token = _token_from_access_email(fake_gmail)

    grant, path = buyers.consume_download(db, token)
    assert path == _book_file().resolve()
    assert grant.download_count == 1

    grant.max_downloads = 1
    db.commit()
    with pytest.raises(buyers.AccessDenied) as caught:
        buyers.consume_download(db, token)
    assert caught.value.reason == "limit"


def test_expired_link_is_refused(db, ebook, product, fake_gmail):
    product.file_path = str(_book_file())
    db.commit()
    sales.apply_gumroad_sale(db, sale=dict(support.GUMROAD_SALE))
    db.commit()
    token = _token_from_access_email(fake_gmail)
    grant = db.scalars(select(BuyerAccess)).one()
    grant.expires_at = utcnow() - timedelta(minutes=1)
    db.commit()
    with pytest.raises(buyers.AccessDenied) as caught:
        buyers.consume_download(db, token)
    assert caught.value.reason == "expired"


def test_file_outside_data_dir_is_never_served(db, ebook, product, fake_gmail):
    product.file_path = "/etc/hosts"
    db.commit()
    sales.apply_gumroad_sale(db, sale=dict(support.GUMROAD_SALE))
    db.commit()
    token = _token_from_access_email(fake_gmail)
    with pytest.raises(buyers.AccessDenied) as caught:
        buyers.consume_download(db, token)
    assert caught.value.reason == "no_file"


def test_unknown_download_token_is_not_found(db):
    with pytest.raises(buyers.AccessDenied) as caught:
        buyers.consume_download(db, "no-such-token")
    assert caught.value.reason == "not_found"


# ---------- Email sequences ----------


def _enrolled_user(db, ebook, sequence="launch", email="reader@example.com"):
    user = buyers.get_or_create_user(db, email=email, source="lead-form")
    enrollment = buyers.enroll(db, user=user, ebook=ebook, sequence=sequence)
    db.commit()
    return user, enrollment


def test_sequence_sends_each_step_when_due_then_completes(db, ebook, fake_gmail):
    _user, enrollment = _enrolled_user(db, ebook, sequence="onboarding")
    start = enrollment.enrolled_at
    assert buyers.process_due_enrollments(db, now=start)["sent"] == 1
    assert buyers.process_due_enrollments(db, now=start + timedelta(hours=12))["sent"] == 0
    assert buyers.process_due_enrollments(db, now=start + timedelta(days=1, minutes=1))["sent"] == 1
    assert buyers.process_due_enrollments(db, now=start + timedelta(days=3, minutes=1))["sent"] == 1
    db.refresh(enrollment)
    assert enrollment.status == "completed"
    assert len(fake_gmail) == 3
    assert all("Unsubscribe:" in message["text"] for message in fake_gmail)
    assert all("List-Unsubscribe" in message["headers"] for message in fake_gmail)


def test_unsubscribe_stops_marketing_email(db, ebook, fake_gmail):
    user, enrollment = _enrolled_user(db, ebook)
    assert buyers.unsubscribe(db, email=user.email, signature="0" * 64) is False
    assert buyers.unsubscribe(db, email=user.email, signature=security.unsubscribe_signature(user.email)) is True
    assert buyers.process_due_enrollments(db, now=utcnow() + timedelta(days=1))["sent"] == 0
    db.refresh(user)
    db.refresh(enrollment)
    assert user.unsubscribed is True
    assert enrollment.status == "unsubscribed"
    assert fake_gmail == []


def test_marketing_email_is_blocked_without_a_postal_address(db, ebook, fake_gmail, monkeypatch):
    monkeypatch.setattr(settings, "business_postal_address", "")
    _user, enrollment = _enrolled_user(db, ebook)
    assert buyers.process_due_enrollments(db, now=enrollment.enrolled_at) == {"sent": 0, "failed": 0, "skipped": 1}
    assert fake_gmail == []


def test_gmail_failure_is_logged_and_retried_later(db, ebook, monkeypatch):
    def refuse(**_kwargs):
        raise gmail_client.GmailError("not authorized")

    monkeypatch.setattr(gmail_client, "create_draft_and_send", refuse)
    _user, enrollment = _enrolled_user(db, ebook)
    assert buyers.process_due_enrollments(db, now=enrollment.enrolled_at)["failed"] == 1
    db.refresh(enrollment)
    assert enrollment.next_step == 0
    assert enrollment.status == "active"
    assert enrollment.next_send_at > enrollment.enrolled_at
    log = db.scalars(select(EmailLog)).one()
    assert log.status == "failed"
    assert "not authorized" in log.error


def test_add_lead_enrolls_in_launch_sequence(db, ebook, fake_gmail):
    result = buyers.add_lead(db, email="new@example.com", ebook=ebook, full_name="New Reader", country="IN", source="tally")
    assert result["enrolled"] is True
    enrollment = db.scalars(select(SequenceEnrollment)).one()
    assert enrollment.sequence == "launch"


# ---------- Instagram publishing ----------


@pytest.fixture()
def instagram_on(monkeypatch):
    monkeypatch.setattr(instagram, "is_configured", lambda: True)


def _scheduled_post(db, ebook, *, minutes_ago=5, image="https://cdn.example.test/post.jpg"):
    post = MarketingPost(
        ebook_id=ebook.id,
        platform="instagram",
        caption="A caption",
        hashtags="#ai #solofounder",
        image_url=image,
        status="scheduled",
        scheduled_for=utcnow() - timedelta(minutes=minutes_ago),
    )
    db.add(post)
    db.commit()
    return post


def test_publishing_stops_at_the_live_quota(db, ebook, instagram_on, monkeypatch):
    monkeypatch.setattr(instagram, "get_quota", lambda: PublishingQuota(used=99, total=100))
    monkeypatch.setattr(instagram, "publish_image", lambda image_url, caption: "media-1")
    first = _scheduled_post(db, ebook, minutes_ago=10, image="https://cdn.example.test/a.jpg")
    second = _scheduled_post(db, ebook, minutes_ago=5, image="https://cdn.example.test/b.jpg")
    result = marketing.publish_due_posts(db)
    assert result["published"] == 1
    assert result["deferred"] == 1
    db.refresh(first)
    db.refresh(second)
    assert first.status == "published" and first.platform_post_id == "media-1"
    assert second.status == "scheduled"


def test_quota_exhausted_error_defers_the_post(db, ebook, instagram_on, monkeypatch):
    monkeypatch.setattr(instagram, "get_quota", lambda: PublishingQuota(used=0, total=100))

    def exhausted(image_url, caption):
        raise InstagramError("Application request limit reached", code=9, subcode=2207042)

    monkeypatch.setattr(instagram, "publish_image", exhausted)
    post = _scheduled_post(db, ebook)
    result = marketing.publish_due_posts(db)
    assert result["deferred"] == 1 and result["published"] == 0
    db.refresh(post)
    assert post.status == "scheduled"
    assert post.error and "Application request limit" in post.error


def test_falls_back_to_configured_limit_when_live_quota_fails(db, ebook, instagram_on, monkeypatch):
    def unavailable():
        raise InstagramError("temporarily unavailable", retryable=True)

    monkeypatch.setattr(instagram, "get_quota", unavailable)
    monkeypatch.setattr(instagram, "publish_image", lambda image_url, caption: "media-9")
    _scheduled_post(db, ebook)
    result = marketing.publish_due_posts(db)
    assert result["quota"]["source"] == "configured"
    assert result["published"] == 1


def test_transient_publish_failure_is_rescheduled(db, ebook, instagram_on, monkeypatch):
    monkeypatch.setattr(instagram, "get_quota", lambda: PublishingQuota(used=0, total=100))

    def throttled(image_url, caption):
        raise InstagramError("rate limited", code=4, retryable=True)

    monkeypatch.setattr(instagram, "publish_image", throttled)
    post = _scheduled_post(db, ebook)
    marketing.publish_due_posts(db)
    db.refresh(post)
    assert post.status == "scheduled"
    assert post.scheduled_for > utcnow()


def test_weekly_slots_schedule_only_drafts_with_images(db, ebook):
    for index in range(4):
        db.add(MarketingPost(ebook_id=ebook.id, caption=f"Caption {index}", hashtags="#ai", image_url="https://cdn.example.test/x.jpg", status="draft"))
    db.add(MarketingPost(ebook_id=ebook.id, caption="No image yet", hashtags="", image_url=None, status="draft"))
    db.commit()
    now = datetime(2026, 10, 8, 3, 30)  # Thursday 09:00 in Asia/Kolkata
    assert marketing.schedule_weekly_slots(db, now=now) == settings.marketing_posts_per_week
    scheduled = db.scalars(select(MarketingPost).where(MarketingPost.status == "scheduled").order_by(MarketingPost.scheduled_for)).all()
    assert len(scheduled) == settings.marketing_posts_per_week
    assert all(post.scheduled_for > now for post in scheduled)
    first = scheduled[0].scheduled_for
    assert first.weekday() == 0 and (first.hour, first.minute) == (3, 30)  # Monday 09:00 IST is 03:30 UTC


# ---------- Notion conversion ----------


def test_markdown_becomes_notion_blocks():
    markdown = "# Title\n\nIntro line one\ncontinues here.\n\n## Section\n\n- first point\n- second point\n"
    blocks = notion_client.markdown_to_blocks(markdown)
    assert [block["type"] for block in blocks] == ["heading_1", "paragraph", "heading_2", "bulleted_list_item", "bulleted_list_item"]
    assert blocks[1]["paragraph"]["rich_text"][0]["text"]["content"] == "Intro line one continues here."


def test_long_paragraph_is_split_to_notion_text_limits():
    blocks = notion_client.markdown_to_blocks("x" * 4500)
    assert len(blocks[0]["paragraph"]["rich_text"]) == 3


def test_stale_runs_are_marked_failed_after_a_restart(db, ebook):
    import json as _json

    from db.models import AgentRun
    from scheduler import job_reap_stale_runs

    old = AgentRun(task="write", ebook_id=ebook.id, status="running", input_json=_json.dumps({}), created_at=utcnow() - timedelta(hours=5))
    fresh = AgentRun(task="write", ebook_id=ebook.id, status="running", input_json=_json.dumps({}))
    db.add_all([old, fresh])
    db.commit()
    assert job_reap_stale_runs() == 1
    db.refresh(old)
    db.refresh(fresh)
    assert old.status == "failed" and "Interrupted" in old.error
    assert fresh.status == "running"
