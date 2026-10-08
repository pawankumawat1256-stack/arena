"""Canned model outputs and a fake chat model. Tests run fully offline: no OpenAI, Gmail, Gumroad, or Instagram calls."""

from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

RESEARCH: dict[str, Any] = {
    "title_options": [
        {"title": "AI Advantage for Solopreneurs", "subtitle": "Save 10 Hours a Week"},
        {"title": "The Solo Founder's AI Toolkit", "subtitle": "Practical Systems"},
        {"title": "Work Smarter With AI", "subtitle": "A Field Guide"},
    ],
    "audience_profile": "Solo founders with fewer than five staff.",
    "pain_points": ["Too many tasks", "No time for marketing", "Tool overload", "Unclear pricing", "Slow content"],
    "competitor_gaps": ["No budget-first guide", "Few worked examples", "No weekly routine"],
    "keyword_ideas": [f"ai tools keyword {i}" for i in range(10)],
    "outline": [
        {"chapter_title": f"Chapter {i}", "goal": f"Goal {i}", "key_points": ["Point one", "Point two", "Point three"]}
        for i in range(1, 9)
    ],
    "claims_to_verify": ["Time savings figure needs a source"],
}

CHAPTER_TEXT = "## Chapter\n\n" + "A practical paragraph with concrete steps for the reader. " * 30

DESIGN: dict[str, Any] = {
    "concepts": [
        {
            "name": f"Concept {i}",
            "layout": "Artwork fills the top 60 percent; title block sits in the lower third.",
            "colors": ["#1B2A41", "#F4F1EA", "#E07A5F"],
            "fonts": {"headline": "Montserrat", "body": "Inter"},
            "midjourney_prompt": "A calm desk with a laptop and a notebook, soft light, no text --ar 5:8 --no text, letters, words, logo",
            "dalle_prompt": "Portrait illustration of a calm desk with a laptop, soft light, no text",
            "canva_steps": ["Create a 1600 x 2560 design", "Place the artwork", "Add the title", "Export as JPEG"],
            "text_for_canva": {"title": "AI Advantage for Solopreneurs", "subtitle": "Save 10 Hours a Week"},
        }
        for i in range(1, 4)
    ]
}

PRICING: dict[str, Any] = {
    "tiers": [
        {"tier_id": "basic", "name": "Basic", "description": "The book.", "includes": ["Book PDF"], "platform": "kdp"},
        {"tier_id": "pro", "name": "Pro", "description": "The toolkit.", "includes": ["Toolkit"], "platform": "gumroad"},
        {"tier_id": "bundle", "name": "Bundle", "description": "Everything.", "includes": ["Book", "Toolkit"], "platform": "gumroad"},
    ],
    "rationale": "The ladder starts at the book price and adds the toolkit for a discounted bundle.",
}

MARKETING: dict[str, Any] = {
    "social_posts": [
        {"caption": "A hook about saving time with AI. " * 6, "hashtags": ["ai", "solofounder"], "image_brief": "A tidy desk"}
        for _ in range(3)
    ],
    "email_sequences": {
        "launch": [
            {"subject": f"Launch email {i}", "preview_text": "Preview", "body_text": "Useful content for readers. " * 20}
            for i in range(1, 6)
        ],
        "onboarding": [
            {"subject": f"Welcome email {i}", "preview_text": "Preview", "body_text": "Getting started content. " * 20}
            for i in range(1, 4)
        ],
    },
}

ANALYTICS: dict[str, Any] = {
    "what_is_working": ["Paid units are 1 in the window."],
    "what_is_failing": ["Refund rate is above zero."],
    "how_to_improve_sales": [{"action": "Add a bundle page", "reason": "Sales are low", "metric_to_watch": "paid units"}],
    "data_gaps": ["Clicks by channel"],
}

GUMROAD_SALE: dict[str, Any] = {
    "id": "S-1",
    "email": "Buyer@Example.com",
    "product_id": "gum-prod-1",
    "product_permalink": "basic",
    "price": 999,
    "gumroad_fee": 150,
    "currency": "usd",
    "created_at": "2026-10-01T10:00:00Z",
    "refunded": False,
    "chargedback": False,
    "disputed": False,
    "dispute_won": False,
    "ended": False,
    "cancelled": False,
    "subscription_id": None,
    "is_recurring_billing": False,
}


def _responder(prompt_value: Any) -> AIMessage:
    text = prompt_value.to_string()
    if "market researcher" in text:
        return AIMessage(content=json.dumps(RESEARCH))
    if "professional nonfiction ghostwriter" in text:
        return AIMessage(content=CHAPTER_TEXT)
    if "book cover art director" in text:
        return AIMessage(content=json.dumps(DESIGN))
    if "pricing strategist" in text:
        return AIMessage(content=json.dumps(PRICING))
    if "ebook marketing strategist" in text:
        return AIMessage(content=json.dumps(MARKETING))
    if "owner's data analyst" in text:
        return AIMessage(content=json.dumps(ANALYTICS))
    raise AssertionError("Unexpected prompt sent to the fake model")


def fake_llm(content: Any = None) -> RunnableLambda:
    """A Runnable that stands in for ChatOpenAI. With no argument it answers each agent with canned output."""
    if content is None:
        return RunnableLambda(_responder)
    text = content if isinstance(content, str) else json.dumps(content)
    return RunnableLambda(lambda _prompt_value: AIMessage(content=text))
