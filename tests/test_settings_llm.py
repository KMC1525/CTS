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


# ---------------------------------------------------------------- v0.3: local connection
def test_local_requests_bypass_http_proxies(mock_llm, monkeypatch):
    """Reproduces: with HTTP_PROXY set (and no NO_PROXY) every call to the engine on 127.0.0.1 went to the proxy."""
    from conftest import local_spec

    mock = mock_llm()
    for var in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.setenv(var, "http://127.0.0.1:9")  # a proxy that does not exist
    for var in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(var, raising=False)
    reply = llm.test_connection(local_spec(mock))
    assert reply == "Rain hammered the rooftop. Mira did not turn around. \"I told you not to follow me,\" she said.\n\nThe city glowed below her, indifferent."[:120]
    assert {"127.0.0.1", "localhost"} <= set(os.environ["NO_PROXY"].split(","))


def test_proxy_bypass_keeps_existing_entries_and_only_applies_to_loopback(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "intranet.example")
    llm.bypass_proxy_for_loopback()
    assert os.environ["NO_PROXY"].split(",")[0] == "intranet.example" and "::1" in os.environ["NO_PROXY"]
    assert llm.is_loopback("http://127.0.0.1:8080/v1") and llm.is_loopback("http://localhost:1234") and llm.is_loopback("http://[::1]:1/v1")
    assert not llm.is_loopback("https://api.openai.com/v1") and not llm.is_loopback(None)


@pytest.mark.parametrize("message,needle", [
    ("Error code: 400 - {'error': {'code': 400, 'message': 'request (9123 tokens) exceeds the available context size (8192 tokens), try increasing it', 'type': 'exceed_context_size_error'}}", "context window"),
    ("input (9000 tokens) is larger than the max context size (8192 tokens). skipping", "context window"),
    ("Error code: 500 - image input is not supported - hint: if this is unexpected, you may need to provide the mmproj", "cannot read images"),
])
def test_llama_cpp_errors_that_retrying_cannot_fix_are_fatal_with_advice(message, needle):
    text, fatal = llm.describe_error(RuntimeError(message))
    assert fatal and needle in text


def test_transient_local_errors_stay_retryable():
    for message in ("Connection error.", "Error code: 503 - Loading model", "Server disconnected without sending a response"):
        assert llm.describe_error(RuntimeError(message))[1] is False
