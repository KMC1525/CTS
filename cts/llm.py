"""Builds CrewAI LLM objects from the user's settings and feeds comic pages to vision models.

Vision is injected with a CrewAI ``before_llm_call`` hook: the analyst task contains a
marker ``[[CTS_IMAGE:<token>]]`` and the hook swaps it for the provider-specific image
block. This works for every backend (Anthropic, OpenAI, Gemini, xAI, OpenRouter, local
llama.cpp) - CrewAI's own ``input_files`` only covers providers with a fixed model list.
"""

from __future__ import annotations

import base64
import os
import re
import threading
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from . import settings as settings_mod
from .paths import subdir
from .providers import DEFAULT_MODEL, PROVIDERS


class ConfigError(RuntimeError):
    """The user has not finished configuring a provider (missing key, model, endpoint...)."""


@dataclass
class LLMSpec:
    kind: str  # "frontier" | "local"
    provider: str  # provider id, or "local"
    model: str
    api_key: str | None
    base_url: str | None
    context_window: int
    max_image_side: int

    @property
    def label(self) -> str:
        return f"{PROVIDERS[self.provider].label if self.provider in PROVIDERS else 'Local GGUF'} / {self.model}"


def resolve_spec(settings: settings_mod.Settings, local_endpoint: str | None = None,
                 local_model_name: str = "local") -> LLMSpec:
    ctx = settings.context_window
    if settings.provider_type == "local":
        if not local_endpoint:
            raise ConfigError("The local model is not running. Install and select a model in Settings.")
        return LLMSpec("local", "local", local_model_name, "sk-local", local_endpoint, ctx, 1024)

    pid = settings.frontier.provider
    provider = PROVIDERS.get(pid)
    if provider is None:
        raise ConfigError(f"Unknown provider: {pid}")
    key = settings.frontier.api_keys.get(pid) or os.environ.get(provider.env_key) or ""
    if provider.requires_key and not key:
        raise ConfigError(f"Add your {provider.label} API key in Settings (or set {provider.env_key}).")
    base_url = settings.frontier.base_urls.get(pid) or provider.base_url
    if provider.custom_base_url and not base_url:
        raise ConfigError("Enter the base URL of your OpenAI-compatible endpoint in Settings.")
    model = settings.frontier.models.get(pid) or DEFAULT_MODEL.get(pid, "")
    if not model:
        raise ConfigError("Choose a model in Settings.")
    return LLMSpec("frontier", pid, model, key or "not-needed", base_url, ctx, 1568)


LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def is_loopback(url: str | None) -> bool:
    host = (urlparse(url or "").hostname or "").lower()
    return host in LOOPBACK_HOSTS or host.startswith("127.")


def bypass_proxy_for_loopback() -> None:
    """Never send traffic to this computer through a proxy.

    The HTTP clients used here (httpx, the OpenAI SDK) obey ``HTTP_PROXY``/``HTTPS_PROXY``. On a network that
    sets one without ``NO_PROXY`` for localhost, requests to the local llama-server would go to the proxy
    and fail or time out.
    """
    for var in ("NO_PROXY", "no_proxy"):
        items = [x.strip() for x in os.environ.get(var, "").split(",") if x.strip()]
        for host in LOOPBACK_HOSTS:
            if host not in items:
                items.append(host)
        os.environ[var] = ",".join(items)


def build_llm(spec: LLMSpec, *, temperature: float | None, max_tokens: int, timeout: int = 900) -> Any:
    """Create a CrewAI ``LLM`` for the given spec."""
    os.environ.setdefault("CREWAI_STORAGE_DIR", str(subdir("crewai")))
    if spec.kind == "local" or is_loopback(spec.base_url):
        bypass_proxy_for_loopback()
    from crewai import LLM

    if spec.kind == "local":
        prefix = "openai"
    else:
        prefix = PROVIDERS[spec.provider].crewai_prefix
    kwargs: dict[str, Any] = {
        "model": f"{prefix}/{spec.model}",
        "api_key": spec.api_key,
        "timeout": timeout,
        # Tell CrewAI the real window, otherwise it assumes ~8K and summarises prompts needlessly.
        "context_window_size": int(spec.context_window),
    }
    if spec.base_url:
        kwargs["base_url"] = spec.base_url
    if spec.provider == "openai":
        # Current OpenAI models (gpt-5, o-series) reject max_tokens and non-default temperature.
        kwargs["max_completion_tokens"] = max_tokens
    else:
        kwargs["max_tokens"] = max_tokens
        if temperature is not None:
            kwargs["temperature"] = temperature
    return LLM(**kwargs)


# ------------------------------------------------------------------------- vision
_MARKER = re.compile(r"\[\[CTS_IMAGE:([0-9a-f]{12})\]\]")
_images: dict[str, tuple[str, str]] = {}  # token -> (base64, mime)
_images_lock = threading.Lock()
_hook_registered = False
_hook_lock = threading.Lock()


def register_image(data: bytes, mime: str = "image/jpeg") -> str:
    token = uuid.uuid4().hex[:12]
    with _images_lock:
        _images[token] = (base64.b64encode(data).decode("ascii"), mime)
    return token


def release_image(token: str) -> None:
    with _images_lock:
        _images.pop(token, None)


def image_marker(token: str) -> str:
    return f"[[CTS_IMAGE:{token}]]"


def multimodal_content(provider_name: str, text: str, b64: str, mime: str) -> list[dict[str, Any]]:
    """Content parts for one user message: text followed by the image, in the provider's dialect."""
    if "gemini" in provider_name or "google" in provider_name:
        return [{"text": text}, {"inlineData": {"mimeType": mime, "data": b64}}]
    # OpenAI, xAI, OpenRouter, llama.cpp ... (CrewAI's Anthropic client converts this form itself)
    return [{"type": "text", "text": text}, {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}]


def inject_images(messages: list[dict[str, Any]], provider_name: str) -> int:
    """Replace image markers in-place. Returns how many images were attached."""
    attached = 0
    for message in messages:
        content = message.get("content")
        if not isinstance(content, str):
            continue
        match = _MARKER.search(content)
        if not match:
            continue
        with _images_lock:
            image = _images.get(match.group(1))
        text = _MARKER.sub("", content).strip()
        if image is None:
            message["content"] = text
            continue
        message["content"] = multimodal_content(provider_name, text, image[0], image[1])
        attached += 1
    return attached


def _before_llm_call(context: Any) -> None:
    provider = str(getattr(context.llm, "provider", "") or getattr(context.llm, "model", "")).lower()
    inject_images(context.messages, provider)
    return None


def ensure_vision_hook() -> None:
    global _hook_registered
    with _hook_lock:
        if _hook_registered:
            return
        from crewai.hooks import register_before_llm_call_hook

        register_before_llm_call_hook(_before_llm_call)
        _hook_registered = True


# ------------------------------------------------------------------------- errors
# llama-server answers with these when a request cannot work however often it is repeated.
_CONTEXT_OVERFLOW = ("exceed_context_size", "exceeds the available context size", "larger than the max context size",
                     "context length exceeded", "maximum context length", "context_length_exceeded")
_NO_VISION = ("image input is not supported", "does not support image", "does not support vision",
              "vision is not supported", "multimodal is not supported")

_FATAL_HINTS = (
    "401", "403", "invalid api key", "invalid_api_key", "incorrect api key", "authentication",
    "permission", "unauthorized", "not_found_error", "model_not_found", "does not exist",
    "insufficient_quota", "billing", "credit balance",
)


def describe_error(exc: BaseException) -> tuple[str, bool]:
    """Return (friendly message, fatal?). Fatal errors will not go away by retrying."""
    text = f"{type(exc).__name__}: {exc}".strip()
    lowered = text.lower()
    if any(h in lowered for h in _CONTEXT_OVERFLOW):
        return (text[:400] + "\n-> The request does not fit in the model's context window. Raise the context window in "
                "Settings (and make sure the model/server really has it), or use a lighter quantization.", True)
    if any(h in lowered for h in _NO_VISION):
        return (text[:400] + "\n-> This model cannot read images. Choose a vision-capable model (a local model needs "
                "its mmproj file).", True)
    fatal = any(h in lowered for h in _FATAL_HINTS) or isinstance(exc, ConfigError)
    if "image" in lowered and ("support" in lowered or "vision" in lowered or "multimodal" in lowered):
        text += "  (This model may not support images - choose a vision-capable model.)"
        fatal = True
    return text[:600], fatal


def test_connection(spec: LLMSpec) -> str:
    """One tiny text round-trip; returns the model's reply."""
    llm = build_llm(spec, temperature=0.0, max_tokens=64 if spec.provider != "openai" else 1024, timeout=60)
    reply = llm.call([{"role": "user", "content": "Reply with the single word: OK"}])
    return str(reply).strip()[:120]
