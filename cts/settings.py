"""User settings, persisted as JSON in the CTS data directory.

API keys live in ``settings.json`` (permissions 0600 where the OS supports it) and
are never sent back to the browser - the UI only learns *whether* a key is set.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any, Literal

from pydantic import BaseModel, Field

from .paths import settings_file

CONTEXT_MIN = 2048
CONTEXT_MAX = 2_000_000


class FrontierSettings(BaseModel):
    provider: str = "anthropic"
    models: dict[str, str] = Field(default_factory=dict)  # provider -> model id
    base_urls: dict[str, str] = Field(default_factory=dict)  # provider -> custom base URL
    api_keys: dict[str, str] = Field(default_factory=dict)  # provider -> key (never exposed)


class LocalSettings(BaseModel):
    active_model: str = ""  # id of an installed model (see cts.local.models)
    hf_token: str = ""  # optional Hugging Face token for gated repos (never exposed)


class GenerationDefaults(BaseModel):
    rating: Literal["sfw", "nsfw"] = "sfw"
    words_per_page: int = 300
    language: str = "auto"
    carry_story_bible: bool = True
    skip_covers: bool = True
    final_review: bool = True
    custom_prompt: str = ""


class Settings(BaseModel):
    provider_type: Literal["frontier", "local"] = "frontier"
    frontier: FrontierSettings = Field(default_factory=FrontierSettings)
    local: LocalSettings = Field(default_factory=LocalSettings)
    context_window: int = 32768
    defaults: GenerationDefaults = Field(default_factory=GenerationDefaults)

    # ------------------------------------------------------------------ views
    def public(self) -> dict[str, Any]:
        """Settings safe to send to the browser (secrets replaced by booleans)."""
        data = self.model_dump()
        data["frontier"]["api_keys_set"] = {
            provider: bool(key) for provider, key in self.frontier.api_keys.items()
        }
        del data["frontier"]["api_keys"]
        data["local"]["hf_token_set"] = bool(self.local.hf_token)
        del data["local"]["hf_token"]
        return data


_lock = threading.RLock()
_cache: Settings | None = None


def _clamp_context(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 32768
    return max(CONTEXT_MIN, min(CONTEXT_MAX, number))


def load() -> Settings:
    global _cache
    with _lock:
        if _cache is not None:
            return _cache.model_copy(deep=True)
        path = settings_file()
        if path.exists():
            try:
                _cache = Settings.model_validate(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                # A corrupt settings file must never brick the app: keep a backup, start fresh.
                try:
                    path.replace(path.with_suffix(".json.bak"))
                except OSError:
                    pass
                _cache = Settings()
        else:
            _cache = Settings()
        return _cache.model_copy(deep=True)


def save(settings: Settings) -> None:
    global _cache
    with _lock:
        settings.context_window = _clamp_context(settings.context_window)
        path = settings_file()
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(settings.model_dump_json(indent=2), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:  # Windows / exotic filesystems
            pass
        tmp.replace(path)
        _cache = settings.model_copy(deep=True)


def update(patch: dict[str, Any]) -> Settings:
    """Apply a partial update coming from the UI.

    Secrets follow one rule: a key that is *absent* is left untouched, an empty string
    clears it, any other value replaces it.
    """
    with _lock:
        current = load()
        data = current.model_dump()

        def merge(dst: dict[str, Any], src: dict[str, Any]) -> None:
            for key, value in src.items():
                if key in ("api_keys", "models", "base_urls") and isinstance(value, dict):
                    bucket = dst.setdefault(key, {})
                    for name, item in value.items():
                        if item in ("", None):
                            bucket.pop(name, None)
                        else:
                            bucket[name] = str(item).strip()
                elif isinstance(value, dict) and isinstance(dst.get(key), dict):
                    merge(dst[key], value)
                else:
                    dst[key] = value

        patch = {k: v for k, v in patch.items() if k in Settings.model_fields}
        merge(data, patch)
        updated = Settings.model_validate(data)
        save(updated)
        return updated


def reset_cache() -> None:
    """For tests: forget the in-memory copy so the next load() re-reads the disk."""
    global _cache
    with _lock:
        _cache = None
