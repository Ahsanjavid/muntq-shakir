from __future__ import annotations

from openai import AsyncOpenAI

from chatbot.config import settings

_client: AsyncOpenAI | None = None


def get_llm_client() -> AsyncOpenAI:

    global _client
    if _client is None:
        _client = AsyncOpenAI(
            base_url=settings.LLM_BASE_URL,
            api_key=settings.LLM_API_KEY,
        )
    return _client
