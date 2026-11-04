from __future__ import annotations

import sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).parent))
from mock_llm import MockLLM  # noqa: E402


@pytest.fixture(autouse=True)
def cts_home(tmp_path, monkeypatch):
    """Every test gets its own empty CTS data folder and fresh module singletons."""
    home = tmp_path / "cts-home"
    monkeypatch.setenv("CTS_HOME", str(home))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CTS_LLAMA_SERVER", raising=False)
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "XAI_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    from cts import jobs, settings
    from cts.local import models

    settings.reset_cache()
    jobs.manager = None
    models.installs.clear()
    yield home
    if jobs.manager is not None:
        for job in jobs.manager.jobs.values():
            job.cancel_event.set()
    jobs.manager = None


@pytest.fixture(autouse=True)
def no_system_changes(monkeypatch):
    """Tests must never install real system packages (apt/dnf/winget...). Tests that care override these."""
    from cts.local import deps

    calls = []
    monkeypatch.setattr(deps, "install_system_packages", lambda libs, *a, **k: (calls.append(("pkgs", libs)) or (False, "fake: not installed")))
    monkeypatch.setattr(deps, "install_vc_redist", lambda *a, **k: (calls.append(("vcredist",)) or (False, "fake: not installed")))
    return calls


@pytest.fixture
def mock_llm():
    servers: list[MockLLM] = []

    def factory(handler=None) -> MockLLM:
        server = MockLLM(handler).start()
        servers.append(server)
        return server

    yield factory
    for server in servers:
        server.stop()


@pytest.fixture
def pages(tmp_path) -> list[Path]:
    folder = tmp_path / "pages"
    folder.mkdir()
    out = []
    for i in range(1, 4):
        path = folder / f"{i}.png"
        Image.new("RGB", (240, 360), (i * 50, 90, 140)).save(path)
        out.append(path)
    return out


def local_spec(mock: MockLLM, context: int = 16384):
    from cts.llm import LLMSpec

    return LLMSpec("local", "local", "mock-vl", "sk-test", mock.base_url, context, 1024)
