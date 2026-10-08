"""The six LangChain agents: ResearchAgent, WriterAgent, DesignerAgent, PricingAgent, MarketingAgent, AnalyticsAgent.

Each agent does one job, asks for a fixed JSON shape (except the writer, which returns Markdown),
and validates the result before returning it. Models never calculate money (see core/pricing.py),
and the analytics agent may only narrate numbers from the database snapshot it receives.
"""

from __future__ import annotations

import json
import re
from typing import Any

from langchain_core.output_parsers import JsonOutputParser, StrOutputParser
from langchain_core.prompts import ChatPromptTemplate

from core.llm import get_llm

HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")
LAUNCH_DELAYS = [0, 2, 4, 7, 10]
ONBOARDING_DELAYS = [0, 1, 3]


class AgentError(RuntimeError):
    """An agent call failed, or its output did not match the required schema."""


class BaseAgent:
    name = "BaseAgent"
    system_prompt = ""
    human_template = ""
    temperature = 0.4
    json_output = True
    max_tokens: int | None = None

    def __init__(self, llm: Any = None) -> None:
        self._llm = llm

    @property
    def llm(self) -> Any:
        if self._llm is None:
            self._llm = get_llm(temperature=self.temperature, json_mode=self.json_output, max_tokens=self.max_tokens)
        return self._llm

    def _invoke(self, human_template: str, **variables: Any) -> Any:
        prompt = ChatPromptTemplate.from_messages([("system", self.system_prompt), ("human", human_template)])
        parser = JsonOutputParser() if self.json_output else StrOutputParser()
        try:
            return (prompt | self.llm | parser).invoke(variables)
        except Exception as exc:  # network, model, or JSON parsing failure
            raise AgentError(f"{self.name} call failed: {exc}") from exc

    def _require(self, data: Any, keys: list[str]) -> dict:
        if not isinstance(data, dict):
            raise AgentError(f"{self.name} returned something other than a JSON object")
        missing = [key for key in keys if key not in data]
        if missing:
            raise AgentError(f"{self.name} output is missing: {', '.join(missing)}")
        return data


RESEARCH_SCHEMA = (
    '{"title_options": [{"title": string, "subtitle": string}] (exactly 3), '
    '"audience_profile": string, '
    '"pain_points": [string] (5 to 8 items), '
    '"competitor_gaps": [string] (3 to 5 items), '
    '"keyword_ideas": [string] (10 to 15 items), '
    '"outline": [{"chapter_title": string, "goal": string, "key_points": [string] (3 to 5 items)}] (8 to 12 chapters), '
    '"claims_to_verify": [string]}'
)


class ResearchAgent(BaseAgent):
    name = "ResearchAgent"
    temperature = 0.3
    system_prompt = (
        "You are a senior market researcher and nonfiction book strategist for digital ebooks. "
        "You never invent statistics, studies, quotes, prices, or sources. Use only the sources the user supplies "
        "for factual claims. Put every factual claim you cannot support from those sources into claims_to_verify. "
        "Return one JSON object and nothing else."
    )
    human_template = (
        "Topic: {topic}\nAudience: {audience}\n\n"
        "Sources supplied by the user (may be empty):\n{sources}\n\n"
        "Return a JSON object with exactly these keys and sizes: {schema}\n"
        "Outline chapters must be specific and actionable for this audience. Title options must not promise "
        "results the book cannot support."
    )

    def run(self, *, topic: str, audience: str, sources: list[dict] | None = None) -> dict:
        source_lines = "\n".join(f"- {s.get('url', '')} :: {s.get('note', '')}" for s in (sources or [])) or "(none)"
        data = self._invoke(self.human_template, topic=topic, audience=audience, sources=source_lines, schema=RESEARCH_SCHEMA)
        self._require(data, ["title_options", "outline", "claims_to_verify"])
        if not data["title_options"] or not data["outline"]:
            raise AgentError("ResearchAgent returned no titles or no outline")
        return data


class WriterAgent(BaseAgent):
    name = "WriterAgent"
    temperature = 0.6
    json_output = False
    max_tokens = 3500
    system_prompt = (
        "You are a professional nonfiction ghostwriter. Write complete, original, practical prose for the stated "
        "audience, in full, with no summaries in place of content. State only facts you are certain of. When an "
        "example is needed, label it as a hypothetical example. Never invent people, companies, prices, statistics, "
        "or testimonials. Never use placeholders such as [insert], TODO, or XX. Format in Markdown: begin the chapter "
        "with its title as a level-2 heading, use level-3 headings for sections, and use bullet lists where they help."
    )
    chapter_template = (
        "Book: {book_title}\nSubtitle: {subtitle}\nAudience: {audience}\n"
        "Chapter {number} of {total}: {chapter_title}\n"
        "Chapter goal: {goal}\n"
        "Key points (cover every one fully):\n{key_points}\n"
        "Chapter titles already written, for continuity: {previous}\n\n"
        "Write the complete chapter now, about 1,000 words."
    )

    def write_chapter(self, *, book_title: str, subtitle: str, audience: str, chapter: dict, number: int, total: int, previous: str) -> str:
        points = "\n".join(f"- {point}" for point in chapter.get("key_points", []))
        text = self._invoke(
            self.chapter_template,
            book_title=book_title,
            subtitle=subtitle,
            audience=audience,
            number=number,
            total=total,
            chapter_title=chapter.get("chapter_title", f"Chapter {number}"),
            goal=chapter.get("goal", ""),
            key_points=points,
            previous=previous,
        )
        text = str(text).strip()
        if not text:
            raise AgentError(f"WriterAgent returned an empty chapter {number}")
        if not text.startswith("#"):
            text = f"## {chapter.get('chapter_title', f'Chapter {number}')}\n\n{text}"
        return text

    def write_book(self, *, book_title: str, subtitle: str, audience: str, outline: list[dict]) -> dict:
        titles = [str(chapter.get("chapter_title", "")) for chapter in outline]
        parts: list[str] = []
        for number, chapter in enumerate(outline, start=1):
            previous = ", ".join(titles[: number - 1]) or "none"
            parts.append(self.write_chapter(book_title=book_title, subtitle=subtitle, audience=audience, chapter=chapter, number=number, total=len(outline), previous=previous))
        markdown = "\n\n".join(parts)
        return {"markdown": markdown, "chapters": len(outline), "words": len(markdown.split())}


class DesignerAgent(BaseAgent):
    name = "DesignerAgent"
    temperature = 0.7
    system_prompt = (
        "You are a book cover art director for ebooks. You create three distinct cover concepts with exact hex "
        "colors, real Google Fonts family names, layouts, and ready-to-paste image prompts for Midjourney and DALL-E. "
        "Image prompts describe artwork only and must say 'no text', because the title is added later in Canva. "
        "Return one JSON object and nothing else."
    )
    human_template = (
        "Book title: {book_title}\nSubtitle: {subtitle}\nAudience: {audience}\nStyle notes: {style_notes}\n\n"
        'Return a JSON object with the key "concepts" holding exactly 3 objects. Each object has these keys: '
        "name (string); layout (string describing where the artwork and the text areas sit, for Canva); "
        "colors (array of exactly 3 hex codes such as #1B2A41); "
        "fonts (object with headline and body keys, each a real Google Fonts family); "
        "midjourney_prompt (string, must include the words no text, and end with --ar 5:8 --no text, letters, words, logo); "
        "dalle_prompt (string, portrait composition, must include the words no text); "
        "canva_steps (array of 4 to 6 short imperative steps for finishing the cover in Canva Free); "
        "text_for_canva (object with title and subtitle keys, copied exactly from the book details)."
    )

    def run(self, *, book_title: str, subtitle: str, audience: str, style_notes: str = "none") -> dict:
        data = self._invoke(self.human_template, book_title=book_title, subtitle=subtitle, audience=audience, style_notes=style_notes)
        self._require(data, ["concepts"])
        concepts = data["concepts"]
        if not isinstance(concepts, list) or len(concepts) != 3:
            raise AgentError("DesignerAgent must return exactly 3 concepts")
        for concept in concepts:
            self._require(concept, ["name", "layout", "colors", "fonts", "midjourney_prompt", "dalle_prompt", "canva_steps", "text_for_canva"])
            colors = concept["colors"]
            if not isinstance(colors, list) or len(colors) != 3 or not all(isinstance(c, str) and HEX_COLOR.match(c) for c in colors):
                raise AgentError("Each concept needs exactly 3 hex colors such as #1B2A41")
            for prompt in (concept["midjourney_prompt"], concept["dalle_prompt"]):
                if "no text" not in str(prompt).lower():
                    raise AgentError("Image prompts must say 'no text' because the title is added in Canva")
        return data


class PricingAgent(BaseAgent):
    name = "PricingAgent"
    temperature = 0.3
    system_prompt = (
        "You are a pricing strategist for digital products. The fee plan you receive is final and was computed by "
        "code. Do not change, recompute, or round any number in it. Write customer-facing tier descriptions and the "
        "rationale. Do not claim sales results, conversion rates, or market averages. Return one JSON object and nothing else."
    )
    human_template = (
        "Book: {book_title}\nAudience: {audience}\n\n"
        "Approved fee plan (final numbers, computed in code):\n{plan}\n\n"
        'Return a JSON object with keys: "tiers" (array of exactly 3 objects in this order: basic, pro, bundle; each '
        "with tier_id, name, description (2 to 3 sentences), includes (array of strings naming the exact contents), "
        'and platform) and "rationale" (4 to 6 sentences explaining the price ladder from the $9.99 book to the Pro '
        "and Bundle prices)."
    )

    def run(self, *, book_title: str, audience: str, plan: dict) -> dict:
        data = self._invoke(self.human_template, book_title=book_title, audience=audience, plan=json.dumps(plan, indent=2))
        self._require(data, ["tiers", "rationale"])
        if not isinstance(data["tiers"], list) or len(data["tiers"]) != 3:
            raise AgentError("PricingAgent must return exactly 3 tiers")
        return data


class MarketingAgent(BaseAgent):
    name = "MarketingAgent"
    temperature = 0.7
    max_tokens = 4000
    system_prompt = (
        "You are an ebook marketing strategist and copywriter. Write honest copy: no invented results, no fake "
        "urgency, no guarantees. Do not include URLs or bracketed fields, because the system adds links. Start every "
        "email with 'Hi there,'. Return one JSON object and nothing else."
    )
    human_template = (
        "Book: {book_title}\nSubtitle: {subtitle}\nAudience: {audience}\nOffer: {offer}\n\n"
        "Return a JSON object with keys:\n"
        '"social_posts": array of exactly {post_count} objects. Each has caption (60 to 150 words; the first line is a '
        "hook), hashtags (array of 5 to 8 strings without the # sign), and image_brief (one sentence describing the "
        "image to pair with the caption).\n"
        '"email_sequences": object with "launch" (exactly 5 objects) and "onboarding" (exactly 3 objects). Each email '
        "has subject (under 60 characters), preview_text (under 90 characters), and body_text (90 to 180 words, plain "
        "text, no links)."
    )

    def run(self, *, book_title: str, subtitle: str, audience: str, offer: str, post_count: int) -> dict:
        data = self._invoke(self.human_template, book_title=book_title, subtitle=subtitle, audience=audience, offer=offer, post_count=post_count)
        self._require(data, ["social_posts", "email_sequences"])
        posts = data["social_posts"]
        if not isinstance(posts, list) or not posts:
            raise AgentError("MarketingAgent returned no social posts")
        for post in posts:
            self._require(post, ["caption", "hashtags"])
            if not isinstance(post["hashtags"], list):
                raise AgentError("Each post needs a hashtags array")
        sequences = self._require(data["email_sequences"], ["launch", "onboarding"])
        for name, delays, expected in (("launch", LAUNCH_DELAYS, 5), ("onboarding", ONBOARDING_DELAYS, 3)):
            emails = sequences[name]
            if not isinstance(emails, list) or len(emails) != expected:
                raise AgentError(f"The {name} sequence must have exactly {expected} emails")
            for email, delay in zip(emails, delays, strict=False):
                self._require(email, ["subject", "body_text"])
                email["delay_days"] = delay  # timing is fixed by code, not by the model
        return {"social_posts": posts, "email_sequences": sequences}


class AnalyticsAgent(BaseAgent):
    name = "AnalyticsAgent"
    temperature = 0.2
    system_prompt = (
        "You are the owner's data analyst for an ebook business. Use only the numbers in the snapshot you receive. "
        "Never invent, estimate, or extrapolate numbers. If the sample is small (fewer than 30 clicks or fewer than 5 "
        "sales), say so and avoid firm conclusions. Report what is working, what is failing, and how to improve sales. "
        "Return one JSON object and nothing else."
    )
    human_template = (
        "Snapshot (JSON; the only data you may use):\n{snapshot}\n\n"
        'Return a JSON object with keys: "what_is_working" (array of strings, each citing a number from the snapshot), '
        '"what_is_failing" (array of strings, each citing a number), "how_to_improve_sales" (array of objects with '
        'action, reason, and metric_to_watch), and "data_gaps" (array of strings describing data to start recording).'
    )

    def run(self, *, snapshot: dict) -> dict:
        data = self._invoke(self.human_template, snapshot=json.dumps(snapshot, indent=2, default=str))
        return self._require(data, ["what_is_working", "what_is_failing", "how_to_improve_sales", "data_gaps"])
