"""Streamlit dashboard. Reads the same database as the API; sends agent work to the API.

Run locally:  streamlit run dashboard.py --server.address 0.0.0.0 --server.port 8501
"""

from __future__ import annotations

import hmac
import json
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx
import pandas as pd
import streamlit as st
from sqlalchemy import select

from config import settings
from db.database import SessionLocal, init_db
from db.models import AgentRun, Ebook, MarketingPost, Product, Sale
from services import sales

st.set_page_config(page_title="Ebook Agent", page_icon=":books:", layout="wide")


def require_login() -> None:
    if not settings.dashboard_password:
        if settings.app_env == "production":
            st.error("DASHBOARD_PASSWORD must be set before the dashboard can run in production.")
            st.stop()
        return
    if st.session_state.get("authenticated"):
        return
    with st.form("login"):
        password = st.text_input("Dashboard password", type="password")
        submitted = st.form_submit_button("Sign in")
    if submitted:
        if hmac.compare_digest(password.encode("utf-8"), settings.dashboard_password.encode("utf-8")):
            st.session_state["authenticated"] = True
            st.rerun()
        st.error("Wrong password")
    st.stop()


def call_api(method: str, path: str, **kwargs) -> dict | list | None:
    headers = {"X-API-Key": settings.api_key}
    try:
        response = httpx.request(method, f"{settings.dashboard_api_url.rstrip('/')}{path}", headers=headers, timeout=30, **kwargs)
    except httpx.HTTPError as exc:
        st.error(f"The API is not reachable at {settings.dashboard_api_url}: {exc}")
        return None
    if response.status_code >= 400:
        detail = response.json().get("detail", response.text) if response.headers.get("content-type", "").startswith("application/json") else response.text
        st.error(f"API error {response.status_code}: {detail}")
        return None
    return response.json()


def ebook_options() -> dict[str, int]:
    with SessionLocal() as db:
        ebooks = db.scalars(select(Ebook).order_by(Ebook.created_at.desc())).all()
        return {f"#{e.id} {e.title} ({e.stage})": e.id for e in ebooks}


def page_overview() -> None:
    st.header("Overview")
    days = st.slider("Window (days)", min_value=7, max_value=365, value=30)
    with SessionLocal() as db:
        metrics = sales.ebook_metrics(db, ebook_id=None, days=days)
    cols = st.columns(4)
    cols[0].metric("Paid units", metrics["sales"]["paid_units"])
    cols[1].metric("Gross (USD)", metrics["sales"]["gross_usd"])
    cols[2].metric("Net (USD)", metrics["sales"]["net_usd"])
    cols[3].metric("Clicks", metrics["traffic"]["clicks"])
    st.caption(metrics["data_note"])
    if metrics["products"]:
        st.subheader("Products")
        st.dataframe(pd.DataFrame(metrics["products"]))
    else:
        st.info("No paid sales in this window yet. Enter KDP royalties under Sales, or wait for webhooks.")


def page_ebooks() -> None:
    st.header("Ebooks")
    with SessionLocal() as db:
        ebooks = db.scalars(select(Ebook).order_by(Ebook.created_at.desc())).all()
    if not ebooks:
        st.info("No ebooks yet. Start one under Run agents.")
        return
    st.dataframe(
        pd.DataFrame(
            [{"id": e.id, "title": e.title, "subtitle": e.subtitle or "", "stage": e.stage, "price_usd": e.list_price_cents / 100} for e in ebooks]
        )
    )
    for ebook in ebooks:
        with st.expander(f"#{ebook.id} {ebook.title}"):
            if ebook.outline_json:
                st.markdown("**Outline**")
                for chapter in json.loads(ebook.outline_json):
                    st.write(f"- {chapter.get('chapter_title', '')}: {chapter.get('goal', '')}")
            if ebook.pricing_json:
                plan = json.loads(ebook.pricing_json)
                st.markdown("**Pricing plan (computed in code)**")
                st.json(plan.get("plan", {}))
            if ebook.cover_concepts_json:
                st.markdown("**Cover concepts**")
                st.json(json.loads(ebook.cover_concepts_json).get("concepts", []))
            if ebook.manuscript_path:
                st.write(f"Manuscript file: `{ebook.manuscript_path}`")


def page_run_agents() -> None:
    st.header("Run agents")
    options = ebook_options()
    with st.form("new_ebook"):
        st.subheader("New ebook")
        topic = st.text_input("Topic", max_chars=300)
        audience = st.text_input("Audience", max_chars=500)
        title = st.text_input("Working title (optional)", max_chars=255)
        if st.form_submit_button("Run research for a new ebook"):
            if not topic or not audience:
                st.warning("Enter a topic and an audience first.")
            else:
                result = call_api("POST", "/research", json={"topic": topic, "audience": audience, "title": title or None})
                if result:
                    st.success(f"Research queued as run {result['run_id']}.")

    st.subheader("Existing ebook")
    if not options:
        st.info("Create an ebook first.")
        return
    label = st.selectbox("Ebook", list(options.keys()))
    ebook_id = options[label]
    cols = st.columns(5)
    tasks = [("Research", "/research"), ("Write", "/write"), ("Design", "/design"), ("Price", "/price"), ("Market", "/market")]
    for col, (name, path) in zip(cols, tasks, strict=False):
        if col.button(name, key=f"btn-{name}"):
            result = call_api("POST", path, json={"ebook_id": ebook_id})
            if result:
                st.success(f"{name} queued as run {result['run_id']}.")

    st.subheader("Data analyst report")
    days = st.number_input("Days to analyse", min_value=1, max_value=365, value=30)
    if st.button("Run analyst report for this ebook"):
        result = call_api("POST", "/analytics", json={"ebook_id": ebook_id, "days": int(days)})
        if result:
            st.success(f"Analyst report queued as run {result['run_id']}.")
    with SessionLocal() as db:
        latest = db.scalar(
            select(AgentRun)
            .where(AgentRun.task == "analytics", AgentRun.ebook_id == ebook_id, AgentRun.status == "succeeded")
            .order_by(AgentRun.id.desc())
            .limit(1)
        )
    if latest and latest.output_json:
        report = json.loads(latest.output_json).get("report", {})
        st.markdown("**What is working**")
        st.write(report.get("what_is_working", []))
        st.markdown("**What is failing**")
        st.write(report.get("what_is_failing", []))
        st.markdown("**How to improve sales**")
        st.json(report.get("how_to_improve_sales", []))
        st.markdown("**Data to start recording**")
        st.write(report.get("data_gaps", []))

    st.subheader("Check a run")
    run_id = st.number_input("Run id", min_value=1, value=1, step=1)
    if st.button("Fetch run"):
        result = call_api("GET", f"/runs/{int(run_id)}")
        if result:
            st.write(f"Status: **{result['status']}**")
            if result.get("error"):
                st.error(result["error"])
            if result.get("output"):
                st.json(result["output"])


def page_sales() -> None:
    st.header("Sales")
    options = ebook_options()
    st.caption("Manual entry is for platforms without webhooks, such as KDP royalty reports. Amounts are in major units.")
    if options:
        with st.form("manual_sale"):
            label = st.selectbox("Ebook", list(options.keys()))
            platform = st.selectbox("Platform", ["kdp", "gumroad", "payhip", "stripe", "other"])
            currency = st.selectbox("Currency", ["usd", "inr", "gbp", "eur"])
            amount = st.number_input("List price received (gross)", min_value=0.01, value=9.99, step=0.01)
            fee = st.number_input("Fees and taxes deducted", min_value=0.0, value=0.0, step=0.01)
            sold_on = st.date_input("Sale date", value=date.today())
            external_id = st.text_input("External id (optional; leave blank to auto-generate)")
            if st.form_submit_button("Save sale"):
                result = call_api(
                    "POST",
                    "/sales/manual",
                    json={
                        "ebook_id": options[label],
                        "platform": platform,
                        "currency": currency,
                        "amount": f"{amount:.2f}",
                        "fee": f"{fee:.2f}",
                        "sold_on": sold_on.isoformat(),
                        "external_id": external_id or None,
                    },
                )
                if result:
                    st.success(f"Saved sale {result['id']}.")
    with SessionLocal() as db:
        rows = db.scalars(select(Sale).order_by(Sale.sold_at.desc()).limit(200)).all()
    if rows:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "id": s.id,
                        "sold_at": s.sold_at,
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
        )
        resend_sale_id = st.number_input("Resend the access link for sale id", min_value=0, value=0, step=1)
        if resend_sale_id and st.button("Resend access link"):
            result = call_api("POST", f"/access/{int(resend_sale_id)}/resend")
            if result:
                st.success(f"Link issued. Email sent: {result['email_sent']}")


def page_marketing() -> None:
    st.header("Marketing")
    with SessionLocal() as db:
        posts = db.scalars(select(MarketingPost).order_by(MarketingPost.created_at.desc()).limit(200)).all()
        products = db.scalars(select(Product).order_by(Product.price_cents)).all()
    if products:
        st.subheader("Products")
        st.dataframe(
            pd.DataFrame(
                [{"id": p.id, "ebook_id": p.ebook_id, "tier": p.tier, "name": p.name, "price_usd": p.price_cents / 100, "platform": p.platform, "checkout_url": p.checkout_url or ""} for p in products]
            )
        )
    if not posts:
        st.info("No posts yet. Run Market for an ebook under Run agents.")
        return
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "id": p.id,
                    "status": p.status,
                    "scheduled_for_utc": p.scheduled_for,
                    "has_image": bool(p.image_url),
                    "caption": p.caption[:120],
                    "error": (p.error or "")[:120],
                }
                for p in posts
            ]
        )
    )
    st.subheader("Attach an image and schedule a post")
    post_id = st.number_input("Post id", min_value=1, value=int(posts[0].id), step=1)
    image_url = st.text_input("Public HTTPS JPEG URL for the image")
    if st.button("Save image URL") and image_url:
        result = call_api("POST", f"/posts/{int(post_id)}/image", json={"image_url": image_url})
        if result:
            st.success("Image saved.")
    scheduled_day = st.date_input("Publish on (your local date)", value=date.today() + timedelta(days=1))
    scheduled_time = st.time_input("Publish at (IST)", value=time(9, 0))
    if st.button("Schedule post"):
        local = datetime.combine(scheduled_day, scheduled_time).replace(tzinfo=ZoneInfo(settings.scheduler_timezone))
        result = call_api("POST", f"/posts/{int(post_id)}/schedule", json={"scheduled_for": local.isoformat()})
        if result:
            st.success(f"Scheduled for {result['scheduled_for']} UTC.")


def page_runs() -> None:
    st.header("Agent runs")
    with SessionLocal() as db:
        runs = db.scalars(select(AgentRun).order_by(AgentRun.id.desc()).limit(200)).all()
    if not runs:
        st.info("No runs yet.")
        return
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "id": r.id,
                    "task": r.task,
                    "ebook_id": r.ebook_id,
                    "status": r.status,
                    "created_at": r.created_at,
                    "finished_at": r.finished_at,
                    "error": (r.error or "")[:160],
                }
                for r in runs
            ]
        )
    )


def main() -> None:
    init_db()
    require_login()
    page = st.sidebar.radio("Page", ["Overview", "Ebooks", "Run agents", "Sales", "Marketing", "Runs"])
    st.sidebar.caption(f"API: {settings.dashboard_api_url}")
    pages = {
        "Overview": page_overview,
        "Ebooks": page_ebooks,
        "Run agents": page_run_agents,
        "Sales": page_sales,
        "Marketing": page_marketing,
        "Runs": page_runs,
    }
    pages[page]()


main()
