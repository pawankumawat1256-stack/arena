"""Buyers: accounts, expiring download links, the access email, and the onboarding and launch email sequences.

Access policy:
- A paid, non-test sale that is not refunded, not under an open dispute, and not an ended subscription gets access.
- The access link is emailed once per purchase (as a Gmail draft that is sent at once), unless the buyer already has
  an active link for the same product, which covers subscription renewals.
- Links expire after BUYER_LINK_TTL_HOURS and allow BUYER_MAX_DOWNLOADS downloads. Only the SHA-256 hash is stored.
- Marketing emails (onboarding and launch) are sent only when BUSINESS_POSTAL_ADDRESS is set, and always carry an
  unsubscribe link. Access emails are transactional and carry no marketing footer.
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from config import settings, utcnow
from db.models import (
    BuyerAccess,
    Ebook,
    EmailLog,
    Product,
    Sale,
    SequenceEnrollment,
    User,
)
from integrations import gmail_client
from services import security

logger = logging.getLogger(__name__)

SEQUENCE_ACCESS = "access"
SEQUENCE_ONBOARDING = "onboarding"
SEQUENCE_LAUNCH = "launch"
MAX_STEP_FAILURES = 3
RETRY_AFTER_FAILURE = timedelta(hours=6)


class AccessDenied(Exception):
    """A download was refused. `reason` decides the HTTP status in main.py."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


# ---------- People ----------


def get_or_create_user(db: Session, *, email: str, full_name: str | None = None, country: str | None = None, source: str = "unknown") -> User:
    normalized = email.strip().lower()
    user = db.scalar(select(User).where(User.email == normalized))
    if user is None:
        user = User(email=normalized, full_name=full_name, country=country, source=source)
        db.add(user)
        db.flush()
        return user
    if full_name and not user.full_name:
        user.full_name = full_name
    if country and not user.country:
        user.country = country
    return user


# ---------- Download links ----------


def download_url(token: str) -> str:
    return f"{settings.public_base_url.rstrip('/')}/download/{token}"


def _new_grant(db: Session, *, sale: Sale, ebook: Ebook, product: Product) -> tuple[BuyerAccess, str]:
    token = security.new_token()
    grant = BuyerAccess(
        user_id=sale.user_id,
        ebook_id=ebook.id,
        product_id=product.id,
        sale_id=sale.id,
        token_hash=security.hash_token(token),
        expires_at=utcnow() + timedelta(hours=settings.buyer_link_ttl_hours),
        max_downloads=settings.buyer_max_downloads,
        download_count=0,
        revoked=False,
    )
    db.add(grant)
    db.flush()
    return grant, token


def ensure_access(db: Session, *, sale: Sale, ebook: Ebook, product: Product, buyer_email: str | None) -> BuyerAccess:
    """Create the sale's access grant once, or restore it if it was revoked. Sends the link only for new buyers."""
    grant = db.scalar(select(BuyerAccess).where(BuyerAccess.sale_id == sale.id))
    if grant is not None:
        grant.revoked = False
        return grant
    already_has_access = False
    if sale.user_id is not None:
        already_has_access = (
            db.scalar(
                select(BuyerAccess.id)
                .where(
                    BuyerAccess.user_id == sale.user_id,
                    BuyerAccess.product_id == product.id,
                    BuyerAccess.revoked.is_(False),
                )
                .limit(1)
            )
            is not None
        )
    grant, token = _new_grant(db, sale=sale, ebook=ebook, product=product)
    if already_has_access:
        logger.info("Sale %s is a renewal; access link not resent", sale.external_id)
        return grant
    if not buyer_email:
        logger.warning("Sale %s has no buyer email; access link not sent", sale.external_id)
        return grant
    send_access_email(db, grant=grant, token=token, ebook=ebook, product=product, buyer_email=buyer_email)
    return grant


def send_access_email(db: Session, *, grant: BuyerAccess, token: str, ebook: Ebook, product: Product, buyer_email: str) -> bool:
    link = download_url(token)
    subject = f"Your download: {ebook.title}"[:255]
    body = (
        "Thank you for your purchase.\n\n"
        f"Download {product.name} here:\n{link}\n\n"
        f"The link works for {settings.buyer_link_ttl_hours} hours and allows up to "
        f"{settings.buyer_max_downloads} downloads. If it expires, reply to this email and a new link will be sent.\n"
    )
    return _send_and_log(
        db,
        to_email=buyer_email,
        subject=subject,
        body=body,
        sequence=SEQUENCE_ACCESS,
        ebook_id=ebook.id,
        user_id=grant.user_id,
        sale_id=grant.sale_id,
        step=None,
        headers=None,
    )


def resend_access(db: Session, *, sale_id: int) -> tuple[BuyerAccess, bool]:
    """Issue a fresh link for an existing grant. The old link stops working. Returns (grant, email_sent)."""
    grant = db.scalar(select(BuyerAccess).where(BuyerAccess.sale_id == sale_id))
    sale = db.get(Sale, sale_id)
    if grant is None or sale is None:
        raise LookupError("No access grant exists for that sale")
    if sale.is_refunded:
        raise ValueError("The sale is refunded, so access cannot be resent")
    if not sale.buyer_email:
        raise ValueError("The sale has no buyer email")
    token = security.new_token()
    grant.token_hash = security.hash_token(token)
    grant.expires_at = utcnow() + timedelta(hours=settings.buyer_link_ttl_hours)
    grant.download_count = 0
    grant.revoked = False
    ebook = db.get(Ebook, grant.ebook_id)
    product = db.get(Product, grant.product_id)
    sent = send_access_email(db, grant=grant, token=token, ebook=ebook, product=product, buyer_email=sale.buyer_email)
    db.commit()
    return grant, sent


def resolve_product_file(product: Product) -> Path:
    if not product.file_path:
        raise AccessDenied("no_file", "This product has no downloadable file yet.")
    path = Path(product.file_path).resolve()
    root = settings.data_dir.resolve()
    if root not in path.parents or not path.is_file():
        raise AccessDenied("no_file", "The file for this product is not available yet.")
    return path


def consume_download(db: Session, token: str) -> tuple[BuyerAccess, Path]:
    """Validate a token and count one download, atomically. Raises AccessDenied when the download is refused."""
    grant = db.scalar(select(BuyerAccess).where(BuyerAccess.token_hash == security.hash_token(token)))
    if grant is None:
        raise AccessDenied("not_found", "This download link is not valid.")
    if grant.revoked:
        raise AccessDenied("revoked", "Access to this product has been revoked.")
    if grant.expires_at <= utcnow():
        raise AccessDenied("expired", "This download link has expired. Reply to your purchase email for a new one.")
    if grant.download_count >= grant.max_downloads:
        raise AccessDenied("limit", "This download link has reached its download limit.")
    product = db.get(Product, grant.product_id)
    if product is None:
        raise AccessDenied("no_file", "This product no longer exists.")
    path = resolve_product_file(product)
    now = utcnow()
    result = db.execute(
        update(BuyerAccess)
        .where(
            BuyerAccess.id == grant.id,
            BuyerAccess.revoked.is_(False),
            BuyerAccess.download_count < BuyerAccess.max_downloads,
        )
        .values(download_count=BuyerAccess.download_count + 1, last_downloaded_at=now)
    )
    if result.rowcount != 1:
        db.rollback()
        raise AccessDenied("limit", "This download link has reached its download limit.")
    db.commit()
    db.refresh(grant)
    return grant, path


def revoke_for_sale(db: Session, sale_id: int) -> None:
    db.execute(update(BuyerAccess).where(BuyerAccess.sale_id == sale_id).values(revoked=True))


def revoke_for_user_product(db: Session, user_id: int, product_id: int) -> None:
    db.execute(
        update(BuyerAccess)
        .where(BuyerAccess.user_id == user_id, BuyerAccess.product_id == product_id)
        .values(revoked=True)
    )


# ---------- Email sequences ----------


def sequence_emails(ebook: Ebook, sequence: str) -> list[dict[str, Any]]:
    if not ebook.email_sequences_json:
        return []
    data = json.loads(ebook.email_sequences_json)
    emails = data.get(sequence) or []
    return [item for item in emails if isinstance(item, dict)]


def _delay_days(email: dict[str, Any]) -> int:
    return int(email.get("delay_days") or 0)


def enroll(db: Session, *, user: User, ebook: Ebook, sequence: str) -> SequenceEnrollment | None:
    """Enroll a person in a sequence once. Returns None when the ebook has no emails for that sequence yet."""
    emails = sequence_emails(ebook, sequence)
    if not emails:
        return None
    existing = db.scalar(
        select(SequenceEnrollment).where(
            SequenceEnrollment.user_id == user.id,
            SequenceEnrollment.ebook_id == ebook.id,
            SequenceEnrollment.sequence == sequence,
        )
    )
    if existing is not None:
        return existing
    now = utcnow()
    enrollment = SequenceEnrollment(
        user_id=user.id,
        ebook_id=ebook.id,
        sequence=sequence,
        next_step=0,
        enrolled_at=now,
        next_send_at=now + timedelta(days=_delay_days(emails[0])),
        status="unsubscribed" if user.unsubscribed else "active",
    )
    db.add(enrollment)
    db.flush()
    return enrollment


def unsubscribe_url(email: str) -> str:
    query = urlencode({"email": email, "sig": security.unsubscribe_signature(email)})
    return f"{settings.public_base_url.rstrip('/')}/unsubscribe?{query}"


def _send_and_log(
    db: Session,
    *,
    to_email: str,
    subject: str,
    body: str,
    sequence: str,
    ebook_id: int | None,
    user_id: int | None,
    sale_id: int | None,
    step: int | None,
    headers: dict[str, str] | None,
) -> bool:
    log = EmailLog(
        ebook_id=ebook_id,
        user_id=user_id,
        sale_id=sale_id,
        to_email=to_email,
        sequence=sequence,
        step=step,
        subject=subject[:255],
        body_text=body,
        status="failed",
    )
    try:
        message_id = gmail_client.create_draft_and_send(to=to_email, subject=subject, text=body, headers=headers)
        log.status = "sent"
        log.provider_message_id = message_id[:128] or None
        log.sent_at = utcnow()
        ok = True
    except Exception as exc:  # Gmail not authorized, quota, or network: recorded for retry or resend
        logger.warning("Email (%s) to %s failed: %s", sequence, to_email, exc)
        log.error = str(exc)[:2000]
        ok = False
    db.add(log)
    db.flush()
    return ok


def _advance(db: Session, enrollment: SequenceEnrollment, now) -> str:
    user = db.get(User, enrollment.user_id)
    ebook = db.get(Ebook, enrollment.ebook_id)
    if user is None or ebook is None:
        enrollment.status = "paused"
        return "skipped"
    if user.unsubscribed:
        enrollment.status = "unsubscribed"
        return "skipped"
    emails = sequence_emails(ebook, enrollment.sequence)
    if enrollment.next_step >= len(emails):
        enrollment.status = "completed"
        return "skipped"
    if not settings.business_postal_address:
        enrollment.next_send_at = now + RETRY_AFTER_FAILURE
        logger.warning("Marketing email blocked: BUSINESS_POSTAL_ADDRESS is not set")
        return "skipped"

    email = emails[enrollment.next_step]
    link = unsubscribe_url(user.email)
    body = (
        f"{str(email.get('body_text', '')).strip()}\n\n--\n"
        f"{settings.business_postal_address}\nUnsubscribe: {link}\n"
    )
    headers = {"List-Unsubscribe": f"<{link}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}
    sent = _send_and_log(
        db,
        to_email=user.email,
        subject=str(email.get("subject", ""))[:255],
        body=body,
        sequence=enrollment.sequence,
        ebook_id=ebook.id,
        user_id=user.id,
        sale_id=None,
        step=enrollment.next_step,
        headers=headers,
    )
    if not sent:
        failures = db.scalar(
            select(func.count(EmailLog.id)).where(
                EmailLog.user_id == user.id,
                EmailLog.ebook_id == ebook.id,
                EmailLog.sequence == enrollment.sequence,
                EmailLog.step == enrollment.next_step,
                EmailLog.status == "failed",
            )
        ) or 0
        if failures >= MAX_STEP_FAILURES:
            enrollment.status = "paused"
        else:
            enrollment.next_send_at = now + RETRY_AFTER_FAILURE
        return "failed"

    enrollment.next_step += 1
    if enrollment.next_step >= len(emails):
        enrollment.status = "completed"
    else:
        enrollment.next_send_at = enrollment.enrolled_at + timedelta(days=_delay_days(emails[enrollment.next_step]))
    return "sent"


def process_due_enrollments(db: Session, *, now=None, batch: int | None = None) -> dict[str, int]:
    """Send at most one due email per enrollment. Run every 15 minutes by the scheduler."""
    now = now or utcnow()
    due = db.scalars(
        select(SequenceEnrollment)
        .where(SequenceEnrollment.status == "active", SequenceEnrollment.next_send_at <= now)
        .order_by(SequenceEnrollment.next_send_at)
        .limit(batch or settings.email_batch_limit)
    ).all()
    totals = {"sent": 0, "failed": 0, "skipped": 0}
    for enrollment in due:
        totals[_advance(db, enrollment, now)] += 1
        db.commit()
    return totals


def add_lead(db: Session, *, email: str, ebook: Ebook, full_name: str | None, country: str | None, source: str) -> dict[str, Any]:
    user = get_or_create_user(db, email=email, full_name=full_name, country=country, source=source)
    enrollment = enroll(db, user=user, ebook=ebook, sequence=SEQUENCE_LAUNCH)
    db.commit()
    return {
        "user_id": user.id,
        "enrolled": enrollment is not None,
        "enrollment_status": enrollment.status if enrollment else "no launch emails yet; run POST /market first",
    }


def unsubscribe(db: Session, *, email: str, signature: str | None) -> bool:
    if not security.verify_signature(security.unsubscribe_message(email), signature):
        return False
    normalized = email.strip().lower()
    user = db.scalar(select(User).where(User.email == normalized))
    if user is not None:
        user.unsubscribed = True
        db.execute(
            update(SequenceEnrollment)
            .where(SequenceEnrollment.user_id == user.id, SequenceEnrollment.status == "active")
            .values(status="unsubscribed")
        )
    db.commit()
    return True
