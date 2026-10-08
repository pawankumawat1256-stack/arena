"""Shared test setup. Environment values are set before the application's settings module is imported."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="ebook-agent-tests-"))
os.environ.update(
    {
        "APP_ENV": "test",
        "LOG_LEVEL": "WARNING",
        "DATA_DIR": str(_TMP / "data"),
        "DATABASE_URL": f"sqlite:///{(_TMP / 'test.db').as_posix()}",
        "APP_SECRET_KEY": "test-secret-key-with-more-than-thirty-two-characters",
        "API_KEY": "test-api-key",
        "PUBLIC_BASE_URL": "https://agent.example.test",
        "OPENAI_API_KEY": "test-openai-key",
        "GUMROAD_ACCESS_TOKEN": "test-gumroad-access",
        "GUMROAD_WEBHOOK_TOKEN": "test-gumroad-webhook",
        "BUSINESS_POSTAL_ADDRESS": "Test Studio, 1 Example Road, Jaipur, Rajasthan 302001, India",
        "INSTAGRAM_ACCESS_TOKEN": "",
        "INSTAGRAM_USER_ID": "",
        "NOTION_API_KEY": "",
        "NOTION_PARENT_PAGE_ID": "",
        "STRIPE_SECRET_KEY": "",
        "STRIPE_WEBHOOK_SECRET": "",
        "DASHBOARD_PASSWORD": "",
    }
)

import pytest  # noqa: E402

from db.database import Base, SessionLocal, engine, init_db  # noqa: E402
from db.models import Ebook, Product  # noqa: E402
from tests import support  # noqa: E402

SEQUENCES = {
    "launch": [
        {"subject": f"Launch {i}", "preview_text": "Preview", "body_text": "Launch body text. " * 15, "delay_days": d}
        for i, d in enumerate([0, 2, 4, 7, 10], start=1)
    ],
    "onboarding": [
        {"subject": f"Welcome {i}", "preview_text": "Preview", "body_text": "Welcome body text. " * 15, "delay_days": d}
        for i, d in enumerate([0, 1, 3], start=1)
    ],
}


@pytest.fixture(scope="session", autouse=True)
def _schema():
    init_db()
    yield
    engine.dispose()
    shutil.rmtree(_TMP, ignore_errors=True)


@pytest.fixture(autouse=True)
def clean_tables():
    init_db()
    with engine.begin() as connection:
        for table in reversed(Base.metadata.sorted_tables):
            connection.execute(table.delete())
    yield


@pytest.fixture()
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def ebook(db):
    record = Ebook(
        slug="test-book",
        title="Test Book",
        subtitle="A Subtitle",
        topic="AI for solopreneurs",
        audience="solo founders",
        stage="marketed",
        list_price_cents=999,
        outline_json=json.dumps(support.RESEARCH["outline"]),
        email_sequences_json=json.dumps(SEQUENCES),
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


@pytest.fixture()
def product(db, ebook):
    record = Product(
        ebook_id=ebook.id,
        tier="basic",
        name="Test Book (PDF)",
        price_cents=999,
        platform="gumroad",
        external_product_id="gum-prod-1",
        permalink="basic",
        active=True,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


@pytest.fixture()
def fake_gmail(monkeypatch):
    """Records every Gmail draft-and-send call instead of contacting Google."""
    sent: list[dict] = []

    def fake_send(*, to, subject, text, headers=None):
        sent.append({"to": to, "subject": subject, "text": text, "headers": headers or {}})
        return f"msg-{len(sent)}"

    monkeypatch.setattr("integrations.gmail_client.create_draft_and_send", fake_send)
    return sent


@pytest.fixture()
def gumroad_sales(monkeypatch):
    """A dict of sale id to sale object. The fake GET /v2/sales/:id reads from it."""
    from integrations.gumroad import GumroadError

    store: dict[str, dict] = {}

    def fake_get_sale(sale_id: str) -> dict:
        if sale_id not in store:
            raise GumroadError(f"Sale {sale_id} was not found")
        return dict(store[sale_id])

    monkeypatch.setattr("integrations.gumroad.get_sale", fake_get_sale)
    return store
