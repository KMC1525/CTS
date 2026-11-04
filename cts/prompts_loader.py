"""Loads the Markdown system prompts (built-in, with optional per-user overrides)."""

from __future__ import annotations

from dataclasses import dataclass

from .paths import PROMPTS_DIR, user_prompts_dir


@dataclass(frozen=True)
class Prompt:
    name: str
    role: str
    goal: str
    body: str

    def system_text(self) -> str:
        return self.body.strip()


def _parse(name: str, raw: str) -> Prompt:
    role, goal, body = name, "", raw
    text = raw.lstrip("﻿")
    if text.startswith("---"):
        parts = text.split("\n---", 1)
        if len(parts) == 2:
            header = parts[0].lstrip("-").strip("\n")
            body = parts[1].lstrip("-").lstrip("\n") if parts[1].startswith("---") else parts[1]
            body = body.lstrip("\r\n")
            for line in header.splitlines():
                key, _, value = line.partition(":")
                key, value = key.strip().lower(), value.strip()
                if key == "role":
                    role = value
                elif key == "goal":
                    goal = value
    return Prompt(name=name, role=role, goal=goal, body=body)


def load(name: str) -> Prompt:
    """Load ``<name>.md``; a copy in the user's prompts folder wins over the built-in one."""
    override = user_prompts_dir() / f"{name}.md"
    path = override if override.is_file() else PROMPTS_DIR / f"{name}.md"
    return _parse(name, path.read_text(encoding="utf-8"))
