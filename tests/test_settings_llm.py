import json
import os

import pytest

from cts import llm, settings
from cts.pipeline.text import language_instruction


def test_defaults_and_secret_masking(cts_home):
    s = settings.update({"frontier": {"provider": "openai", "api_keys": {"openai": "sk-secret"}, "models": {"openai": "gpt-5"}},
                         "local": {"hf_token": "hf_secret"}, "context_window": 65536})
    public = json.dumps(s.public())
    assert "sk-secret" not in public and "hf_secret" not in public
    assert s.public()["frontier"]["api_keys_set"] == {"openai": True} and s.public()["local"]["hf_token_set"] is True
    # secrets are on disk, readable only by the owner
    path = cts_home / "settings.json"
    assert "sk-secret" in path.read_text()
    if os.name == "posix":
        assert (path.stat().st_mode & 0o777) == 0o600


def test_secret_update_rules(cts_home):
    settings.update({"frontier": {"api_keys": {"anthropic": "k1"}}})
    settings.update({"frontier": {"provider": "openai"}})  # key absent: untouched
    assert settings.load().frontier.api_keys == {"anthropic": "k1"}
    settings.update({"frontier": {"api_keys": {"anthropic": ""}}})  # empty string clears
    assert settings.load().frontier.api_keys == {}


def test_context_window_is_clamped_and_unknown_keys_ignored(cts_home):
    assert settings.update({"context_window": 10}).context_window == 2048
    s = settings.update({"nonsense": 1, "context_window": 131072})
    assert s.context_window == 131072 and not hasattr(s, "nonsense")


def test_corrupt_settings_file_does_not_brick_the_app(cts_home):
    cts_home.mkdir(parents=True, exist_ok=True)
    (cts_home / "settings.json").write_text("{not json")
    settings.reset_cache()
    assert settings.load().context_window == 32768
    assert (cts_home / "settings.json.bak").exists()


def test_resolve_spec_errors_are_friendly(cts_home, monkeypatch):
    with pytest.raises(llm.ConfigError, match="Anthropic.*API key"):
        llm.resolve_spec(settings.load())
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env")
    spec = llm.resolve_spec(settings.load())
    assert (spec.provider, spec.api_key, spec.model) == ("anthropic", "from-env", "claude-sonnet-5-5")
    s = settings.update({"frontier": {"provider": "custom"}})
    with pytest.raises(llm.ConfigError, match="base URL"):
        llm.resolve_spec(s)
    with pytest.raises(llm.ConfigError, match="local model"):
        llm.resolve_spec(settings.update({"provider_type": "local"}))


def test_xai_uses_openai_client_with_xai_endpoint(cts_home):
    s = settings.update({"frontier": {"provider": "xai", "api_keys": {"xai": "x"}}, "context_window": 50000})
    spec = llm.resolve_spec(s)
    assert spec.base_url == "https://api.x.ai/v1"
    built = llm.build_llm(spec, temperature=0.5, max_tokens=500)
    assert type(built).__name__ == "OpenAICompletion" and built.base_url == "https://api.x.ai/v1"
    assert built.context_window_size == 50000 and built.max_tokens == 500


def test_openai_models_get_max_completion_tokens_and_no_temperature(cts_home):
    spec = llm.resolve_spec(settings.update({"frontier": {"provider": "openai", "api_keys": {"openai": "k"}}}))
    built = llm.build_llm(spec, temperature=0.9, max_tokens=700)
    assert built.max_completion_tokens == 700 and built.max_tokens is None and built.temperature is None


def test_image_injection_per_provider():
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "Describe. [[CTS_IMAGE:%s]] Now."}]
    token = llm.register_image(b"\xff\xd8abc", "image/jpeg")
    msgs[1]["content"] %= token
    assert llm.inject_images(msgs, "openai") == 1
    parts = msgs[1]["content"]
    assert parts[0] == {"type": "text", "text": "Describe.  Now."}
    assert parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")

    gem = [{"role": "user", "content": f"x [[CTS_IMAGE:{token}]]"}]
    llm.inject_images(gem, "gemini")
    assert gem[0]["content"][1]["inlineData"]["mimeType"] == "image/jpeg"

    llm.release_image(token)
    gone = [{"role": "user", "content": f"x [[CTS_IMAGE:{token}]]"}]
    assert llm.inject_images(gone, "openai") == 0 and gone[0]["content"] == "x"


def test_error_classification():
    assert llm.describe_error(RuntimeError("Error code: 401 - invalid api key"))[1] is True
    assert llm.describe_error(RuntimeError("model_not_found"))[1] is True
    assert llm.describe_error(ConnectionError("Connection reset"))[1] is False
    msg, fatal = llm.describe_error(RuntimeError("this model does not support image input"))
    assert fatal and "vision" in msg


def test_language_instruction():
    assert "lettering" in language_instruction("auto") and language_instruction("French") == "French"
