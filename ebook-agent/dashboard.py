"""Streamlit dashboard for the ebook agent.

The dashboard reads the same database as the API. Every change (agent runs, manual sales, image URLs, schedules)
goes through the API with the X-API-Key header, so the API stays the single place where work is recorded.

Run locally:  streamlit run dashboard.py --server.address 0.0.0.0 --server.port 8501
"""

from __future__ import annotations

import hmac
import html
import json
import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pandas as pd
import streamlit as st
from sqlalchemy import select
from sqlalchemy.orm import Session

from config import settings, utcnow
from db.database import SessionLocal, init_db
from db.models import AgentRun, Ebook, MarketingPost, Product, Sale
from services import sales

st.set_page_config(page_title="Ebook Agent", page_icon=":books:", layout="wide")

LOCAL_TZ = ZoneInfo(settings.scheduler_timezone)
STAGE_ORDER = ["new", "researched", "written", "designed", "priced", "marketed"]
PLATFORMS = ["kdp", "gumroad", "payhip", "stripe", "other"]
CURRENCIES = ["usd", "inr", "gbp", "eur"]
RUN_STATUSES = ["all", "queued", "running", "succeeded", "failed"]
HEX_COLOR = re.compile(r"#[0-9A-Fa-f]{6}")
PAGES = ["Overview", "Ebooks", "Run agents", "Analyst", "Sales", "Marketing", "Runs"]


# ---------- Formatting and parsing helpers ----------


def usd_text(value: Any) -> str:
    """Format a USD amount that arrives as a string or number. A missing value shows a dash."""
    if value is None or value == "":
        return "—"
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return str(value)


def cents_text(cents: int, currency: str) -> str:
    amount = cents / 100
    return f"${amount:,.2f}" if currency == "usd" else f"{amount:,.2f} {currency.upper()}"


def ist_text(value: datetime | None) -> str:
    """Stored datetimes are naive UTC. Show them in the scheduler's timezone."""
    if value is None:
        return "—"
    return value.replace(tzinfo=timezone.utc).astimezone(LOCAL_TZ).strftime("%d %b %Y, %H:%M")


def today_ist() -> date:
    return datetime.now(LOCAL_TZ).date()


def as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def as_text(item: Any) -> str:
    if isinstance(item, dict):
        return "; ".join(f"{key}: {value}" for key, value in item.items())
    return str(item)


def plain(value: Any) -> Any:
    """Scalars stay as they are. Nested data becomes JSON text so every table cell is a simple value."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def table(items: list[dict]) -> pd.DataFrame:
    return pd.DataFrame([{key: plain(value) for key, value in item.items()} for item in items])


def parse_json(text: str | None, label: str) -> Any:
    """Parse a stored JSON column. A broken value shows a warning instead of crashing the page."""
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        st.warning(f"{label} is stored as invalid JSON, so it cannot be shown. Run that step again to regenerate it.")
        return None


def bullets(items: Any, empty: str) -> None:
    lines = [f"- {as_text(item)}" for item in as_list(items) if as_text(item).strip()]
    if lines:
        st.markdown("\n".join(lines))
    else:
        st.caption(empty)


def color_chip(value: Any) -> str:
    """Show a hex colour as a swatch. The text is HTML-escaped; a swatch is drawn only for a strict #RRGGBB value."""
    text = str(value)
    if not HEX_COLOR.fullmatch(text):
        return f"<code>{html.escape(text)}</code>"
    return (
        f'<span style="display:inline-block;width:0.9em;height:0.9em;border-radius:3px;'
        f'background:{text};border:1px solid rgba(128,128,128,0.5);vertical-align:middle"></span> <code>{text}</code>'
    )


# ---------- Access and API calls ----------


def require_login() -> None:
    if not settings.dashboard_password:
        if settings.app_env == "production":
            st.error("DASHBOARD_PASSWORD must be set before the dashboard can run in production.")
            st.stop()
        return
    if st.session_state.get("authenticated"):
        return
    st.title("Ebook Agent")
    with st.form("login"):
        password = st.text_input("Dashboard password", type="password")
        submitted = st.form_submit_button("Sign in")
    if submitted:
        if hmac.compare_digest(password.encode("utf-8"), settings.dashboard_password.encode("utf-8")):
            st.session_state["authenticated"] = True
            st.rerun()
        st.error("Wrong password")
    st.stop()


def api_error_text(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return (response.text or response.reason_phrase)[:300]
    detail = payload.get("detail", payload) if isinstance(payload, dict) else payload
    if isinstance(detail, list):  # FastAPI validation errors: one entry per field
        parts = []
        for item in detail:
            if isinstance(item, dict):
                location = ".".join(str(part) for part in as_list(item.get("loc"))[1:])
                parts.append(f"{location}: {item.get('msg', '')}".strip(": "))
        return "; ".join(part for part in parts if part) or str(detail)[:300]
    return str(detail)[:300]


def call_api(method: str, path: str, **kwargs: Any) -> Any:
    """Call the API. Shows the error on the page and returns None when the call fails."""
    url = f"{settings.dashboard_api_url.rstrip('/')}{path}"
    try:
        response = httpx.request(method, url, headers={"X-API-Key": settings.api_key}, timeout=30, **kwargs)
    except httpx.HTTPError as exc:
        st.error(
            f"The API is not reachable at {settings.dashboard_api_url} ({type(exc).__name__}). "
            "Check that the api service is running."
        )
        return None
    if response.status_code >= 400:
        st.error(f"API error {response.status_code}: {api_error_text(response)}")
        return None
    try:
        return response.json()
    except ValueError:
        st.error("The API replied, but not with JSON.")
        return None


# ---------- Database reads (read-only) ----------


def load_ebooks() -> list[Ebook]:
    with SessionLocal() as db:
        return list(db.scalars(select(Ebook).order_by(Ebook.created_at.desc(), Ebook.id.desc())).all())


def ebook_picker(ebooks: list[Ebook], *, key: str, label: str = "Ebook", include_all: bool = False) -> Ebook | None:
    """Select box of ebooks. Returns None when 'All ebooks' is chosen (only offered with include_all)."""
    choices: dict[str, Ebook | None] = {}
    if include_all:
        choices["All ebooks"] = None
    for ebook in ebooks:
        choices[f"#{ebook.id} · {ebook.title or 'Untitled'} ({ebook.stage})"] = ebook
    if not choices:
        return None
    return choices[st.selectbox(label, list(choices), key=key)]


def load_runs(*, ebook_id: int | None = None, status: str = "all", limit: int = 200) -> list[AgentRun]:
    with SessionLocal() as db:
        stmt = select(AgentRun)
        if ebook_id is not None:
            stmt = stmt.where(AgentRun.ebook_id == ebook_id)
        if status != "all":
            stmt = stmt.where(AgentRun.status == status)
        return list(db.scalars(stmt.order_by(AgentRun.id.desc()).limit(limit)).all())


def latest_analyst_run(ebook_id: int | None) -> AgentRun | None:
    with SessionLocal() as db:
        scope = AgentRun.ebook_id.is_(None) if ebook_id is None else AgentRun.ebook_id == ebook_id
        stmt = (
            select(AgentRun)
            .where(AgentRun.task == "analytics", AgentRun.status == "succeeded", scope)
            .order_by(AgentRun.id.desc())
            .limit(1)
        )
        return db.scalar(stmt)


def daily_net_usd(db: Session, *, days: int) -> pd.DataFrame:
    """Net USD per IST day for paid, non-test sales in the window. Days without sales are 0."""
    since = utcnow() - timedelta(days=days)
    rows = db.execute(
        select(Sale.sold_at, Sale.net_cents).where(
            Sale.sold_at >= since,
            Sale.is_test.is_(False),
            Sale.is_refunded.is_(False),
            Sale.currency == "usd",
        )
    ).all()
    index = pd.date_range(end=pd.Timestamp(today_ist()), periods=days, freq="D")
    if not rows:
        return pd.DataFrame({"net_usd": 0.0}, index=index)
    frame = pd.DataFrame(rows, columns=["sold_at", "net_cents"])
    local_days = (
        pd.to_datetime(frame["sold_at"])
        .dt.tz_localize("UTC")
        .dt.tz_convert(settings.scheduler_timezone)
        .dt.tz_localize(None)
        .dt.normalize()
    )
    totals = (frame["net_cents"] / 100).groupby(local_days).sum()
    return pd.DataFrame({"net_usd": totals}).reindex(index, fill_value=0.0)


# ---------- Pages ----------


def page_overview() -> None:
    st.title("Overview")
    st.caption("Money is in USD. Test sales and non-USD sales are left out of these totals.")
    days = st.slider("Window (days)", min_value=7, max_value=365, value=30, key="overview_days")
    with SessionLocal() as db:
        metrics = sales.ebook_metrics(db, ebook_id=None, days=days)
        daily = daily_net_usd(db, days=days)
    totals, traffic, marketing = metrics["sales"], metrics["traffic"], metrics["marketing"]

    cols = st.columns(4)
    cols[0].metric("Paid units", totals["paid_units"])
    cols[1].metric("Gross", usd_text(totals["gross_usd"]))
    cols[2].metric("Net after fees", usd_text(totals["net_usd"]))
    cols[3].metric("Refund rate", f"{totals['refund_rate_percent']:.1f}%")
    cols = st.columns(4)
    cols[0].metric("Clicks", traffic["clicks"])
    per_100 = traffic["sales_per_100_clicks"]
    cols[1].metric(
        "Sales per 100 clicks",
        "Not enough clicks" if per_100 is None else f"{per_100:.2f}",
        help=f"Shown once there are at least {traffic['minimum_clicks_for_rate']} clicks in the window.",
    )
    cols[2].metric("Instagram posts published", marketing["instagram_posts_published"])
    cols[3].metric("Non-USD sales left out", totals["non_usd_units_not_included"])
    st.caption(metrics["data_note"])

    st.subheader("Net by day (USD)")
    if daily["net_usd"].sum() == 0:
        st.caption("No paid USD sales in this window yet.")
    st.bar_chart(daily)

    if metrics["products"]:
        st.subheader("Products")
        frame = pd.DataFrame(metrics["products"])  # built directly: platforms stays a list until joined below
        frame["gross_usd"] = frame["gross_usd"].astype(float)
        frame["net_usd"] = frame["net_usd"].astype(float)
        frame["platforms"] = frame["platforms"].apply(lambda names: ", ".join(names))
        st.dataframe(
            frame,
            hide_index=True,
            column_config={
                "product": "Product",
                "tier": "Tier",
                "units": st.column_config.NumberColumn("Units"),
                "gross_usd": st.column_config.NumberColumn("Gross (USD)", format="$%.2f"),
                "net_usd": st.column_config.NumberColumn("Net (USD)", format="$%.2f"),
                "platforms": "Platforms",
            },
        )
    else:
        st.info(
            "No paid sales in this window yet. Gumroad and Stripe sales arrive by webhook. Enter KDP royalties on the Sales page."
        )


def page_ebooks() -> None:
    st.title("Ebooks")
    ebooks = load_ebooks()
    if not ebooks:
        st.info("No ebooks yet. Start one under Run agents.")
        return
    st.dataframe(
        table(
            [
                {
                    "id": e.id,
                    "title": e.title,
                    "subtitle": e.subtitle or "",
                    "stage": e.stage,
                    "list price (USD)": e.list_price_cents / 100,
                    "created (IST)": ist_text(e.created_at),
                }
                for e in ebooks
            ]
        ),
        hide_index=True,
        column_config={"list price (USD)": st.column_config.NumberColumn(format="$%.2f")},
    )
    ebook = ebook_picker(ebooks, key="ebooks_pick")
    if ebook is None:
        return

    st.subheader(f"#{ebook.id} · {ebook.title or 'Untitled'}")
    if ebook.subtitle:
        st.caption(ebook.subtitle)
    st.write(f"**Topic:** {ebook.topic or '—'}  \n**Audience:** {ebook.audience or '—'}")
    done = STAGE_ORDER.index(ebook.stage) if ebook.stage in STAGE_ORDER else 0
    st.progress(done / (len(STAGE_ORDER) - 1), text=f"{done} of {len(STAGE_ORDER) - 1} steps done. Stage: {ebook.stage}")

    outline_tab, pricing_tab, covers_tab, research_tab, files_tab = st.tabs(["Outline", "Pricing", "Covers", "Research", "Files"])
    with outline_tab:
        render_outline(ebook)
    with pricing_tab:
        render_pricing(ebook)
    with covers_tab:
        render_covers(ebook)
    with research_tab:
        render_research(ebook)
    with files_tab:
        render_files(ebook)


def render_outline(ebook: Ebook) -> None:
    chapters = [c for c in as_list(parse_json(ebook.outline_json, "Outline")) if isinstance(c, dict)]
    if not chapters:
        st.info("No outline yet. Run Research for this ebook.")
        return
    st.dataframe(
        table(
            [
                {
                    "#": number,
                    "chapter": chapter.get("chapter_title", ""),
                    "goal": chapter.get("goal", ""),
                    "key points": "; ".join(str(point) for point in as_list(chapter.get("key_points"))),
                }
                for number, chapter in enumerate(chapters, start=1)
            ]
        ),
        hide_index=True,
    )


def render_pricing(ebook: Ebook) -> None:
    payload = as_dict(parse_json(ebook.pricing_json, "Pricing"))
    if not payload:
        st.info("No pricing yet. Run Price for this ebook.")
        return
    plan = as_dict(payload.get("plan"))
    narrative = as_dict(payload.get("narrative"))
    for error in as_list(plan.get("errors")):
        st.error(str(error))
    for warning in as_list(plan.get("warnings")):
        st.warning(str(warning))
    st.caption(
        f"Distribution: {plan.get('distribution_strategy', 'not recorded')}. "
        "Fees and royalties are computed in code. The PricingAgent writes the narrative only."
    )

    kdp = as_dict(plan.get("basic_kdp_us"))
    discount = as_dict(plan.get("bundle_discount"))
    cols = st.columns(4)
    cols[0].metric("Basic royalty (KDP US)", usd_text(kdp.get("royalty_usd")))
    cols[1].metric("Break-even file size", f"{kdp.get('break_even_file_size_mb', '—')} MB")
    cols[2].metric("Bundle price", usd_text(as_dict(plan.get("bundle")).get("price_usd")))
    cols[3].metric("Saving vs Basic + Pro", usd_text(discount.get("saving_usd")))

    st.subheader("Fees and net on each platform (Pro and Bundle)")
    rows = []
    for label, key in (("Pro toolkit", "pro_toolkit"), ("Bundle", "bundle")):
        block = as_dict(plan.get(key))
        rows.append(
            {
                "tier": label,
                "price (USD)": block.get("price_usd"),
                "Gumroad fee": block.get("direct_fee_usd"),
                "Gumroad net": block.get("direct_net_usd"),
                "Gumroad Discover fee": block.get("discover_fee_usd"),
                "Gumroad Discover net": block.get("discover_net_usd"),
                "Payhip Free fee": block.get("payhip_free_fee_usd"),
                "Payhip Free net, before processor": block.get("payhip_free_net_before_processor_usd"),
            }
        )
    st.dataframe(table(rows), hide_index=True)

    tiers = [tier for tier in as_list(narrative.get("tiers")) if isinstance(tier, dict)]
    if tiers:
        st.subheader("Price ladder")
        st.dataframe(table(tiers), hide_index=True)
    if narrative.get("rationale"):
        st.write(str(narrative["rationale"]))
    with st.expander("Full computed plan (JSON)"):
        st.json(plan)


def render_covers(ebook: Ebook) -> None:
    payload = as_dict(parse_json(ebook.cover_concepts_json, "Cover concepts"))
    concepts = [c for c in as_list(payload.get("concepts")) if isinstance(c, dict)]
    if not concepts:
        st.info("No cover concepts yet. Run Design for this ebook.")
        return
    concepts = concepts[:3]
    for column, number, concept in zip(st.columns(len(concepts)), range(1, len(concepts) + 1), concepts, strict=True):
        with column:
            st.markdown(f"**{number}. {concept.get('name') or 'Untitled concept'}**")
            colors = as_list(concept.get("colors"))
            if colors:
                st.markdown(" &nbsp; ".join(color_chip(color) for color in colors), unsafe_allow_html=True)
            st.write(str(concept.get("layout") or ""))
            fonts = as_dict(concept.get("fonts"))
            st.caption(f"Headline font: {fonts.get('headline', '—')} · Body font: {fonts.get('body', '—')}")
            st.markdown("**Midjourney prompt**")
            st.code(str(concept.get("midjourney_prompt") or ""), language=None)
            st.markdown("**DALL-E prompt**")
            st.code(str(concept.get("dalle_prompt") or ""), language=None)
            st.markdown("**Finish in Canva**")
            for step_number, step in enumerate(as_list(concept.get("canva_steps")), start=1):
                st.write(f"{step_number}. {step}")
            text = as_dict(concept.get("text_for_canva"))
            st.caption(f"Title text: {text.get('title', '—')}  \nSubtitle text: {text.get('subtitle', '—')}")


def render_research(ebook: Ebook) -> None:
    research = as_dict(parse_json(ebook.research_json, "Research"))
    if not research:
        st.info("No research yet. Run Research for this ebook.")
        return
    st.markdown("**Title options**")
    bullets(research.get("title_options"), "None recorded.")
    st.markdown("**Claims to verify before publishing**")
    bullets(research.get("claims_to_verify"), "None recorded.")
    st.caption("Check every claim against a source you can cite before the book is published.")


def render_files(ebook: Ebook) -> None:
    st.write(f"**Slug:** `{ebook.slug}`")
    st.write(f"**Manuscript:** `{ebook.manuscript_path or 'not written yet'}`")
    st.write(f"**Cover image:** `{ebook.cover_image_path or 'not set'}`")
    if ebook.notion_page_url and ebook.notion_page_url.startswith("https://"):
        st.link_button("Open the Notion page", ebook.notion_page_url)
    with SessionLocal() as db:
        products = list(db.scalars(select(Product).where(Product.ebook_id == ebook.id).order_by(Product.price_cents)).all())
    if not products:
        st.info("No products yet. Create one with POST /products.")
        return
    st.dataframe(
        table(
            [
                {
                    "id": p.id,
                    "name": p.name,
                    "tier": p.tier,
                    "price (USD)": p.price_cents / 100,
                    "platform": p.platform,
                    "active": p.active,
                    "checkout URL": p.checkout_url or "",
                }
                for p in products
            ]
        ),
        hide_index=True,
        column_config={"price (USD)": st.column_config.NumberColumn(format="$%.2f")},
    )


def page_run_agents() -> None:
    st.title("Run agents")
    ebooks = load_ebooks()
    st.subheader("Start a new ebook")
    st.caption("Research uses the topic and audience you enter. Its output is a draft to check, not a finished book.")
    with st.form("new_ebook"):
        topic = st.text_input("Topic", max_chars=300, help="At least 3 characters.")
        audience = st.text_input("Audience", max_chars=500, help="Who the book is for. At least 3 characters.")
        title = st.text_input("Working title (optional)", max_chars=255)
        submitted = st.form_submit_button("Run research for a new ebook")
    if submitted:
        if len(topic.strip()) < 3 or len(audience.strip()) < 3:
            st.warning("Enter a topic and an audience of at least 3 characters each.")
        else:
            result = call_api(
                "POST",
                "/research",
                json={"topic": topic.strip(), "audience": audience.strip(), "title": title.strip() or None},
            )
            if result is not None:
                st.success(f"Research queued as run {result['run_id']}. Check Agent runs for the result.")

    st.subheader("Work on an existing ebook")
    if not ebooks:
        st.info("Create an ebook first.")
        return
    ebook = ebook_picker(ebooks, key="run_pick")
    if ebook is None:
        return
    st.caption(f"Current stage: {ebook.stage}. Each step runs in the background.")
    steps = [("Research", "/research"), ("Write", "/write"), ("Design", "/design"), ("Price", "/price"), ("Market", "/market")]
    for column, (name, path) in zip(st.columns(len(steps)), steps, strict=True):
        if column.button(name, key=f"run-{name}"):
            result = call_api("POST", path, json={"ebook_id": ebook.id})
            if result is not None:
                st.success(f"{name} queued as run {result['run_id']}.")

    st.subheader("Recent runs for this ebook")
    titles = {e.id: e.title for e in ebooks}
    runs = load_runs(ebook_id=ebook.id, limit=8)
    if runs:
        st.dataframe(runs_frame(runs, titles), hide_index=True)
    else:
        st.caption("No runs for this ebook yet.")


def page_analyst() -> None:
    st.title("Analyst")
    st.caption(
        "The analyst reads one snapshot of your numbers and reports what is working, what is failing, and how to improve "
        "sales. It uses only the numbers in that snapshot. Enter sales on the Sales page first."
    )
    ebooks = load_ebooks()
    scope = ebook_picker(ebooks, key="analyst_scope", include_all=True)
    days = st.number_input("Days to analyse", min_value=1, max_value=365, value=30, step=1, key="analyst_days")
    if st.button("Run analyst report", key="run_analyst"):
        body: dict[str, Any] = {"days": int(days)}
        if scope is not None:
            body["ebook_id"] = scope.id
        result = call_api("POST", "/analytics", json=body)
        if result is not None:
            st.success(f"Analyst report queued as run {result['run_id']}. Refresh this page when it finishes.")

    run = latest_analyst_run(scope.id if scope is not None else None)
    if run is None:
        st.info("No analyst report yet for this scope. Run one above.")
        return
    output = as_dict(parse_json(run.output_json, "Analyst report"))
    report = as_dict(output.get("report"))
    snapshot = as_dict(output.get("snapshot"))
    st.caption(
        f"From run #{run.id}, finished {ist_text(run.finished_at)} IST. "
        f"Window: {snapshot.get('window_days', '—')} days. {snapshot.get('data_note', '')}"
    )

    left, right = st.columns(2)
    with left:
        st.subheader("What is working")
        bullets(report.get("what_is_working"), "Nothing reported.")
    with right:
        st.subheader("What is failing")
        bullets(report.get("what_is_failing"), "Nothing reported.")
    st.subheader("How to improve sales")
    actions = [a for a in as_list(report.get("how_to_improve_sales")) if isinstance(a, dict)]
    if actions:
        st.dataframe(
            table(
                [
                    {
                        "action": action.get("action", ""),
                        "reason": action.get("reason", ""),
                        "metric to watch": action.get("metric_to_watch", ""),
                    }
                    for action in actions
                ]
            ),
            hide_index=True,
        )
    else:
        st.caption("No actions reported.")
    st.subheader("Data to start recording")
    bullets(report.get("data_gaps"), "No gaps reported.")
    with st.expander("Numbers the analyst used"):
        st.json(snapshot)


def sales_frame(rows: list[Sale], titles: dict[int, str]) -> pd.DataFrame:
    return table(
        [
            {
                "id": s.id,
                "sold (IST)": ist_text(s.sold_at),
                "ebook": titles.get(s.ebook_id, "—"),
                "platform": s.platform,
                "amount": s.amount_cents / 100,
                "fee": s.fee_cents / 100,
                "net": s.net_cents / 100,
                "currency": s.currency,
                "refunded": s.is_refunded,
                "test": s.is_test,
                "buyer": s.buyer_email or "",
            }
            for s in rows
        ]
    )


def render_manual_sale_form(ebooks: list[Ebook]) -> None:
    default_price = ebooks[0].list_price_cents / 100
    with st.form("manual_sale"):
        st.subheader("Add a sale by hand")
        ebook = ebook_picker(ebooks, key="sale_ebook")
        cols = st.columns(3)
        platform = cols[0].selectbox("Platform", PLATFORMS, key="sale_platform")
        currency = cols[1].selectbox("Currency", CURRENCIES, key="sale_currency")
        sold_on = cols[2].date_input("Sale date (IST)", value=today_ist(), key="sale_date")
        cols = st.columns(3)
        amount = cols[0].number_input(
            "Amount paid (list price)", min_value=0.01, value=default_price, step=0.01, format="%.2f", key="sale_amount"
        )
        fee = cols[1].number_input(
            "Fees and deductions",
            min_value=0.0,
            value=0.0,
            step=0.01,
            format="%.2f",
            key="sale_fee",
            help="For KDP: list price minus your royalty. Example: $9.99 list with a $6.68 royalty (3 MB) gives a fee of $3.31.",
        )
        external_id = cols[2].text_input(
            "External id (optional)", max_chars=128, key="sale_external_id", help="Leave blank to generate one."
        )
        submitted = st.form_submit_button("Save sale")
    if not submitted or ebook is None:
        return
    if fee > amount:
        st.warning("Fees cannot be more than the amount paid. Check the royalty and fee figures.")
        return
    result = call_api(
        "POST",
        "/sales/manual",
        json={
            "ebook_id": ebook.id,
            "platform": platform,
            "currency": currency,
            "amount": f"{amount:.2f}",
            "fee": f"{fee:.2f}",
            "sold_on": sold_on.isoformat(),
            "external_id": external_id.strip() or None,
        },
    )
    if result is not None:
        st.success(f"Saved sale #{result['id']}. It appears in the table below.")


def render_resend(rows: list[Sale]) -> None:
    reachable = [s for s in rows if s.buyer_email]
    if not reachable:
        return
    st.subheader("Resend an access link")
    st.caption("Only sales with a buyer email can receive a link. The buyer gets a new access link by email.")
    choices = {f"#{s.id} · {s.platform} · {s.buyer_email} · {cents_text(s.amount_cents, s.currency)}": s.id for s in reachable}
    choice = st.selectbox("Sale", list(choices), key="resend_sale")
    if st.button("Resend access link", key="resend_button"):
        result = call_api("POST", f"/access/{choices[choice]}/resend")
        if result is not None:
            st.success(f"Link issued. Email sent: {result.get('email_sent')}")


def page_sales() -> None:
    st.title("Sales")
    st.caption(
        "Gumroad and Stripe sales arrive by webhook. Enter the rest here, such as KDP royalty lines. "
        "Amounts are in major units (for example 9.99)."
    )
    ebooks = load_ebooks()
    if ebooks:
        render_manual_sale_form(ebooks)
    else:
        st.info("Create an ebook before entering sales.")
    with SessionLocal() as db:
        rows = list(db.scalars(select(Sale).order_by(Sale.sold_at.desc(), Sale.id.desc()).limit(200)).all())
    if rows:
        titles = {e.id: e.title for e in ebooks}
        st.subheader("Recent sales")
        st.dataframe(
            sales_frame(rows, titles),
            hide_index=True,
            column_config={
                "amount": st.column_config.NumberColumn(format="%.2f"),
                "fee": st.column_config.NumberColumn(format="%.2f"),
                "net": st.column_config.NumberColumn(format="%.2f"),
            },
        )
        render_resend(rows)
    else:
        st.info("No sales recorded yet.")


def posts_frame(posts: list[MarketingPost], titles: dict[int, str]) -> pd.DataFrame:
    return table(
        [
            {
                "id": p.id,
                "ebook": titles.get(p.ebook_id, "—"),
                "status": p.status,
                "scheduled (IST)": ist_text(p.scheduled_for),
                "published (IST)": ist_text(p.published_at),
                "image": "yes" if p.image_url else "no",
                "caption": p.caption[:120],
                "error": (p.error or "")[:160],
            }
            for p in posts
        ]
    )


def page_marketing() -> None:
    st.title("Marketing")
    st.caption(
        "A post stays a draft until it has a public HTTPS image URL and a publish time. "
        f"Times are in {settings.scheduler_timezone}."
    )
    with SessionLocal() as db:
        posts = list(
            db.scalars(select(MarketingPost).order_by(MarketingPost.created_at.desc(), MarketingPost.id.desc()).limit(200)).all()
        )
        products = list(db.scalars(select(Product).order_by(Product.price_cents)).all())
    ebooks = load_ebooks()
    titles = {e.id: e.title for e in ebooks}
    if products:
        st.subheader("Products")
        st.dataframe(
            table(
                [
                    {
                        "id": p.id,
                        "ebook": titles.get(p.ebook_id, "—"),
                        "tier": p.tier,
                        "name": p.name,
                        "price (USD)": p.price_cents / 100,
                        "platform": p.platform,
                        "checkout URL": p.checkout_url or "",
                    }
                    for p in products
                ]
            ),
            hide_index=True,
            column_config={"price (USD)": st.column_config.NumberColumn(format="$%.2f")},
        )
    if not posts:
        st.info("No posts yet. Run Market for an ebook under Run agents.")
        return
    st.subheader("Posts")
    st.dataframe(posts_frame(posts, titles), hide_index=True)

    st.subheader("Review, add an image, and schedule")
    choices = {f"#{p.id} · {p.status} · {p.caption[:70]}": p for p in posts}
    post = choices[st.selectbox("Post", list(choices), key="post_pick")]
    with st.container(border=True):
        st.markdown(post.caption)
        if post.hashtags:
            st.caption(post.hashtags)
        if post.error:
            st.error(post.error)
    image_url = st.text_input("Public HTTPS image URL (JPEG)", value=post.image_url or "", max_chars=512, key=f"image-{post.id}")
    if st.button("Save image URL", key=f"save-image-{post.id}"):
        if not image_url.startswith("https://"):
            st.warning("The image URL must start with https://")
        else:
            result = call_api("POST", f"/posts/{post.id}/image", json={"image_url": image_url})
            if result is not None:
                st.success("Image saved.")
    col_day, col_time = st.columns(2)
    publish_day = col_day.date_input("Publish on (IST date)", value=today_ist() + timedelta(days=1), key=f"day-{post.id}")
    publish_time = col_time.time_input("Publish at (IST)", value=time(9, 0), key=f"time-{post.id}")
    if st.button("Schedule post", key=f"schedule-{post.id}"):
        when = datetime.combine(publish_day, publish_time, tzinfo=LOCAL_TZ)
        if when <= datetime.now(LOCAL_TZ):
            st.warning("Choose a time in the future.")
        else:
            result = call_api("POST", f"/posts/{post.id}/schedule", json={"scheduled_for": when.isoformat()})
            if result is not None:
                st.success(f"Scheduled for {when:%d %b %Y, %H:%M} IST.")


def runs_frame(runs: list[AgentRun], titles: dict[int, str]) -> pd.DataFrame:
    return table(
        [
            {
                "id": r.id,
                "task": r.task,
                "ebook": titles.get(r.ebook_id, "—") if r.ebook_id else "—",
                "status": r.status,
                "created (IST)": ist_text(r.created_at),
                "finished (IST)": ist_text(r.finished_at),
                "error": (r.error or "")[:160],
            }
            for r in runs
        ]
    )


def page_runs() -> None:
    st.title("Agent runs")
    status = st.selectbox("Status", RUN_STATUSES, key="runs_status")
    runs = load_runs(status=status)
    if not runs:
        st.info("No runs match this filter.")
        return
    titles = {e.id: e.title for e in load_ebooks()}
    st.dataframe(runs_frame(runs, titles), hide_index=True)

    choices = {f"#{r.id} · {r.task} · {r.status} · {ist_text(r.created_at)}": r for r in runs}
    run = choices[st.selectbox("Run details", list(choices), key="runs_pick")]
    if run.error:
        st.error(run.error)
    if run.status in ("queued", "running"):
        st.info("This run has not finished. Refresh the page to check again.")
    if run.input_json:
        with st.expander("Input"):
            st.json(parse_json(run.input_json, "Run input") or {})
    if run.status == "succeeded" and run.output_json:
        with st.expander("Output", expanded=True):
            st.json(parse_json(run.output_json, "Run output") or {})


PAGE_RENDERERS = {
    "Overview": page_overview,
    "Ebooks": page_ebooks,
    "Run agents": page_run_agents,
    "Analyst": page_analyst,
    "Sales": page_sales,
    "Marketing": page_marketing,
    "Runs": page_runs,
}


def main() -> None:
    init_db()
    require_login()
    with st.sidebar:
        page = st.radio("Page", PAGES, key="page", label_visibility="collapsed")
        st.caption(f"API: {settings.dashboard_api_url}")
        st.caption(f"Times in {settings.scheduler_timezone}")
        if settings.dashboard_password and st.button("Sign out", key="sign_out"):
            st.session_state.pop("authenticated", None)
            st.rerun()
    PAGE_RENDERERS[page]()


main()
