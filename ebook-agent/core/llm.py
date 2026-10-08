"""Chat model factory. Every LLM call in the project goes through get_llm()."""

from __future__ import annotations

from typing import Any

from langchain_openai import ChatOpenAI

from config import settings


class LLMConfigError(RuntimeError):
    """OPENAI_API_KEY is missing, so no model call can be made."""


def get_llm(*, temperature: float | None = None, json_mode: bool = False, max_tokens: int | None = None) -> ChatOpenAI:
    if not settings.openai_api_key:
        raise LLMConfigError("OPENAI_API_KEY is not set. Add it to your .env file.")
    extra: dict[str, Any] = {}
    if json_mode:
        extra["model_kwargs"] = {"response_format": {"type": "json_object"}}
    return ChatOpenAI(
        model=settings.openai_model,
        api_key=settings.openai_api_key,
        temperature=settings.openai_temperature if temperature is None else temperature,
        max_tokens=max_tokens,
        timeout=180,
        max_retries=3,
        **extra,
    )
