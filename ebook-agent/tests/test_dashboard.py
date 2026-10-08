"""Dashboard tests. dashboard.py runs through Streamlit's AppTest harness against the test database.

The API is replaced by a fake, so no test sends a request to a real server.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from streamlit.testing.v1 import AppTest

from config import settings, utcnow
from core import pricing
from db.models import AgentRun, Click, Ebook, MarketingPost, Product, Sale

SCRIPT = str(Path(__file__).resolve().parent.parent / "dashboard.py")
TIMEOUT = 60
IST = ZoneInfo("Asia/Kolkata")
PAGE_TITLES = {
    "Overview": "Overview",
    "Ebooks": "Ebooks",
    "Run agents": "Run agents",
    "Analyst": "Analyst",
    "Sales": "Sales",
    "Marketing": "Marketing",
    "Runs": "Agent runs",
}
MIDJOURNEY_PROMPT = "Flat navy field with one gold horizon line, no text --ar 5:8 --no text, letters, words, logo"
ANALYST_OUTPUT = {
    "snapshot": {"window_days": 30, "data_note": "Sample sizes are small; treat rates as directional."},
    "report": {
        "what_is_working": ["Two paid USD sales in 30 days ($38.00 gross)."],
        "what_is_failing": ["One of three USD sales was refunded (33.3%)."],
        "how_to_improve_sales": [
            {
                "action": "Ask refunded buyers for the reason in the refund email",
                "reason": "The refund rate is 33.3% on 3 USD sales",
                "metric_to_watch": "refund_rate_percent",
            }
        ],
        "data_gaps": ["Clicks by channel"],
    },
}


class FakeAPI:
    """Stands in for httpx.request. Records every call and returns the configured reply."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.status = 202
        self.body: Any = {"run_id": 7, "status": "queued"}

    def __call__(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append({"method": method, "url": url, "json": kwargs.get("json")})
        return httpx.Response(self.status, json=self.body, request=httpx.Request(method, url))


@pytest.fixture()
def api(monkeypatch) -> FakeAPI:
    fake = FakeAPI()
    monkeypatch.setattr(httpx, "request", fake)
    return fake


def new_app() -> AppTest:
    return AppTest.from_file(SCRIPT, default_timeout=TIMEOUT).run()


def go(at: AppTest, page: str) -> AppTest:
    at.sidebar.radio(key="page").set_value(page).run()
    assert_clean(at)
    return at


def assert_clean(at: AppTest) -> None:
    assert not at.exception, [item.value for item in at.exception]


def texts(items) -> list[str]:
    return [str(getattr(item, "value", "")) for item in items]


def button(at: AppTest, label: str):
    return next(item for item in at.button if item.label == label)


def seed_business(db) -> dict[str, Any]:
    """One priced ebook with a Pro product, paid and refunded sales, a test sale, clicks, a post and an analyst run."""
    plan = pricing.build_pricing_plan(basic_usd=9.99, file_mb=3.0, toolkit_usd=19.0, bundle_usd=24.0)
    assert plan["errors"] == []
    ebook = Ebook(
        slug="ai-advantage-test",
        title="AI Advantage for Solopreneurs",
        subtitle="Save 10 Hours a Week",
        topic="AI tools for solo businesses",
        audience="Solo founders",
        stage="priced",
        list_price_cents=999,
        outline_json=json.dumps(
            [{"chapter_title": "Chapter One", "goal": "Pick the first tool", "key_points": ["Point A", "Point B", "Point C"]}]
        ),
        research_json=json.dumps({"title_options": ["Option A"], "claims_to_verify": ["Tool X is free for 30 days"]}),
        cover_concepts_json=json.dumps(
            {
                "concepts": [
                    {
                        "name": f"Concept {n}",
                        "layout": "Artwork fills the top 60 percent, title sits below",
                        "colors": ["#1B2A41", "#F4D35E", "not a colour"],
                        "fonts": {"headline": "Playfair Display", "body": "Inter"},
                        "midjourney_prompt": MIDJOURNEY_PROMPT,
                        "dalle_prompt": "Portrait composition, no text, flat navy field",
                        "canva_steps": ["Open Canva Free", "Upload the artwork", "Add the title", "Download as PNG"],
                        "text_for_canva": {"title": "AI Advantage for Solopreneurs", "subtitle": "Save 10 Hours a Week"},
                    }
                    for n in (1, 2, 3)
                ]
            }
        ),
        pricing_json=json.dumps(
            {
                "plan": plan,
                "narrative": {
                    "tiers": [{"tier": "basic", "price_usd": "9.99"}, {"tier": "pro", "price_usd": "19.00"}],
                    "rationale": "The ladder starts at the book and adds a toolkit for people who want templates.",
                },
            }
        ),
        manuscript_path="/data/manuscripts/ai-advantage.md",
    )
    db.add(ebook)
    db.commit()
    db.refresh(ebook)
    product = Product(
        ebook_id=ebook.id,
        tier="pro",
        name="Pro toolkit",
        price_cents=1900,
        platform="gumroad",
        external_product_id="gum-pro-test",
        permalink="pro-toolkit",
        active=True,
    )
    db.add(product)
    db.commit()
    db.refresh(product)
    now = utcnow()
    db.add_all(
        [
            Sale(
                ebook_id=ebook.id,
                product_id=product.id,
                platform="gumroad",
                external_id="s-1",
                amount_cents=1900,
                fee_cents=240,
                net_cents=1660,
                currency="usd",
                buyer_email="reader@example.test",
                sold_at=now - timedelta(days=1),
            ),
            Sale(
                ebook_id=ebook.id,
                product_id=product.id,
                platform="gumroad",
                external_id="s-2",
                amount_cents=1900,
                fee_cents=240,
                net_cents=1660,
                currency="usd",
                sold_at=now - timedelta(days=2),
            ),
            Sale(
                ebook_id=ebook.id,
                product_id=product.id,
                platform="gumroad",
                external_id="s-3",
                amount_cents=1900,
                fee_cents=240,
                net_cents=1660,
                currency="usd",
                is_refunded=True,
                sold_at=now - timedelta(days=3),
            ),
            Sale(
                ebook_id=ebook.id,
                product_id=product.id,
                platform="gumroad",
                external_id="s-4",
                amount_cents=1900,
                fee_cents=240,
                net_cents=1660,
                currency="usd",
                is_test=True,
                sold_at=now - timedelta(days=1),
            ),
            Sale(
                ebook_id=ebook.id,
                platform="stripe",
                external_id="s-5",
                amount_cents=39900,
                fee_cents=0,
                net_cents=39900,
                currency="inr",
                sold_at=now - timedelta(days=1),
            ),
        ]
    )
    db.add_all([Click(ebook_id=ebook.id, channel="instagram", created_at=now) for _ in range(40)])
    post = MarketingPost(
        ebook_id=ebook.id,
        caption="Five minutes with one AI tool can replace a weekly admin task.",
        hashtags="#solopreneur #aitools",
        status="draft",
    )
    db.add(post)
    db.add(
        AgentRun(
            task="analytics",
            ebook_id=None,
            status="succeeded",
            input_json=json.dumps({"days": 30}),
            output_json=json.dumps(ANALYST_OUTPUT),
            created_at=now,
            finished_at=now,
        )
    )
    db.commit()
    db.refresh(post)
    return {"ebook": ebook, "product": product, "post": post}


# ---------- Access ----------


def test_login_is_required_when_a_password_is_set(monkeypatch):
    monkeypatch.setattr(settings, "dashboard_password", "correct-password")
    at = new_app()
    assert_clean(at)
    assert at.text_input[0].label == "Dashboard password"
    assert not at.sidebar.radio

    at.text_input[0].input("wrong-password")
    at.button[0].click().run()
    assert "Wrong password" in texts(at.error)

    at.text_input[0].input("correct-password")
    at.button[0].click().run()
    assert_clean(at)
    assert at.sidebar.radio[0].value == "Overview"


def test_production_refuses_to_start_without_a_password(monkeypatch):
    monkeypatch.setattr(settings, "dashboard_password", "")
    monkeypatch.setattr(settings, "app_env", "production")
    at = new_app()
    assert_clean(at)
    assert any("DASHBOARD_PASSWORD must be set" in text for text in texts(at.error))


# ---------- Pages with an empty database ----------


@pytest.mark.parametrize("page", list(PAGE_TITLES))
def test_every_page_renders_on_an_empty_database(page):
    at = go(new_app(), page)
    assert texts(at.title) == [PAGE_TITLES[page]]


# ---------- Pages with data ----------


@pytest.mark.parametrize("page", list(PAGE_TITLES))
def test_every_page_renders_with_data(db, page):
    seed_business(db)
    at = go(new_app(), page)
    assert texts(at.title) == [PAGE_TITLES[page]]


def test_overview_totals_leave_out_refunds_tests_and_non_usd(db):
    seed_business(db)
    at = go(new_app(), "Overview")
    metrics = {metric.label: metric.value for metric in at.metric}
    assert metrics["Paid units"] == "2"
    assert metrics["Gross"] == "$38.00"
    assert metrics["Net after fees"] == "$33.20"
    assert metrics["Refund rate"] == "33.3%"
    assert metrics["Clicks"] == "40"
    assert metrics["Sales per 100 clicks"] == "5.00"
    assert metrics["Non-USD sales left out"] == "1"


def test_overview_asks_for_more_clicks_before_showing_a_conversion_rate(db):
    seed_business(db)
    db.query(Click).delete()
    db.commit()
    at = go(new_app(), "Overview")
    metrics = {metric.label: metric.value for metric in at.metric}
    assert metrics["Sales per 100 clicks"] == "Not enough clicks"


def test_ebooks_page_shows_outline_pricing_covers_and_prompts(db):
    seed_business(db)
    at = go(new_app(), "Ebooks")
    assert any("AI Advantage for Solopreneurs" in value for value in texts(at.subheader))
    assert any(MIDJOURNEY_PROMPT in code.value for code in at.code)
    assert any("Chapter One" in frame.value.to_string() for frame in at.dataframe)
    assert any("Concept 3" in value for value in texts(at.markdown))


def test_invalid_stored_json_shows_a_warning_instead_of_crashing(db):
    ebook = Ebook(slug="broken-json", title="Broken", stage="written", outline_json="{not json")
    db.add(ebook)
    db.commit()
    at = go(new_app(), "Ebooks")
    assert any("invalid JSON" in value for value in texts(at.warning))


def test_analyst_page_renders_the_three_parts_and_the_snapshot(db):
    seed_business(db)
    at = go(new_app(), "Analyst")
    subheaders = texts(at.subheader)
    for heading in ("What is working", "What is failing", "How to improve sales", "Data to start recording"):
        assert heading in subheaders
    assert any("Two paid USD sales" in value for value in texts(at.markdown))
    assert any("Ask refunded buyers" in frame.value.to_string() for frame in at.dataframe)


def test_analyst_button_queues_an_all_ebooks_report(db, api):
    seed_business(db)
    at = go(new_app(), "Analyst")
    at.number_input(key="analyst_days").set_value(14).run()
    button(at, "Run analyst report").click().run()
    assert_clean(at)
    assert api.calls[-1]["method"] == "POST"
    assert api.calls[-1]["url"].endswith("/analytics")
    assert api.calls[-1]["json"] == {"days": 14}


def test_manual_sale_posts_to_the_api_with_the_fee_entered(db, api):
    seed_business(db)
    api.body = {"id": 42}
    at = go(new_app(), "Sales")
    at.text_input(key="sale_external_id").set_value("KDP-LINE-1")
    at.number_input(key="sale_fee").set_value(3.31)
    button(at, "Save sale").click().run()
    assert_clean(at)
    call = api.calls[-1]
    assert call["url"].endswith("/sales/manual")
    assert call["json"]["platform"] == "kdp"
    assert call["json"]["amount"] == "9.99"
    assert call["json"]["fee"] == "3.31"
    assert call["json"]["sold_on"] == datetime.now(IST).date().isoformat()
    assert call["json"]["external_id"] == "KDP-LINE-1"
    assert any("Saved sale #42" in value for value in texts(at.success))


def test_manual_sale_rejects_a_fee_larger_than_the_amount(db, api):
    seed_business(db)
    at = go(new_app(), "Sales")
    at.number_input(key="sale_fee").set_value(12.0)
    button(at, "Save sale").click().run()
    assert api.calls == []
    assert any("Fees cannot be more than" in value for value in texts(at.warning))


def test_scheduling_refuses_a_time_in_the_past(db, api):
    seeded = seed_business(db)
    post = seeded["post"]
    at = go(new_app(), "Marketing")
    yesterday = datetime.now(IST).date() - timedelta(days=1)
    at.date_input(key=f"day-{post.id}").set_value(yesterday).run()
    button(at, "Schedule post").click().run()
    assert api.calls == []
    assert any("Choose a time in the future" in value for value in texts(at.warning))


def test_image_url_must_be_https(db, api):
    seeded = seed_business(db)
    post = seeded["post"]
    at = go(new_app(), "Marketing")
    at.text_input(key=f"image-{post.id}").set_value("http://cdn.example.test/cover.jpg").run()
    button(at, "Save image URL").click().run()
    assert api.calls == []
    assert any("must start with https://" in value for value in texts(at.warning))


def test_valid_image_url_is_sent_to_the_api(db, api):
    seeded = seed_business(db)
    post = seeded["post"]
    api.body = {"id": post.id}
    at = go(new_app(), "Marketing")
    at.text_input(key=f"image-{post.id}").set_value("https://cdn.example.test/cover.jpg").run()
    button(at, "Save image URL").click().run()
    assert api.calls[-1]["url"].endswith(f"/posts/{post.id}/image")
    assert api.calls[-1]["json"] == {"image_url": "https://cdn.example.test/cover.jpg"}


def test_api_validation_errors_are_shown_as_readable_text(api):
    api.status = 422
    api.body = {"detail": [{"loc": ["body", "topic"], "msg": "String should have at least 3 characters"}]}
    at = go(new_app(), "Run agents")
    next(item for item in at.text_input if item.label == "Topic").input("AI tools")
    next(item for item in at.text_input if item.label == "Audience").input("Solo founders")
    button(at, "Run research for a new ebook").click().run()
    assert any("API error 422: topic: String should have at least 3 characters" in value for value in texts(at.error))


def test_runs_page_shows_the_error_of_the_selected_run(db):
    now = utcnow()
    db.add(AgentRun(task="research", status="failed", error="OpenAI key missing", created_at=now))
    db.commit()
    at = go(new_app(), "Runs")
    assert any("OpenAI key missing" in value for value in texts(at.error))


def test_model_colour_values_cannot_inject_html(db):
    ebook = Ebook(
        slug="colour-test",
        title="Colour test",
        stage="designed",
        cover_concepts_json=json.dumps({"concepts": [{"name": "Unsafe", "colors": ["<img src=x onerror=alert(1)>", "#1B2A41"]}]}),
    )
    db.add(ebook)
    db.commit()
    at = go(new_app(), "Ebooks")
    swatch_markup = [value for value in texts(at.markdown) if "1B2A41" in value]
    assert swatch_markup, "the valid colour should still render"
    assert all("<img" not in value for value in swatch_markup)
    assert any("&lt;img" in value for value in swatch_markup)
