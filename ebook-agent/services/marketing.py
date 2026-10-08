"""Marketing: store generated posts and email sequences, schedule Instagram posts, and publish them within quota."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from config import settings, utcnow
from db.models import Ebook, MarketingPost
from integrations import instagram
from integrations.instagram import InstagramError

logger = logging.getLogger(__name__)

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
SLOT_SPACING_DAYS = 2
PUBLISH_BATCH = 10
STALE_PUBLISHING_AFTER = timedelta(minutes=30)
RETRY_AFTER_TRANSIENT_FAILURE = timedelta(minutes=30)


@dataclass
class QuotaState:
    used: int
    total: int
    source: str  # "live" from the Graph API, or "configured" as a fallback

    @property
    def remaining(self) -> int:
        return max(0, self.total - self.used)


def store_marketing_bundle(db: Session, ebook: Ebook, bundle: dict[str, Any]) -> list[MarketingPost]:
    """Save the email sequences on the ebook and create one draft post per caption."""
    ebook.email_sequences_json = json.dumps(bundle["email_sequences"], ensure_ascii=False)
    posts: list[MarketingPost] = []
    for item in bundle["social_posts"]:
        tags = " ".join(
            "#" + str(tag).strip().lstrip("#").replace(" ", "")
            for tag in item.get("hashtags", [])
            if str(tag).strip()
        )
        post = MarketingPost(
            ebook_id=ebook.id,
            platform="instagram",
            caption=str(item["caption"]).strip(),
            hashtags=tags[:500],
            status="draft",
        )
        db.add(post)
        posts.append(post)
    db.flush()
    return posts


def _caption_with_tags(post: MarketingPost) -> str:
    return f"{post.caption}\n\n{post.hashtags}".strip() if post.hashtags else post.caption


def set_post_image(db: Session, post: MarketingPost, image_url: str) -> None:
    if post.status in ("publishing", "published"):
        raise ValueError("A post that is publishing or published cannot change its image")
    post.image_url = image_url
    db.commit()


def schedule_post(db: Session, post: MarketingPost, scheduled_for: datetime) -> None:
    if not post.image_url:
        raise ValueError("Set an HTTPS image URL before scheduling this post")
    if post.status not in ("draft", "scheduled", "failed"):
        raise ValueError(f"A post with status {post.status} cannot be scheduled")
    post.scheduled_for = scheduled_for
    post.status = "scheduled"
    post.error = None
    db.commit()


def schedule_weekly_slots(db: Session, *, now: datetime | None = None) -> int:
    """Turn the next N draft posts that have an image into scheduled posts, two days apart from the configured weekday."""
    now = now or utcnow()
    tz = ZoneInfo(settings.scheduler_timezone)
    local_now = now.replace(tzinfo=timezone.utc).astimezone(tz)
    days_ahead = (WEEKDAYS.index(settings.marketing_cron_weekday.lower()[:3]) - local_now.weekday()) % 7
    base = (local_now + timedelta(days=days_ahead)).replace(hour=settings.marketing_cron_hour, minute=0, second=0, microsecond=0)
    if base <= local_now:
        base += timedelta(days=7)
    drafts = db.scalars(
        select(MarketingPost)
        .where(MarketingPost.status == "draft", MarketingPost.image_url.is_not(None))
        .order_by(MarketingPost.created_at, MarketingPost.id)
        .limit(settings.marketing_posts_per_week)
    ).all()
    for index, post in enumerate(drafts):
        slot_local = base + timedelta(days=index * SLOT_SPACING_DAYS)
        post.scheduled_for = slot_local.astimezone(timezone.utc).replace(tzinfo=None)
        post.status = "scheduled"
    db.commit()
    return len(drafts)


def current_quota(db: Session, *, now: datetime | None = None) -> QuotaState:
    """Live quota from the Graph API. Falls back to the configured limit minus our own posts in the last 24 hours."""
    try:
        live = instagram.get_quota()
        return QuotaState(used=live.used, total=live.total, source="live")
    except InstagramError as exc:
        logger.warning("Live Instagram quota unavailable (%s); using the configured limit", exc)
    since = (now or utcnow()) - timedelta(hours=24)
    used = db.scalar(
        select(func.count(MarketingPost.id)).where(
            MarketingPost.platform == "instagram",
            MarketingPost.status == "published",
            MarketingPost.published_at >= since,
        )
    )
    return QuotaState(used=int(used or 0), total=settings.instagram_daily_post_limit, source="configured")


def publish_due_posts(db: Session, *, now: datetime | None = None) -> dict[str, Any]:
    """Publish scheduled posts that are due, while the publishing quota allows. Run every 15 minutes."""
    now = now or utcnow()
    if not instagram.is_configured():
        return {"published": 0, "failed": 0, "deferred": 0, "skipped": "Instagram is not configured"}

    db.execute(
        update(MarketingPost)
        .where(MarketingPost.status == "publishing", MarketingPost.scheduled_for < now - STALE_PUBLISHING_AFTER)
        .values(status="scheduled")
    )
    db.commit()

    quota = current_quota(db, now=now)
    due = db.scalars(
        select(MarketingPost)
        .where(
            MarketingPost.status == "scheduled",
            MarketingPost.scheduled_for <= now,
            MarketingPost.image_url.is_not(None),
        )
        .order_by(MarketingPost.scheduled_for, MarketingPost.id)
        .limit(PUBLISH_BATCH)
    ).all()

    result: dict[str, Any] = {"published": 0, "failed": 0, "deferred": 0}
    for post in due:
        if quota.remaining <= 0:
            result["deferred"] += 1
            continue
        post.status = "publishing"
        db.commit()
        try:
            media_id = instagram.publish_image(post.image_url, _caption_with_tags(post))
        except InstagramError as exc:
            post.error = str(exc)[:2000]
            if exc.quota_exhausted:
                post.status = "scheduled"
                post.scheduled_for = now + timedelta(hours=1)
                db.commit()
                logger.warning("Instagram post quota is used up; remaining posts deferred")
                result["deferred"] += 1
                break
            if exc.retryable:
                post.status = "scheduled"
                post.scheduled_for = now + RETRY_AFTER_TRANSIENT_FAILURE
            else:
                post.status = "failed"
            db.commit()
            result["failed"] += 1
            continue
        post.status = "published"
        post.published_at = now
        post.platform_post_id = media_id
        post.error = None
        db.commit()
        quota.used += 1
        result["published"] += 1
    result["quota"] = {"used": quota.used, "total": quota.total, "source": quota.source}
    return result
