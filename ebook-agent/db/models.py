"""ORM models (SQLAlchemy 2.0). Money is stored as integer cents. Datetimes are naive UTC."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from config import utcnow
from db.database import Base


class User(Base):
    """A buyer or a lead. The email address is the natural key."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    full_name: Mapped[Optional[str]] = mapped_column(String(255))
    country: Mapped[Optional[str]] = mapped_column(String(8))
    source: Mapped[str] = mapped_column(String(32), default="unknown")
    unsubscribed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    sales: Mapped[list["Sale"]] = relationship(back_populates="user")


class Ebook(Base):
    """One book project: research, manuscript, cover, pricing, and marketing outputs."""

    __tablename__ = "ebooks"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(255), default="")
    subtitle: Mapped[Optional[str]] = mapped_column(String(255))
    topic: Mapped[str] = mapped_column(String(300), default="")
    audience: Mapped[str] = mapped_column(String(500), default="")
    stage: Mapped[str] = mapped_column(String(32), default="new")
    list_price_cents: Mapped[int] = mapped_column(Integer, default=999)
    research_json: Mapped[Optional[str]] = mapped_column(Text)
    outline_json: Mapped[Optional[str]] = mapped_column(Text)
    cover_concepts_json: Mapped[Optional[str]] = mapped_column(Text)
    pricing_json: Mapped[Optional[str]] = mapped_column(Text)
    email_sequences_json: Mapped[Optional[str]] = mapped_column(Text)
    manuscript_path: Mapped[Optional[str]] = mapped_column(String(512))
    notion_page_url: Mapped[Optional[str]] = mapped_column(String(512))
    cover_image_path: Mapped[Optional[str]] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Product(Base):
    """A sellable item. One ebook can have several: Basic on KDP, Pro and Bundle on Gumroad or Stripe."""

    __tablename__ = "products"
    __table_args__ = (UniqueConstraint("ebook_id", "tier", name="uq_product_ebook_tier"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    ebook_id: Mapped[int] = mapped_column(ForeignKey("ebooks.id"), index=True)
    tier: Mapped[str] = mapped_column(String(16))  # basic | pro | bundle | custom
    name: Mapped[str] = mapped_column(String(255))
    price_cents: Mapped[int] = mapped_column(Integer)
    platform: Mapped[str] = mapped_column(String(32))  # kdp | gumroad | stripe | manual
    external_product_id: Mapped[Optional[str]] = mapped_column(String(128), unique=True)
    permalink: Mapped[Optional[str]] = mapped_column(String(128), index=True)
    checkout_url: Mapped[Optional[str]] = mapped_column(String(512))
    file_path: Mapped[Optional[str]] = mapped_column(String(512))
    drive_file_id: Mapped[Optional[str]] = mapped_column(String(128))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Sale(Base):
    """A sale, from Gumroad, Stripe, or a manual KDP royalty entry. Unique per (platform, external_id)."""

    __tablename__ = "sales"
    __table_args__ = (UniqueConstraint("platform", "external_id", name="uq_sale_platform_external"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    ebook_id: Mapped[int] = mapped_column(ForeignKey("ebooks.id"), index=True)
    product_id: Mapped[Optional[int]] = mapped_column(ForeignKey("products.id"), index=True)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), index=True)
    platform: Mapped[str] = mapped_column(String(32))
    external_id: Mapped[str] = mapped_column(String(128))
    buyer_email: Mapped[Optional[str]] = mapped_column(String(255))
    amount_cents: Mapped[int] = mapped_column(Integer)
    fee_cents: Mapped[int] = mapped_column(Integer, default=0)
    net_cents: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(8), default="usd")
    country: Mapped[Optional[str]] = mapped_column(String(8))
    is_refunded: Mapped[bool] = mapped_column(Boolean, default=False)
    is_test: Mapped[bool] = mapped_column(Boolean, default=False)
    sold_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    raw_json: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    user: Mapped[Optional[User]] = relationship(back_populates="sales")


class Click(Base):
    __tablename__ = "clicks"

    id: Mapped[int] = mapped_column(primary_key=True)
    ebook_id: Mapped[int] = mapped_column(ForeignKey("ebooks.id"), index=True)
    product_id: Mapped[Optional[int]] = mapped_column(ForeignKey("products.id"))
    channel: Mapped[str] = mapped_column(String(32), default="direct", index=True)
    campaign: Mapped[Optional[str]] = mapped_column(String(128))
    referrer: Mapped[Optional[str]] = mapped_column(String(512))
    user_agent: Mapped[Optional[str]] = mapped_column(String(255))
    ip_hash: Mapped[Optional[str]] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class MarketingPost(Base):
    __tablename__ = "marketing_posts"

    id: Mapped[int] = mapped_column(primary_key=True)
    ebook_id: Mapped[int] = mapped_column(ForeignKey("ebooks.id"), index=True)
    platform: Mapped[str] = mapped_column(String(32), default="instagram")
    caption: Mapped[str] = mapped_column(Text)
    hashtags: Mapped[str] = mapped_column(String(500), default="")
    image_url: Mapped[Optional[str]] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)  # draft|scheduled|publishing|published|failed
    scheduled_for: Mapped[Optional[datetime]] = mapped_column(DateTime, index=True)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    platform_post_id: Mapped[Optional[str]] = mapped_column(String(128))
    error: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class EmailLog(Base):
    __tablename__ = "email_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    ebook_id: Mapped[Optional[int]] = mapped_column(ForeignKey("ebooks.id"), index=True)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), index=True)
    sale_id: Mapped[Optional[int]] = mapped_column(ForeignKey("sales.id"), index=True)
    to_email: Mapped[str] = mapped_column(String(255))
    sequence: Mapped[str] = mapped_column(String(32))  # launch | onboarding | access
    step: Mapped[Optional[int]] = mapped_column(Integer)
    subject: Mapped[str] = mapped_column(String(255))
    body_text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), index=True)  # sent | failed
    provider_message_id: Mapped[Optional[str]] = mapped_column(String(128))
    error: Mapped[Optional[str]] = mapped_column(Text)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SequenceEnrollment(Base):
    __tablename__ = "sequence_enrollments"
    __table_args__ = (UniqueConstraint("user_id", "ebook_id", "sequence", name="uq_enrollment"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    ebook_id: Mapped[int] = mapped_column(ForeignKey("ebooks.id"), index=True)
    sequence: Mapped[str] = mapped_column(String(32))
    next_step: Mapped[int] = mapped_column(Integer, default=0)
    enrolled_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    next_send_at: Mapped[Optional[datetime]] = mapped_column(DateTime, index=True)
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)  # active|completed|unsubscribed


class BuyerAccess(Base):
    """An expiring download grant. Only the SHA-256 hash of the token is stored."""

    __tablename__ = "buyer_access"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), index=True)
    ebook_id: Mapped[int] = mapped_column(ForeignKey("ebooks.id"), index=True)
    product_id: Mapped[Optional[int]] = mapped_column(ForeignKey("products.id"), index=True)
    sale_id: Mapped[Optional[int]] = mapped_column(ForeignKey("sales.id"), unique=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    max_downloads: Mapped[int] = mapped_column(Integer, default=5)
    download_count: Mapped[int] = mapped_column(Integer, default=0)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_downloaded_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class AgentRun(Base):
    """One agent task. Status: queued -> running -> succeeded | failed."""

    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    task: Mapped[str] = mapped_column(String(32), index=True)
    ebook_id: Mapped[Optional[int]] = mapped_column(ForeignKey("ebooks.id"), index=True)
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    input_json: Mapped[Optional[str]] = mapped_column(Text)
    output_json: Mapped[Optional[str]] = mapped_column(Text)
    error: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class WebhookEvent(Base):
    """An inbound webhook, stored before processing so the endpoint can answer within Gumroad's 5-second window."""

    __tablename__ = "webhook_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32))  # gumroad | stripe
    dedupe_key: Mapped[str] = mapped_column(String(200), unique=True)
    event_type: Mapped[str] = mapped_column(String(64), default="")
    payload_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="received", index=True)  # received|processed|ignored|failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
