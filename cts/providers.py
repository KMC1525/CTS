"""Catalog of frontier (hosted) providers and how to reach them through CrewAI.

CrewAI 1.x ships native clients for OpenAI, Anthropic and Gemini. Everything else that
speaks the OpenAI wire protocol (xAI Grok, OpenRouter, DeepSeek, vLLM, LM Studio, ...)
is reached through the OpenAI client with a custom ``base_url``.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Provider:
    id: str
    label: str
    crewai_prefix: str  # prefix CrewAI uses to select the client
    base_url: str | None  # fixed endpoint, or None for the SDK default
    env_key: str  # environment variable used as a fallback for the API key
    models: tuple[str, ...] = field(default_factory=tuple)  # suggestions only
    custom_base_url: bool = False  # user supplies the endpoint
    requires_key: bool = True
    note: str = ""

    def public(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "models": list(self.models),
            "custom_base_url": self.custom_base_url,
            "requires_key": self.requires_key,
            "default_base_url": self.base_url or "",
            "note": self.note,
        }


PROVIDERS: dict[str, Provider] = {
    p.id: p
    for p in (
        Provider(
            "anthropic", "Anthropic - Claude", "anthropic", None, "ANTHROPIC_API_KEY",
            ("claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5"),
        ),
        Provider(
            "openai", "OpenAI - ChatGPT", "openai", None, "OPENAI_API_KEY",
            ("gpt-5", "gpt-5-mini", "gpt-4.1", "gpt-4o"),
        ),
        Provider(
            "xai", "xAI - Grok", "openai", "https://api.x.ai/v1", "XAI_API_KEY",
            ("grok-4", "grok-4-fast", "grok-2-vision-1212"),
        ),
        Provider(
            "gemini", "Google - Gemini", "gemini", None, "GEMINI_API_KEY",
            ("gemini-2.5-pro", "gemini-2.5-flash"),
        ),
        Provider(
            "openrouter", "OpenRouter (any model)", "openrouter", "https://openrouter.ai/api/v1",
            "OPENROUTER_API_KEY",
            ("anthropic/claude-sonnet-4.5", "google/gemini-2.5-pro", "x-ai/grok-4", "qwen/qwen2.5-vl-72b-instruct"),
            note="Use the exact model slug from openrouter.ai/models. Pick a vision-capable model.",
        ),
        Provider(
            "custom", "Custom OpenAI-compatible endpoint", "openai", None, "OPENAI_COMPATIBLE_API_KEY",
            (), custom_base_url=True, requires_key=False,
            note="LM Studio, vLLM, Ollama (/v1), a company gateway... any server that speaks the OpenAI API.",
        ),
    )
}

DEFAULT_MODEL: dict[str, str] = {pid: (p.models[0] if p.models else "") for pid, p in PROVIDERS.items()}


def catalog() -> list[dict]:
    return [p.public() for p in PROVIDERS.values()]
