"""FastAPI application: agent endpoints, webhooks, buyer downloads, and the unsubscribe link.

Run locally:  uvicorn main:app --reload --port 8000
API docs:     http://localhost:8000/docs

Protected endpoints need the header X-API-Key: <API_KEY>. Webhooks use their own checks (URL token for Gumroad,
Stripe-Signature for Stripe). Downloads use the secret token in the link.
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Literal

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    status,
)
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel, EmailStr, Field, HttpUrl
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from config import settings, utcnow
from core import pipeline
from db.database import SessionLocal, get_db, init_db
from db.models import AgentRun, Ebook, MarketingPost, Product
from integrations import drive_client, gumroad, stripe_client
from integrations.drive_client import DriveError
from integrations.gumroad import PingError
from integrations.stripe_client import StripeError
from services import buyers, marketing, sales, security

logger = logging.getLogger("api")

DENIAL_STATUS = {"not_found": 404, "no_file": 404, "revoked": 403, "limit": 403, "expired": 410}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Ebook Business Agent",
    version="1.0.0",
    description="Research, write, design, price, and market ebooks; sell through Gumroad, Stripe, and KDP.",
    lifespan=lifespan,
)
secure = APIRouter(dependencies=[Depends(security.require_api_key)])


# ---------- Request and response models ----------


class EbookCreate(BaseModel):
    topic: str = Field(min_length=3, max_length=300)
    audience: str = Field(min_length=3, max_length=500)
    title: str | None = Field(default=None, max_length=255)


class SourceIn(BaseModel):
    url: str = Field(min_length=8, max_length=500)
    note: str = Field(default="", max_length=500)


class ResearchRequest(BaseModel):
    ebook_id: int | None = None
    topic: str | None = Field(default=None, min_length=3, max_length=300)
    audience: str | None = Field(default=None, min_length=3, max_length=500)
    title: str | None = Field(default=None, max_length=255)
    subtitle: str | None = Field(default=None, max_length=255)
    sources: list[SourceIn] = Field(default_factory=list, max_length=20)


class EbookTask(BaseModel):
    ebook_id: int


class DesignRequest(EbookTask):
    style_notes: str = Field(default="none", max_length=500)


class AnalyticsRequest(BaseModel):
    ebook_id: int | None = None
    days: int = Field(default=30, ge=1, le=365)


class ProductCreate(BaseModel):
    ebook_id: int
    tier: Literal["basic", "pro", "bundle", "custom"]
    name: str = Field(min_length=2, max_length=255)
    price_usd: Decimal = Field(gt=0, max_digits=8, decimal_places=2)
    platform: Literal["kdp", "gumroad", "stripe", "manual"]
    external_product_id: str | None = Field(default=None, max_length=128)
    permalink: str | None = Field(default=None, max_length=128)
    file_path: str | None = Field(default=None, max_length=512)
    upload_to_drive: bool = False
    active: bool = True


class LeadCreate(BaseModel):
    email: EmailStr
    ebook_id: int
    full_name: str | None = Field(default=None, max_length=255)
    country: str | None = Field(default=None, max_length=8)
    source: str = Field(default="lead-form", max_length=32)


class ManualSaleCreate(BaseModel):
    ebook_id: int
    platform: Literal["kdp", "gumroad", "payhip", "stripe", "other"]
    product_id: int | None = None
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    fee: Decimal = Field(default=Decimal("0"), ge=0, max_digits=12, decimal_places=2)
    currency: Literal["usd", "inr", "gbp", "eur"] = "usd"
    sold_on: date | None = None
    country: str | None = Field(default=None, max_length=8)
    external_id: str | None = Field(default=None, max_length=128)


class PostImage(BaseModel):
    image_url: HttpUrl


class PostSchedule(BaseModel):
    scheduled_for: datetime


# ---------- Helpers ----------


def _to_cents(value: Decimal) -> int:
    return int((value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _as_utc_naive(value: datetime) -> datetime:
    """Naive datetimes are treated as UTC. Aware datetimes are converted to UTC."""
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _get_ebook_or_404(db: Session, ebook_id: int) -> Ebook:
    ebook = db.get(Ebook, ebook_id)
    if ebook is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Ebook {ebook_id} not found")
    return ebook


def _require_stage(value: str | None, message: str) -> None:
    if not value:
        raise HTTPException(status.HTTP_409_CONFLICT, message)


def _ebook_view(ebook: Ebook) -> dict[str, Any]:
    return {
        "id": ebook.id,
        "slug": ebook.slug,
        "title": ebook.title,
        "subtitle": ebook.subtitle,
        "topic": ebook.topic,
        "audience": ebook.audience,
        "stage": ebook.stage,
        "list_price_usd": f"{ebook.list_price_cents / 100:.2f}",
        "manuscript_path": ebook.manuscript_path,
        "notion_page_url": ebook.notion_page_url,
        "created_at": ebook.created_at.isoformat() + "Z",
    }


def _product_view(product: Product) -> dict[str, Any]:
    return {
        "id": product.id,
        "ebook_id": product.ebook_id,
        "tier": product.tier,
        "name": product.name,
        "price_usd": f"{product.price_cents / 100:.2f}",
        "platform": product.platform,
        "external_product_id": product.external_product_id,
        "permalink": product.permalink,
        "checkout_url": product.checkout_url,
        "file_path": product.file_path,
        "drive_file_id": product.drive_file_id,
        "active": product.active,
    }


def _post_view(post: MarketingPost) -> dict[str, Any]:
    return {
        "id": post.id,
        "ebook_id": post.ebook_id,
        "caption": post.caption,
        "hashtags": post.hashtags,
        "image_url": post.image_url,
        "status": post.status,
        "scheduled_for": post.scheduled_for.isoformat() + "Z" if post.scheduled_for else None,
        "published_at": post.published_at.isoformat() + "Z" if post.published_at else None,
        "platform_post_id": post.platform_post_id,
        "error": post.error,
    }


def _run_view(run: AgentRun) -> dict[str, Any]:
    output = json.loads(run.output_json) if run.status == "succeeded" and run.output_json else None
    return {
        "id": run.id,
        "task": run.task,
        "ebook_id": run.ebook_id,
        "status": run.status,
        "error": run.error,
        "created_at": run.created_at.isoformat() + "Z",
        "finished_at": run.finished_at.isoformat() + "Z" if run.finished_at else None,
        "output": output,
    }


def _queue_run(db: Session, background: BackgroundTasks, task: str, ebook_id: int | None, payload: dict[str, Any]) -> AgentRun:
    run = AgentRun(task=task, ebook_id=ebook_id, status="queued", input_json=json.dumps(payload, ensure_ascii=False, default=str))
    db.add(run)
    db.commit()
    db.refresh(run)
    background.add_task(pipeline.execute_run, run.id)
    return run


def _accepted(run: AgentRun) -> dict[str, Any]:
    return {"run_id": run.id, "task": run.task, "status": run.status, "poll": f"/runs/{run.id}"}


# ---------- Health and landing ----------


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse("/docs")


@app.get("/health")
def health() -> dict[str, str]:
    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
    except Exception as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"Database unavailable: {exc}") from exc
    return {"status": "ok", "time_utc": utcnow().isoformat() + "Z"}


# ---------- Ebooks and agent tasks ----------


@secure.post("/ebooks", status_code=201)
def create_ebook_endpoint(body: EbookCreate, db: Session = Depends(get_db)) -> dict[str, Any]:
    ebook = pipeline.create_ebook(db, topic=body.topic, audience=body.audience, title=body.title)
    db.commit()
    return _ebook_view(ebook)


@secure.get("/ebooks")
def list_ebooks(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    return [_ebook_view(e) for e in db.scalars(select(Ebook).order_by(Ebook.created_at.desc())).all()]


@secure.get("/ebooks/{ebook_id}")
def get_ebook(ebook_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    return _ebook_view(_get_ebook_or_404(db, ebook_id))


@secure.post("/research", status_code=202)
def research(body: ResearchRequest, background: BackgroundTasks, db: Session = Depends(get_db)) -> dict[str, Any]:
    if body.ebook_id is not None:
        ebook = _get_ebook_or_404(db, body.ebook_id)
        topic = body.topic or ebook.topic
        audience = body.audience or ebook.audience
    else:
        if not body.topic or not body.audience:
            raise HTTPException(422, "topic and audience are required when ebook_id is not given")
        topic, audience = body.topic, body.audience
        ebook = pipeline.create_ebook(db, topic=topic, audience=audience, title=body.title)
        db.commit()
    if not topic or not audience:
        raise HTTPException(422, "This ebook has no topic or audience yet")
    payload = {
        "topic": topic,
        "audience": audience,
        "title": body.title,
        "subtitle": body.subtitle,
        "sources": [source.model_dump() for source in body.sources],
    }
    return _accepted(_queue_run(db, background, "research", ebook.id, payload))


@secure.post("/write", status_code=202)
def write(body: EbookTask, background: BackgroundTasks, db: Session = Depends(get_db)) -> dict[str, Any]:
    ebook = _get_ebook_or_404(db, body.ebook_id)
    _require_stage(ebook.outline_json, "Run POST /research for this ebook first; there is no outline yet.")
    return _accepted(_queue_run(db, background, "write", ebook.id, {}))


@secure.post("/design", status_code=202)
def design(body: DesignRequest, background: BackgroundTasks, db: Session = Depends(get_db)) -> dict[str, Any]:
    ebook = _get_ebook_or_404(db, body.ebook_id)
    _require_stage(ebook.research_json, "Run POST /research for this ebook first; the cover needs the title.")
    return _accepted(_queue_run(db, background, "design", ebook.id, {"style_notes": body.style_notes}))


@secure.post("/price", status_code=202)
def price(body: EbookTask, background: BackgroundTasks, db: Session = Depends(get_db)) -> dict[str, Any]:
    ebook = _get_ebook_or_404(db, body.ebook_id)
    _require_stage(ebook.research_json, "Run POST /research for this ebook first; pricing needs the title.")
    return _accepted(_queue_run(db, background, "price", ebook.id, {}))


@secure.post("/market", status_code=202)
def market(body: EbookTask, background: BackgroundTasks, db: Session = Depends(get_db)) -> dict[str, Any]:
    ebook = _get_ebook_or_404(db, body.ebook_id)
    _require_stage(ebook.pricing_json, "Run POST /price for this ebook first so the offer is known.")
    return _accepted(_queue_run(db, background, "market", ebook.id, {}))


@secure.post("/pipeline", status_code=202)
def full_pipeline(body: ResearchRequest, background: BackgroundTasks, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Research, write, design, price, and market in one background run. Needs topic and audience."""
    if not body.topic or not body.audience:
        raise HTTPException(422, "topic and audience are required")
    ebook = pipeline.create_ebook(db, topic=body.topic, audience=body.audience, title=body.title)
    db.commit()
    payload = {"topic": body.topic, "audience": body.audience, "title": body.title, "subtitle": body.subtitle, "sources": [s.model_dump() for s in body.sources]}
    return {**_accepted(_queue_run(db, background, "pipeline", ebook.id, payload)), "ebook_id": ebook.id}


@secure.post("/analytics", status_code=202)
def analytics(body: AnalyticsRequest, background: BackgroundTasks, db: Session = Depends(get_db)) -> dict[str, Any]:
    if body.ebook_id is not None:
        _get_ebook_or_404(db, body.ebook_id)
    return _accepted(_queue_run(db, background, "analytics", body.ebook_id, {"days": body.days}))


@secure.get("/analytics/snapshot")
def analytics_snapshot(
    ebook_id: int | None = None,
    days: int = Query(default=30, ge=1, le=365),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return sales.ebook_metrics(db, ebook_id=ebook_id, days=days)


@secure.get("/runs")
def list_runs(
    ebook_id: int | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    stmt = select(AgentRun).order_by(AgentRun.created_at.desc(), AgentRun.id.desc()).limit(limit)
    if ebook_id is not None:
        stmt = stmt.where(AgentRun.ebook_id == ebook_id)
    return [_run_view(run) for run in db.scalars(stmt).all()]


@secure.get("/runs/{run_id}")
def get_run(run_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    run = db.get(AgentRun, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Run {run_id} not found")
    return _run_view(run)


# ---------- Products, leads, sales, posts ----------


@secure.post("/products", status_code=201)
def create_product(body: ProductCreate, db: Session = Depends(get_db)) -> dict[str, Any]:
    ebook = _get_ebook_or_404(db, body.ebook_id)
    price_cents = _to_cents(body.price_usd)
    if body.platform == "kdp" and not (299 <= price_cents <= 1299):
        raise HTTPException(
            422,
            "KDP list price must be between $2.99 and $12.99 to earn the 70% royalty",
        )
    file_path: str | None = None
    if body.file_path:
        resolved = Path(body.file_path).resolve()
        if settings.data_dir.resolve() not in resolved.parents or not resolved.is_file():
            raise HTTPException(422, "file_path must be an existing file inside DATA_DIR")
        file_path = str(resolved)
    drive_file_id: str | None = None
    if body.upload_to_drive:
        if not file_path:
            raise HTTPException(422, "upload_to_drive needs a file_path")
        try:
            drive_file_id = drive_client.upload_file(Path(file_path))["id"]
        except DriveError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    product = Product(
        ebook_id=ebook.id,
        tier=body.tier,
        name=body.name,
        price_cents=price_cents,
        platform=body.platform,
        external_product_id=body.external_product_id,
        permalink=body.permalink,
        file_path=file_path,
        drive_file_id=drive_file_id,
        active=body.active,
    )
    db.add(product)
    try:
        db.flush()
        if body.platform == "stripe":
            product.checkout_url = f"{settings.public_base_url.rstrip('/')}/buy/{product.id}"
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "A product with this tier or external id already exists") from exc
    return _product_view(product)


@secure.get("/products")
def list_products(ebook_id: int | None = None, db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    stmt = select(Product).order_by(Product.price_cents)
    if ebook_id is not None:
        stmt = stmt.where(Product.ebook_id == ebook_id)
    return [_product_view(p) for p in db.scalars(stmt).all()]


@secure.post("/leads", status_code=201)
def create_lead(body: LeadCreate, db: Session = Depends(get_db)) -> dict[str, Any]:
    ebook = _get_ebook_or_404(db, body.ebook_id)
    return buyers.add_lead(
        db,
        email=str(body.email),
        ebook=ebook,
        full_name=body.full_name,
        country=body.country,
        source=body.source,
    )


@secure.post("/sales/manual", status_code=201)
def manual_sale(body: ManualSaleCreate, db: Session = Depends(get_db)) -> dict[str, Any]:
    ebook = _get_ebook_or_404(db, body.ebook_id)
    product = db.get(Product, body.product_id) if body.product_id else None
    if body.product_id and (product is None or product.ebook_id != ebook.id):
        raise HTTPException(422, "product_id does not belong to this ebook")
    amount_cents = _to_cents(body.amount)
    fee_cents = _to_cents(body.fee)
    if fee_cents > amount_cents:
        raise HTTPException(422, "fee cannot be larger than amount")
    sold_at = datetime.combine(body.sold_on, datetime.min.time()) if body.sold_on else utcnow()
    try:
        record = sales.record_manual_sale(
            db,
            ebook=ebook,
            product=product,
            platform=body.platform,
            amount_cents=amount_cents,
            fee_cents=fee_cents,
            currency=body.currency,
            sold_at=sold_at,
            country=body.country,
            external_id=body.external_id,
        )
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "external_id already exists for this platform") from exc
    return {"id": record.id, "external_id": record.external_id, "net_cents": record.net_cents, "currency": record.currency}


@secure.post("/access/{sale_id}/resend")
def resend_access(sale_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        grant, sent = buyers.resend_access(db, sale_id=sale_id)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return {"grant_id": grant.id, "expires_at": grant.expires_at.isoformat() + "Z", "email_sent": sent}


@secure.get("/posts")
def list_posts(
    ebook_id: int | None = None,
    post_status: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    stmt = select(MarketingPost).order_by(MarketingPost.created_at.desc(), MarketingPost.id.desc()).limit(limit)
    if ebook_id is not None:
        stmt = stmt.where(MarketingPost.ebook_id == ebook_id)
    if post_status:
        stmt = stmt.where(MarketingPost.status == post_status)
    return [_post_view(p) for p in db.scalars(stmt).all()]


def _get_post_or_404(db: Session, post_id: int) -> MarketingPost:
    post = db.get(MarketingPost, post_id)
    if post is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Post {post_id} not found")
    return post


@secure.post("/posts/{post_id}/image")
def set_post_image(post_id: int, body: PostImage, db: Session = Depends(get_db)) -> dict[str, Any]:
    post = _get_post_or_404(db, post_id)
    try:
        marketing.set_post_image(db, post, str(body.image_url))
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return _post_view(post)


@secure.post("/posts/{post_id}/schedule")
def schedule_post(post_id: int, body: PostSchedule, db: Session = Depends(get_db)) -> dict[str, Any]:
    post = _get_post_or_404(db, post_id)
    scheduled_for = _as_utc_naive(body.scheduled_for)
    if scheduled_for <= utcnow():
        raise HTTPException(422, "scheduled_for must be in the future")
    try:
        marketing.schedule_post(db, post, scheduled_for)
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return _post_view(post)


@secure.post("/admin/gumroad/reconcile", status_code=202)
def reconcile_gumroad(background: BackgroundTasks) -> dict[str, str]:
    background.add_task(sales.reconcile_gumroad)
    return {"status": "queued", "detail": "Recent Gumroad sales are being re-read from the API"}


app.include_router(secure)


# ---------- Webhooks ----------


@app.post("/webhook/gumroad")
async def gumroad_webhook(
    request: Request,
    background: BackgroundTasks,
    token: str | None = Query(default=None),
) -> dict[str, Any]:
    """Gumroad Ping receiver. Stores the event and answers at once; the sale is read back in the background."""
    security.verify_gumroad_token(token)
    body = await request.body()
    try:
        payload = gumroad.parse_ping(body)
        event = gumroad.ping_event(payload)
    except PingError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    dedupe = gumroad.dedupe_key(event, body)

    def _store() -> tuple[int, str, bool]:
        with SessionLocal() as db:
            stored, is_new = sales.store_webhook_event(
                db, source="gumroad", dedupe_key=dedupe, event_type=event.resource_name, payload=payload
            )
            return stored.id, stored.status, is_new

    event_id, current_status, is_new = await run_in_threadpool(_store)
    if is_new or current_status in ("received", "failed"):
        background.add_task(sales.process_webhook_event, event_id)
    return {"ok": True, "event_id": event_id, "duplicate": not is_new}


@app.post("/webhook/stripe")
async def stripe_webhook(
    request: Request,
    background: BackgroundTasks,
    stripe_signature: str | None = Header(default=None, alias="Stripe-Signature"),
) -> dict[str, Any]:
    body = await request.body()
    try:
        event = stripe_client.verify_webhook(body, stripe_signature)
    except StripeError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    dedupe = f"stripe:{event['id']}"

    def _store() -> tuple[int, str, bool]:
        with SessionLocal() as db:
            stored, is_new = sales.store_webhook_event(
                db, source="stripe", dedupe_key=dedupe, event_type=str(event.get("type", "")), payload=event
            )
            return stored.id, stored.status, is_new

    event_id, current_status, is_new = await run_in_threadpool(_store)
    if is_new or current_status in ("received", "failed"):
        background.add_task(sales.process_webhook_event, event_id)
    return {"ok": True, "event_id": event_id, "duplicate": not is_new}


# ---------- Buyer pages ----------


@app.get("/buy/{product_id}", include_in_schema=False)
def buy(product_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    product = db.get(Product, product_id)
    if product is None or not product.active or product.platform != "stripe":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Product not found")
    base = settings.public_base_url.rstrip("/")
    try:
        url = stripe_client.create_checkout_url(
            product_id=product.id,
            ebook_id=product.ebook_id,
            product_name=product.name,
            price_cents=product.price_cents,
            success_url=f"{base}/thanks",
            cancel_url=f"{base}/",
        )
    except StripeError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    return RedirectResponse(url, status_code=303)


@app.get("/thanks", response_class=HTMLResponse, include_in_schema=False)
def thanks() -> str:
    return "<h1>Thank you for your purchase</h1><p>Check your email for your download link.</p>"


@app.get("/download/{token}", include_in_schema=False)
def download(token: str, db: Session = Depends(get_db)) -> FileResponse:
    try:
        _grant, path = buyers.consume_download(db, token)
    except buyers.AccessDenied as exc:
        raise HTTPException(DENIAL_STATUS.get(exc.reason, 403), exc.message) from exc
    return FileResponse(path, filename=path.name, media_type="application/octet-stream")


@app.api_route("/unsubscribe", methods=["GET", "POST"], response_class=HTMLResponse, include_in_schema=False)
def unsubscribe(
    email: str = Query(min_length=3, max_length=255),
    sig: str = Query(min_length=10, max_length=128),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    if not buyers.unsubscribe(db, email=email, signature=sig):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This unsubscribe link is not valid.")
    return HTMLResponse("<h1>You are unsubscribed</h1><p>You will not receive more marketing emails from us.</p>")
