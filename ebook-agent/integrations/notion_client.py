"""Notion: save a manuscript as a child page under NOTION_PARENT_PAGE_ID (Notion-Version 2022-06-28)."""

from __future__ import annotations

import re
from typing import Any

from config import settings
from integrations.net import ApiError, request

NOTION_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
MAX_BLOCKS_PER_REQUEST = 100
MAX_TEXT_CHARS = 2000

HEADING_RE = re.compile(r"^(#{1,3})\s+(.*)$")
BULLET_RE = re.compile(r"^\s*[-*]\s+(.*)$")


class NotionError(RuntimeError):
    """A Notion call failed, or Notion is not configured."""


def _headers() -> dict[str, str]:
    if not settings.notion_api_key:
        raise NotionError("NOTION_API_KEY is not set")
    return {
        "Authorization": f"Bearer {settings.notion_api_key}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _rich_text(text: str) -> list[dict[str, Any]]:
    chunks = [text[i : i + MAX_TEXT_CHARS] for i in range(0, len(text), MAX_TEXT_CHARS)] or [""]
    return [{"type": "text", "text": {"content": chunk}} for chunk in chunks]


def markdown_to_blocks(markdown: str) -> list[dict[str, Any]]:
    """Convert the subset of Markdown the writer produces: headings, bullets, and paragraphs."""
    blocks: list[dict[str, Any]] = []
    paragraph: list[str] = []

    def flush() -> None:
        if paragraph:
            text = " ".join(paragraph)
            blocks.append({"object": "block", "type": "paragraph", "paragraph": {"rich_text": _rich_text(text)}})
            paragraph.clear()

    for raw_line in markdown.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            flush()
            continue
        heading = HEADING_RE.match(line)
        if heading:
            flush()
            kind = f"heading_{len(heading.group(1))}"
            blocks.append({"object": "block", "type": kind, kind: {"rich_text": _rich_text(heading.group(2).strip())}})
            continue
        bullet = BULLET_RE.match(line)
        if bullet:
            flush()
            blocks.append(
                {
                    "object": "block",
                    "type": "bulleted_list_item",
                    "bulleted_list_item": {"rich_text": _rich_text(bullet.group(1).strip())},
                }
            )
            continue
        paragraph.append(line.strip())
    flush()
    return blocks


def _call(method: str, url: str, **kwargs: Any) -> dict[str, Any]:
    try:
        response = request("Notion", method, url, headers=_headers(), timeout=60, **kwargs)
    except ApiError as exc:
        raise NotionError(str(exc)) from exc
    return response.json()


def create_notion_page(title: str, markdown: str) -> dict[str, Any]:
    if not settings.notion_parent_page_id:
        raise NotionError("NOTION_PARENT_PAGE_ID is not set")
    blocks = markdown_to_blocks(markdown)
    body = {
        "parent": {"page_id": settings.notion_parent_page_id},
        "properties": {"title": {"title": [{"type": "text", "text": {"content": title[:MAX_TEXT_CHARS]}}]}},
        "children": blocks[:MAX_BLOCKS_PER_REQUEST],
    }
    page = _call("POST", f"{NOTION_BASE}/pages", json=body)
    page_id = page["id"]
    for start in range(MAX_BLOCKS_PER_REQUEST, len(blocks), MAX_BLOCKS_PER_REQUEST):
        _call(
            "PATCH",
            f"{NOTION_BASE}/blocks/{page_id}/children",
            json={"children": blocks[start : start + MAX_BLOCKS_PER_REQUEST]},
        )
    return page
