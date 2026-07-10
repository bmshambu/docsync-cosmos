"""LLM client factory — supports Google Gemini and Azure OpenAI.

Set LLM_PROVIDER in .env to switch:
  LLM_PROVIDER=google          → uses GOOGLE_API_KEY + MODEL_* settings
  LLM_PROVIDER=azure_openai    → uses AZURE_OPENAI_* settings
"""

from __future__ import annotations

from functools import lru_cache

from langchain_core.language_models.chat_models import BaseChatModel

from app.config import get_settings


@lru_cache(maxsize=16)
def get_chat(
    model: str,
    temperature: float = 0.0,
    max_tokens: int = 8192,
    json_mode: bool = False,
) -> BaseChatModel:
    settings = get_settings()
    if settings.llm_provider == "azure_openai":
        return _azure_chat(settings, temperature, max_tokens, json_mode)
    return _google_chat(settings, model, temperature, max_tokens, json_mode)


# ── Google Gemini ─────────────────────────────────────────────────────────────

def _google_chat(settings, model: str, temperature: float, max_tokens: int, json_mode: bool) -> BaseChatModel:
    from langchain_google_genai import ChatGoogleGenerativeAI

    if not settings.google_api_key:
        raise RuntimeError(
            "GOOGLE_API_KEY is not set. Add it to .env, "
            "or switch LLM_PROVIDER=azure_openai and set AZURE_OPENAI_* vars."
        )
    extra: dict = {}
    if json_mode:
        # Forces Gemini to return valid JSON — critical for entity extraction
        extra["response_mime_type"] = "application/json"

    return ChatGoogleGenerativeAI(
        model=model,
        temperature=temperature,
        max_output_tokens=max_tokens,
        google_api_key=settings.google_api_key,
        timeout=120,
        max_retries=3,
        **extra,
    )


# ── Azure OpenAI ──────────────────────────────────────────────────────────────

def _azure_chat(settings, temperature: float, max_tokens: int, json_mode: bool) -> BaseChatModel:
    from langchain_openai import AzureChatOpenAI

    if not settings.azure_openai_api_key or not settings.azure_openai_endpoint:
        raise RuntimeError(
            "AZURE_OPENAI_API_KEY and AZURE_OPENAI_ENDPOINT must both be set "
            "when LLM_PROVIDER=azure_openai."
        )
    kwargs: dict = {}
    if json_mode:
        kwargs["model_kwargs"] = {"response_format": {"type": "json_object"}}

    return AzureChatOpenAI(
        azure_deployment=settings.azure_openai_deployment,
        azure_endpoint=settings.azure_openai_endpoint,
        api_key=settings.azure_openai_api_key,
        api_version=settings.azure_openai_api_version,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=120,
        max_retries=3,
        **kwargs,
    )
