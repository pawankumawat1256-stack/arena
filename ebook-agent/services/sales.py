"""Sales: webhook intake and processing, the Gumroad reconciliation job, manual sales, and the analytics snapshot.

Gumroad is handled state-first. Each ping is only a trigger. The sale is read back through GET /v2/sales/:id, and
access is decided from the sale's current state (refunded, disputed, ended, test). That makes duplicate, late, and
out-of-order pings safe: the outcome depends on the sale, not on the order the pings arrive in.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from config import utcnow
from db.database import SessionLocal
from db.models import Click, Ebook, EmailLog, MarketingPost, Product, Sale, WebhookEvent
from integrations import gumroad, stripe_client
from services import buyers

logger = logging.getLogger(__name__)

MIN_CLICKS_FOR_RATE = 30
RECONCILE_DAYS = 3


# ---------- Helpers ----------


def _clean_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _cents(value: Any) -> int:
    if value in (None, ""):
        return 0
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _parse_timestamp(value: Any) -> datetime | None:
    text = _clean_str(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _usd(cents: int) -> str:
    return str((Decimal(cents) / Decimal(100)).quantize(Decimal("0.01")))


# ---------- Webhook intake ----------


def store_webhook_event(
    db: Session,
    *,
    source: str,
    dedupe_key: str,
    event_type: str,
    payload: dict[str, Any],
) -> tuple[WebhookEvent, bool]:
    """Store a webhook before processing it. Returns (event, is_new). Duplicates return the stored event."""
    existing = db.scalar(select(WebhookEvent).where(WebhookEvent.dedupe_key == dedupe_key))
    if existing is not None:
        return existing, False
    event = WebhookEvent(
        source=source,
        dedupe_key=dedupe_key,
        event_type=event_type[:64],
        payload_json=json.dumps(payload, ensure_ascii=False, default=str),
        status="received",
    )
    db.add(event)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(select(WebhookEvent).where(WebhookEvent.dedupe_key == dedupe_key))
        if existing is None:
            raise
        return existing, False
    return event, True


def process_webhook_event(event_id: int) -> None:
    """Background task: apply one stored webhook. Never raises. Failures are recorded for the retry job."""
    with SessionLocal() as db:
        event = db.get(WebhookEvent, event_id)
        if event is None or event.status in ("processed", "ignored"):
            return
        event.attempts += 1
        db.commit()
        try:
            if event.source == "gumroad":
                status, message = _process_gumroad_event(db, event)
            elif event.source == "stripe":
                status, message = _process_stripe_event(db, event)
            else:
                status, message = "ignored", f"Unknown source {event.source}"
        except Exception as exc:
            logger.exception("Webhook event %s failed", event_id)
            db.rollback()
            status, message = "failed", f"{type(exc).__name__}: {exc}"[:2000]
        event = db.get(WebhookEvent, event_id)
        event.status = status
        event.last_error = message if status != "processed" else None
        event.processed_at = utcnow()
        db.commit()
        logger.info("Webhook event %s (%s): %s - %s", event_id, event.source, status, message)


def retry_failed_webhooks(*, max_attempts: int = 8, limit: int = 50) -> int:
    with SessionLocal() as db:
        ids = db.scalars(
            select(WebhookEvent.id)
            .where(WebhookEvent.status == "failed", WebhookEvent.attempts < max_attempts)
            .order_by(WebhookEvent.received_at)
            .limit(limit)
        ).all()
    for event_id in ids:
        process_webhook_event(event_id)
    return len(ids)


# ---------- Gumroad ----------


def _process_gumroad_event(db: Session, event: WebhookEvent) -> tuple[str, str]:
    payload = json.loads(event.payload_json)
    ping = gumroad.ping_event(payload)
    if not ping.sale_id:
        return "ignored", "No sale_id in the ping, so there is no sale to read back"
    sale = gumroad.get_sale(ping.sale_id)
    message = apply_gumroad_sale(db, sale=sale, resource_name=ping.resource_name, is_test=ping.is_test)
    db.commit()
    return "processed", message


def apply_gumroad_sale(db: Session, *, sale: dict[str, Any], resource_name: str = "sale", is_test: bool = False) -> str:
    """Apply one sale object read back from the Gumroad API. Idempotent: the same input gives the same state."""
    sale_id = str(sale["id"])
    product_id = _clean_str(sale.get("product_id"))
    permalink = _clean_str(sale.get("product_permalink"))
    product = None
    if product_id:
        product = db.scalar(select(Product).where(Product.platform == "gumroad", Product.external_product_id == product_id))
    if product is None and permalink:
        product = db.scalar(select(Product).where(Product.platform == "gumroad", Product.permalink == permalink))
    if product is None:
        raise LookupError(f"No Gumroad product is mapped to product_id {product_id or '(none)'}. Create it with POST /products.")
    ebook = db.get(Ebook, product.ebook_id)

    email = _clean_str(sale.get("email") or sale.get("purchase_email"))
    buyer_email = email.lower() if email else None
    user = buyers.get_or_create_user(db, email=buyer_email, source="gumroad") if buyer_email else None

    refunded = bool(sale.get("refunded") or sale.get("chargedback"))
    disputed_open = bool(sale.get("disputed")) and not bool(sale.get("dispute_won"))
    ended = bool(sale.get("ended"))
    is_subscription = bool(sale.get("subscription_id") or sale.get("is_recurring_billing"))
    amount = _cents(sale.get("price"))
    fee = _cents(sale.get("gumroad_fee"))

    record = _upsert_sale(
        db,
        platform="gumroad",
        external_id=sale_id,
        ebook=ebook,
        product=product,
        user=user,
        buyer_email=buyer_email,
        amount_cents=amount,
        fee_cents=fee,
        net_cents=amount - fee,
        currency=(_clean_str(sale.get("currency")) or "usd").lower(),
        country=None,
        is_refunded=refunded,
        is_test=is_test,
        sold_at=_parse_timestamp(sale.get("created_at")) or utcnow(),
        raw=sale,
    )

    access_allowed = not (record.is_refunded or disputed_open or ended) and not record.is_test
    if access_allowed:
        buyers.ensure_access(db, sale=record, ebook=ebook, product=product, buyer_email=buyer_email)
        if user is not None:
            buyers.enroll(db, user=user, ebook=ebook, sequence=buyers.SEQUENCE_ONBOARDING)
        state = "access active"
    else:
        if is_subscription and user is not None:
            buyers.revoke_for_user_product(db, user.id, product.id)
        else:
            buyers.revoke_for_sale(db, record.id)
        state = "access revoked"
    return f"{resource_name} for Gumroad sale {sale_id}: {state}"


def reconcile_gumroad(*, days: int = RECONCILE_DAYS) -> dict[str, int]:
    """Read recent sales from GET /v2/sales and apply them. This catches any ping that was lost."""
    after = (utcnow() - timedelta(days=days)).date().isoformat()
    applied = failed = 0
    with SessionLocal() as db:
        for sale in gumroad.iter_sales(after=after):
            try:
                apply_gumroad_sale(db, sale=sale, resource_name="reconcile")
                db.commit()
                applied += 1
            except Exception as exc:
                db.rollback()
                failed += 1
                logger.warning("Reconcile could not apply sale %s: %s", sale.get("id"), exc)
    return {"applied": applied, "failed": failed}


# ---------- Stripe ----------


def _process_stripe_event(db: Session, event: WebhookEvent) -> tuple[str, str]:
    payload = json.loads(event.payload_json)
    event_type = str(payload.get("type", ""))
    obj = (payload.get("data") or {}).get("object") or {}
    if event_type == "checkout.session.completed":
        if obj.get("payment_status") != "paid":
            return "ignored", f"payment_status is {obj.get('payment_status')}"
        return "processed", apply_stripe_checkout(db, session=obj)
    if event_type == "charge.refunded":
        return "processed", apply_stripe_refund(db, charge=obj)
    return "ignored", f"Event type {event_type} is not used"


def apply_stripe_checkout(db: Session, *, session: dict[str, Any]) -> str:
    metadata = session.get("metadata") or {}
    product_ref = _clean_str(metadata.get("product_id"))
    product = db.get(Product, int(product_ref)) if product_ref and product_ref.isdigit() else None
    if product is None or product.platform != "stripe":
        raise LookupError(f"Stripe checkout has no matching stripe product (metadata product_id={product_ref})")
    ebook = db.get(Ebook, product.ebook_id)
    intent_id = _clean_str(session.get("payment_intent"))
    external_id = intent_id or str(session["id"])
    details = session.get("customer_details") or {}
    buyer_email = (_clean_str(details.get("email")) or _clean_str(session.get("customer_email")) or "").lower() or None
    user = buyers.get_or_create_user(db, email=buyer_email, source="stripe") if buyer_email else None
    amount = int(session.get("amount_total") or 0)
    fee, net = 0, amount
    if intent_id:
        fees = stripe_client.fetch_fee_and_net(intent_id)
        if fees is not None:
            fee, net = fees
    record = _upsert_sale(
        db,
        platform="stripe",
        external_id=external_id,
        ebook=ebook,
        product=product,
        user=user,
        buyer_email=buyer_email,
        amount_cents=amount,
        fee_cents=fee,
        net_cents=net,
        currency=(_clean_str(session.get("currency")) or "usd").lower(),
        country=_clean_str((details.get("address") or {}).get("country")),
        is_refunded=False,
        is_test=not bool(session.get("livemode", True)),
        sold_at=datetime.fromtimestamp(int(session.get("created") or time.time()), tz=timezone.utc).replace(tzinfo=None),
        raw=session,
    )
    if not record.is_test and not record.is_refunded:
        buyers.ensure_access(db, sale=record, ebook=ebook, product=product, buyer_email=buyer_email)
        if user is not None:
            buyers.enroll(db, user=user, ebook=ebook, sequence=buyers.SEQUENCE_ONBOARDING)
    return f"Stripe checkout {external_id} recorded"


def apply_stripe_refund(db: Session, *, charge: dict[str, Any]) -> str:
    if not charge.get("refunded"):
        return "Partial refund recorded; access unchanged"
    intent_id = _clean_str(charge.get("payment_intent"))
    if not intent_id:
        return "Refund has no payment_intent; nothing to update"
    record = db.scalar(select(Sale).where(Sale.platform == "stripe", Sale.external_id == intent_id))
    if record is None:
        raise LookupError(f"No Stripe sale exists yet for payment_intent {intent_id}; the retry job will try again")
    record.is_refunded = True
    buyers.revoke_for_sale(db, record.id)
    return f"Stripe refund for {intent_id}: access revoked"


# ---------- Sales records ----------


def _upsert_sale(
    db: Session,
    *,
    platform: str,
    external_id: str,
    ebook: Ebook,
    product: Product,
    user,
    buyer_email: str | None,
    amount_cents: int,
    fee_cents: int,
    net_cents: int,
    currency: str,
    country: str | None,
    is_refunded: bool,
    is_test: bool,
    sold_at: datetime,
    raw: dict[str, Any] | None,
) -> Sale:
    record = db.scalar(select(Sale).where(Sale.platform == platform, Sale.external_id == external_id))
    if record is None:
        record = Sale(platform=platform, external_id=external_id, ebook_id=ebook.id, product_id=product.id)
        db.add(record)
    if user is not None:
        record.user_id = user.id
    record.buyer_email = buyer_email or record.buyer_email
    record.amount_cents = amount_cents
    record.fee_cents = fee_cents
    record.net_cents = net_cents
    record.currency = currency[:8]
    record.country = country or record.country
    record.is_refunded = record.is_refunded or is_refunded
    record.is_test = record.is_test or is_test
    record.sold_at = sold_at
    if raw is not None:
        record.raw_json = json.dumps(raw, ensure_ascii=False, default=str)
    db.flush()
    return record


def record_manual_sale(
    db: Session,
    *,
    ebook: Ebook,
    product: Product | None,
    platform: str,
    amount_cents: int,
    fee_cents: int,
    currency: str,
    sold_at: datetime,
    country: str | None,
    external_id: str | None,
) -> Sale:
    """Manual entry, for example a KDP royalty report line. amount is the list price and fee is list minus royalty."""
    record = Sale(
        ebook_id=ebook.id,
        product_id=product.id if product else None,
        platform=platform,
        external_id=external_id or f"manual-{uuid.uuid4().hex}",
        amount_cents=amount_cents,
        fee_cents=fee_cents,
        net_cents=amount_cents - fee_cents,
        currency=currency.lower()[:8],
        country=country,
        sold_at=sold_at,
        raw_json=json.dumps({"source": "manual entry"}),
    )
    db.add(record)
    db.commit()
    return record


# ---------- Analytics snapshot ----------


def ebook_metrics(db: Session, *, ebook_id: int | None, days: int = 30) -> dict[str, Any]:
    """The only numbers the AnalyticsAgent may use. Money is USD only; other currencies are counted separately."""
    since = utcnow() - timedelta(days=days)
    sales_stmt = select(Sale).where(Sale.sold_at >= since, Sale.is_test.is_(False))
    clicks_stmt = select(func.count(Click.id)).where(Click.created_at >= since)
    posts_stmt = select(func.count(MarketingPost.id)).where(
        MarketingPost.status == "published", MarketingPost.published_at >= since
    )
    emails_stmt = select(EmailLog.status, func.count(EmailLog.id)).where(EmailLog.created_at >= since)
    if ebook_id is not None:
        sales_stmt = sales_stmt.where(Sale.ebook_id == ebook_id)
        clicks_stmt = clicks_stmt.where(Click.ebook_id == ebook_id)
        posts_stmt = posts_stmt.where(MarketingPost.ebook_id == ebook_id)
        emails_stmt = emails_stmt.where(EmailLog.ebook_id == ebook_id)

    rows = db.scalars(sales_stmt).all()
    usd_rows = [row for row in rows if row.currency == "usd"]
    paid = [row for row in usd_rows if not row.is_refunded]
    refunded = [row for row in usd_rows if row.is_refunded]
    gross = sum(row.amount_cents for row in paid)
    fees = sum(row.fee_cents for row in paid)
    net = sum(row.net_cents for row in paid)

    clicks = int(db.scalar(clicks_stmt) or 0)
    published_posts = int(db.scalar(posts_stmt) or 0)
    email_counts = {status: int(count) for status, count in db.execute(emails_stmt.group_by(EmailLog.status)).all()}

    products: dict[int | None, dict[str, Any]] = {}
    for row in paid:
        entry = products.setdefault(
            row.product_id,
            {"product_id": row.product_id, "units": 0, "gross_usd": 0, "net_usd": 0, "platforms": set()},
        )
        entry["units"] += 1
        entry["gross_usd"] += row.amount_cents
        entry["net_usd"] += row.net_cents
        entry["platforms"].add(row.platform)
    product_rows = []
    for entry in sorted(products.values(), key=lambda item: item["units"], reverse=True):
        product = db.get(Product, entry["product_id"]) if entry["product_id"] else None
        product_rows.append(
            {
                "product": product.name if product else "unlinked",
                "tier": product.tier if product else None,
                "units": entry["units"],
                "gross_usd": _usd(entry["gross_usd"]),
                "net_usd": _usd(entry["net_usd"]),
                "platforms": sorted(entry["platforms"]),
            }
        )

    sales_per_100_clicks = round(len(paid) / clicks * 100, 2) if clicks >= MIN_CLICKS_FOR_RATE else None
    refund_rate = round(len(refunded) / len(usd_rows) * 100, 2) if usd_rows else 0.0
    return {
        "as_of_utc": utcnow().isoformat(timespec="seconds") + "Z",
        "window_days": days,
        "ebook_id": ebook_id,
        "sales": {
            "paid_units": len(paid),
            "refunded_units": len(refunded),
            "refund_rate_percent": refund_rate,
            "gross_usd": _usd(gross),
            "fees_usd": _usd(fees),
            "net_usd": _usd(net),
            "non_usd_units_not_included": len(rows) - len(usd_rows),
        },
        "traffic": {
            "clicks": clicks,
            "sales_per_100_clicks": sales_per_100_clicks,
            "minimum_clicks_for_rate": MIN_CLICKS_FOR_RATE,
        },
        "marketing": {"instagram_posts_published": published_posts, "email_counts_by_status": email_counts},
        "products": product_rows,
        "data_note": (
            "Sample sizes are small; treat rates as directional."
            if len(paid) < 5 or clicks < MIN_CLICKS_FOR_RATE
            else "Sample sizes are adequate for directional comparisons."
        ),
    }
