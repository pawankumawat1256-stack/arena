"""Agent output validation and the offline pipeline run. The model is replaced by a fake; no network calls."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from langchain_core.runnables import RunnableLambda

from config import settings
from core import agent as agent_module
from core import pipeline
from core.agent import (
    AgentError,
    AnalyticsAgent,
    DesignerAgent,
    MarketingAgent,
    PricingAgent,
    ResearchAgent,
    WriterAgent,
)
from core.llm import LLMConfigError, get_llm
from db.database import SessionLocal
from db.models import AgentRun, Ebook
from tests import support


def test_research_agent_returns_titles_and_outline():
    result = ResearchAgent(llm=support.fake_llm(support.RESEARCH)).run(topic="AI for solopreneurs", audience="solo founders")
    assert len(result["title_options"]) == 3
    assert len(result["outline"]) == 8


def test_research_agent_rejects_empty_outline():
    bad = {**support.RESEARCH, "outline": []}
    with pytest.raises(AgentError, match="no titles or no outline"):
        ResearchAgent(llm=support.fake_llm(bad)).run(topic="AI for solopreneurs", audience="solo founders")


def test_agent_wraps_model_failures_in_agent_error():
    def boom(_prompt_value):
        raise RuntimeError("rate limited")

    with pytest.raises(AgentError, match="rate limited"):
        ResearchAgent(llm=RunnableLambda(boom)).run(topic="AI for solopreneurs", audience="solo founders")


def test_designer_agent_returns_three_concepts():
    result = DesignerAgent(llm=support.fake_llm(support.DESIGN)).run(book_title="Book", subtitle="Sub", audience="Founders")
    assert len(result["concepts"]) == 3


def test_designer_agent_rejects_invalid_hex_colors():
    bad = copy.deepcopy(support.DESIGN)
    bad["concepts"][0]["colors"] = ["red", "#fff", "#12345G"]
    with pytest.raises(AgentError, match="hex"):
        DesignerAgent(llm=support.fake_llm(bad)).run(book_title="Book", subtitle="Sub", audience="Founders")


def test_designer_agent_requires_no_text_in_image_prompts():
    bad = copy.deepcopy(support.DESIGN)
    bad["concepts"][1]["midjourney_prompt"] = "A calm desk --ar 5:8"
    with pytest.raises(AgentError, match="no text"):
        DesignerAgent(llm=support.fake_llm(bad)).run(book_title="Book", subtitle="Sub", audience="Founders")


def test_marketing_agent_applies_fixed_send_schedule():
    result = MarketingAgent(llm=support.fake_llm(support.MARKETING)).run(
        book_title="Book", subtitle="Sub", audience="Founders", offer="Basic", post_count=3
    )
    assert [email["delay_days"] for email in result["email_sequences"]["launch"]] == [0, 2, 4, 7, 10]
    assert [email["delay_days"] for email in result["email_sequences"]["onboarding"]] == [0, 1, 3]
    assert len(result["social_posts"]) == 3


def test_marketing_agent_rejects_short_launch_sequence():
    bad = copy.deepcopy(support.MARKETING)
    bad["email_sequences"]["launch"] = bad["email_sequences"]["launch"][:4]
    with pytest.raises(AgentError, match="exactly 5"):
        MarketingAgent(llm=support.fake_llm(bad)).run(book_title="Book", subtitle="Sub", audience="Founders", offer="Basic", post_count=3)


def test_pricing_agent_requires_exactly_three_tiers():
    bad = {**support.PRICING, "tiers": support.PRICING["tiers"][:2]}
    with pytest.raises(AgentError, match="exactly 3"):
        PricingAgent(llm=support.fake_llm(bad)).run(book_title="Book", audience="Founders", plan={"basic": "9.99"})


def test_writer_agent_writes_each_chapter_as_markdown():
    outline = [
        {"chapter_title": "Start Here", "goal": "Orient", "key_points": ["One"]},
        {"chapter_title": "Next Steps", "goal": "Act", "key_points": ["Two"]},
    ]
    result = WriterAgent(llm=support.fake_llm(support.CHAPTER_TEXT)).write_book(
        book_title="Book", subtitle="Sub", audience="Founders", outline=outline
    )
    assert result["chapters"] == 2
    assert result["markdown"].count("## ") >= 2
    assert result["words"] > 100


def test_analytics_agent_returns_required_sections():
    report = AnalyticsAgent(llm=support.fake_llm(support.ANALYTICS)).run(snapshot={"sales": {"paid_units": 1}})
    assert report["what_is_working"] and report["how_to_improve_sales"]


def test_get_llm_requires_an_api_key(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "")
    with pytest.raises(LLMConfigError):
        get_llm()


def test_get_llm_builds_a_chat_model_offline(monkeypatch):
    from langchain_openai import ChatOpenAI

    monkeypatch.setattr(settings, "openai_api_key", "sk-test-offline")
    assert isinstance(get_llm(json_mode=True), ChatOpenAI)


def test_full_pipeline_runs_offline(monkeypatch):
    monkeypatch.setattr(agent_module, "get_llm", lambda **_kwargs: support.fake_llm())
    with SessionLocal() as db:
        ebook = pipeline.create_ebook(db, topic="AI for solopreneurs", audience="solo founders")
        db.commit()
        payload = {"topic": "AI for solopreneurs", "audience": "solo founders", "title": None, "subtitle": None, "sources": []}
        run = AgentRun(task="pipeline", ebook_id=ebook.id, status="queued", input_json=json.dumps(payload))
        db.add(run)
        db.commit()
        run_id, ebook_id = run.id, ebook.id

    pipeline.execute_run(run_id)

    with SessionLocal() as db:
        run = db.get(AgentRun, run_id)
        assert run.status == "succeeded", run.error
        output = json.loads(run.output_json)
        assert set(output) == {"research", "write", "design", "price", "market"}
        assert output["market"]["posts_created"] == 3
        ebook = db.get(Ebook, ebook_id)
        assert ebook.stage == "marketed"
        assert ebook.title == "AI Advantage for Solopreneurs"
        assert Path(ebook.manuscript_path).is_file()
