"""Pipeline: runs the agents in order, stores their output, and records every run in the database.

CLI: python -m core.pipeline "Topic" --audience "Who it is for" [--title "..."] [--sources-file sources.json]
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from config import settings, utcnow
from core import pricing
from core.agent import (
    AnalyticsAgent,
    DesignerAgent,
    MarketingAgent,
    PricingAgent,
    ResearchAgent,
    WriterAgent,
)
from db.database import SessionLocal, init_db
from db.models import AgentRun, Ebook, Product
from integrations import notion_client
from integrations.notion_client import NotionError
from services import marketing, sales

logger = logging.getLogger(__name__)
Stage = Callable[[Session, Ebook | None, dict], Any]


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:100] or "ebook"


def unique_slug(db: Session, base: str) -> str:
    slug, counter = base, 2
    while db.scalar(select(Ebook.id).where(Ebook.slug == slug)) is not None:
        slug = f"{base}-{counter}"
        counter += 1
    return slug


def create_ebook(db: Session, *, topic: str, audience: str, title: str | None = None) -> Ebook:
    ebook = Ebook(
        slug=unique_slug(db, slugify(title or topic)),
        title=title or topic,
        topic=topic,
        audience=audience,
        stage="new",
        list_price_cents=round(settings.kdp_list_price_usd * 100),
    )
    db.add(ebook)
    db.flush()
    return ebook


def _stage_research(db: Session, ebook: Ebook | None, payload: dict) -> dict:
    assert ebook is not None
    topic = payload.get("topic") or ebook.topic
    audience = payload.get("audience") or ebook.audience
    result = ResearchAgent().run(topic=topic, audience=audience, sources=payload.get("sources") or [])
    option = result["title_options"][0]
    ebook.topic, ebook.audience = topic, audience
    ebook.title = payload.get("title") or option.get("title") or ebook.title
    ebook.subtitle = payload.get("subtitle") or option.get("subtitle") or None
    ebook.research_json = json.dumps(result, ensure_ascii=False)
    ebook.outline_json = json.dumps(result["outline"], ensure_ascii=False)
    ebook.stage = "researched"
    return result


def _stage_write(db: Session, ebook: Ebook | None, payload: dict) -> dict:
    assert ebook is not None
    outline = json.loads(ebook.outline_json or "[]")
    if not outline:
        raise ValueError("No outline yet. Run research first.")
    book = WriterAgent().write_book(book_title=ebook.title, subtitle=ebook.subtitle or "", audience=ebook.audience, outline=outline)
    header = f"# {ebook.title}\n\n" + (f"{ebook.subtitle}\n\n" if ebook.subtitle else "")
    markdown = header + book["markdown"] + "\n"
    path = settings.data_dir / "manuscripts" / f"{ebook.slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown, encoding="utf-8")
    ebook.manuscript_path = str(path)
    ebook.stage = "written"
    notion_url = None
    if settings.notion_api_key and settings.notion_parent_page_id:
        try:  # best effort: the local manuscript is already saved
            page = notion_client.create_notion_page(ebook.title, markdown)
            notion_url = page.get("url")
            ebook.notion_page_url = notion_url
        except NotionError as exc:
            logger.warning("Manuscript saved locally, but the Notion page failed: %s", exc)
    return {"manuscript_path": str(path), "chapters": book["chapters"], "words": len(markdown.split()), "notion_url": notion_url}


def _stage_design(db: Session, ebook: Ebook | None, payload: dict) -> dict:
    assert ebook is not None
    result = DesignerAgent().run(book_title=ebook.title, subtitle=ebook.subtitle or "", audience=ebook.audience, style_notes=str(payload.get("style_notes") or "none"))
    ebook.cover_concepts_json = json.dumps(result, ensure_ascii=False)
    ebook.stage = "designed"
    return result


def _stage_price(db: Session, ebook: Ebook | None, payload: dict) -> dict:
    assert ebook is not None
    plan = pricing.build_pricing_plan(
        basic_usd=ebook.list_price_cents / 100,
        file_mb=settings.kdp_file_size_mb,
        toolkit_usd=settings.toolkit_price_usd,
        bundle_usd=settings.bundle_price_usd,
        withholding_rate=Decimal(str(settings.amazon_in_withholding_rate)),
    )
    if plan["errors"]:
        raise ValueError("Pricing check failed: " + " ".join(plan["errors"]))
    narrative = PricingAgent().run(book_title=ebook.title, audience=ebook.audience, plan=plan)
    ebook.pricing_json = json.dumps({"plan": plan, "narrative": narrative}, ensure_ascii=False)
    ebook.stage = "priced"
    return {"plan": plan, "narrative": narrative, "warnings": plan["warnings"]}


def _stage_market(db: Session, ebook: Ebook | None, payload: dict) -> dict:
    assert ebook is not None
    products = db.scalars(select(Product).where(Product.ebook_id == ebook.id, Product.active.is_(True)).order_by(Product.price_cents)).all()
    offer = "; ".join(f"{p.name} ({p.tier}, ${p.price_cents / 100:.2f}, sold on {p.platform})" for p in products) or "not yet priced"
    bundle = MarketingAgent().run(
        book_title=ebook.title,
        subtitle=ebook.subtitle or "",
        audience=ebook.audience,
        offer=offer,
        post_count=settings.marketing_posts_per_week,
    )
    posts = marketing.store_marketing_bundle(db, ebook, bundle)
    ebook.stage = "marketed"
    return {
        "posts_created": len(posts),
        "post_statuses": [p.status for p in posts],
        "image_briefs": [str(item.get("image_brief", "")) for item in bundle["social_posts"]],
        "email_sequences": bundle["email_sequences"],
    }


def _stage_analytics(db: Session, ebook: Ebook | None, payload: dict) -> dict:
    snapshot = sales.ebook_metrics(db, ebook_id=ebook.id if ebook else None, days=int(payload.get("days", 30)))
    report = AnalyticsAgent().run(snapshot=snapshot)
    return {"snapshot": snapshot, "report": report}


def _stage_pipeline(db: Session, ebook: Ebook | None, payload: dict) -> dict:
    assert ebook is not None
    results: dict[str, Any] = {}
    for name in ("research", "write", "design", "price", "market"):
        results[name] = TASKS[name](db, ebook, payload)
        db.commit()  # keep each finished stage even if a later one fails
    return results


TASKS: dict[str, Stage] = {
    "research": _stage_research,
    "write": _stage_write,
    "design": _stage_design,
    "price": _stage_price,
    "market": _stage_market,
    "analytics": _stage_analytics,
    "pipeline": _stage_pipeline,
}
EBOOK_TASKS = {"research", "write", "design", "price", "market", "pipeline"}


def execute_run(run_id: int) -> None:
    """Execute one queued AgentRun. Runs in background tasks and the scheduler; never raises."""
    db = SessionLocal()
    run: AgentRun | None = None
    try:
        run = db.get(AgentRun, run_id)
        if run is None:
            return
        run.status = "running"
        db.commit()
        stage = TASKS.get(run.task)
        if stage is None:
            raise ValueError(f"Unknown task '{run.task}'")
        ebook = db.get(Ebook, run.ebook_id) if run.ebook_id else None
        if ebook is None and run.task in EBOOK_TASKS:
            raise ValueError(f"Task '{run.task}' needs an ebook_id")
        payload = json.loads(run.input_json or "{}")
        output = stage(db, ebook, payload)
        db.commit()
        run = db.get(AgentRun, run_id)
        run.output_json = json.dumps(output, ensure_ascii=False, default=str)
        run.status = "succeeded"
    except Exception as exc:
        logger.exception("Run %s failed", run_id)
        db.rollback()
        run = db.get(AgentRun, run_id)
        if run is not None:
            run.status = "failed"
            run.error = f"{type(exc).__name__}: {exc}"[:4000]
    finally:
        if run is not None:
            run.finished_at = utcnow()
        db.commit()
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the full ebook pipeline for one topic.")
    parser.add_argument("topic")
    parser.add_argument("--audience", required=True)
    parser.add_argument("--title")
    parser.add_argument("--sources-file", type=Path, help="JSON list of {url, note} objects")
    args = parser.parse_args()
    logging.basicConfig(level=settings.log_level.upper(), format="%(levelname)s %(name)s: %(message)s")
    init_db()
    sources = json.loads(args.sources_file.read_text(encoding="utf-8")) if args.sources_file else []
    payload = {"topic": args.topic, "audience": args.audience, "title": args.title, "sources": sources}
    with SessionLocal() as db:
        ebook = create_ebook(db, topic=args.topic, audience=args.audience, title=args.title)
        run = AgentRun(task="pipeline", ebook_id=ebook.id, status="queued", input_json=json.dumps(payload, ensure_ascii=False))
        db.add(run)
        db.commit()
        run_id = run.id
    execute_run(run_id)
    with SessionLocal() as db:
        run = db.get(AgentRun, run_id)
        print(f"run {run.id}: {run.status}")
        if run.error:
            print(f"error: {run.error}")
        print(f"ebook id {run.ebook_id}; output stored in agent_runs.output_json")


if __name__ == "__main__":
    main()
