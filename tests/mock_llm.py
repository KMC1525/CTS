"""A tiny OpenAI-compatible server used by the tests (no network, no API keys)."""

from __future__ import annotations

import json
import re
import socket
import threading
import time
from typing import Any, Callable

import uvicorn
from fastapi import FastAPI, Request

ANALYSIS = """PAGE TYPE: story
READING DIRECTION: left-to-right
SETTING: A rainy rooftop at night.
PANELS:
1. Wide shot - Mira stands at the edge of the roof, coat flapping.
   TEXT:
   - Mira (speech): "I told you not to follow me."
CHARACTERS ON PAGE:
- Mira: short black hair, red coat, adult woman
MOOD AND PACING: Tense and quiet.
CONTINUITY CUES: none
FLAGS:
sexual_content: none
minor_involved: no
violence: none
"""

BIBLE = """# Story Bible
## Voice
Third person limited, past tense.
## Language
English
## Characters
- Mira: black hair, red coat, adult, stubborn.
## Places and objects
- The rooftop: rainy, neon-lit.
## Story so far
Mira confronts a follower on the rooftop.
## Open threads
Who followed her?
## Terms
none
"""

NOT_COVER = """IS_COVER: no
COVER_KIND: none
CONFIDENCE: high
TITLE: none
VOLUME: none
EVIDENCE: A page of panels with speech balloons.
"""

FRONT_COVER = """IS_COVER: yes
COVER_KIND: front
CONFIDENCE: high
TITLE: Midnight Rooftop
VOLUME: 1
EVIDENCE: Large title logo, publisher imprint and a composed key illustration without panels.
"""

PROSE = "Rain hammered the rooftop. Mira did not turn around. \"I told you not to follow me,\" she said.\n\nThe city glowed below her, indifferent."


COHERENT = """VERDICT: coherent
PAGES: none
NOTES: none
REASON: The pages follow each other and agree with the bible.
"""


def default_review(messages: list[dict[str, Any]]) -> str:
    """What a happy Final Revisor answers: every page OK for task A, 'coherent' for task B."""
    task = _text(messages[-1].get("content"))
    if "TASK A" in task:
        return "\n".join(f"PAGE {n} | OK | summary: Page {n} of the story." for n in re.findall(r"--- PAGE (\d+) ---", task))
    return COHERENT


class MockLLM:
    """Records every request; answers by agent role found in the system prompt."""

    def __init__(self, handler: Callable[[str, list[dict[str, Any]]], str] | None = None):
        self.requests: list[dict[str, Any]] = []
        self.handler = handler
        self.delay = 0.0
        self.app = FastAPI()
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self.port = 0
        self._route()

    @staticmethod
    def role_of(messages: list[dict[str, Any]]) -> str:
        system = " ".join(_text(m.get("content")) for m in messages if m.get("role") == "system")
        first = system.lower()
        for key, role in (("final revisor", "revisor"), ("book cover identifier", "cover"), ("comic page analyst", "analyst"), ("story keeper", "keeper"),
                          ("fiction editor", "editor"), ("literary novelist", "novelist")):
            if key in first:
                return role
        return "other"

    def _route(self) -> None:
        @self.app.post("/v1/chat/completions")
        async def completions(request: Request):
            body = await request.json()
            self.requests.append(body)
            if self.delay:
                time.sleep(self.delay)
            messages = body.get("messages", [])
            role = self.role_of(messages)
            if self.handler:
                content = self.handler(role, messages)
                if role == "revisor" and not isinstance(content, str):
                    content = default_review(messages)
            else:
                content = {"analyst": ANALYSIS, "keeper": BIBLE, "cover": NOT_COVER}.get(role, PROSE)
                if role == "revisor":
                    content = default_review(messages)
            return {
                "id": "mock", "object": "chat.completion", "created": 0, "model": body.get("model", "mock"),
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": content}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
            }

        @self.app.get("/health")
        async def health():
            return {"status": "ok"}

    # ----------------------------------------------------------------- lifecycle
    def start(self) -> "MockLLM":
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        config = uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="error")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.2):
                    return self
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("mock server did not start")

    def stop(self) -> None:
        if self._server:
            self._server.should_exit = True
        if self._thread:
            self._thread.join(timeout=5)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def images_seen(self) -> int:
        count = 0
        for req in self.requests:
            for m in req.get("messages", []):
                if isinstance(m.get("content"), list):
                    count += sum(1 for part in m["content"] if part.get("type") == "image_url")
        return count


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""
