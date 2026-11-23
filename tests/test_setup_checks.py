"""Unit tests of the setup wizard helpers (no network, no model). Run: python -m pytest -q tests"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "agent" / "app"))

import setup_checks as s  # noqa: E402


class AuthenticationError(Exception):
    pass


class ConnectError(Exception):
    pass


def test_model_id_adds_the_provider_prefix_once():
    assert s.model_id("openai", "gpt-4.1") == "openai/gpt-4.1"
    assert s.model_id("openai", "openai/gpt-4.1") == "openai/gpt-4.1"
    assert s.model_id("vllm", "Qwen3-32B") == "hosted_vllm/Qwen3-32B"
    assert s.model_id("custom", "gigachat/GigaChat-2-Max") == "gigachat/GigaChat-2-Max"


def test_errors_are_classified():
    assert s.classify(AuthenticationError("Incorrect API key provided")) == "key_invalid"
    assert s.classify(ConnectError("[Errno -2] Name or service not known")) == "unreachable"
    assert s.classify(RuntimeError("[SSL: CERTIFICATE_VERIFY_FAILED] self-signed")) == "cert"
    assert s.classify(RuntimeError("Error code: 429 rate limit")) == "rate_limited"
    assert s.classify(RuntimeError("You exceeded your current quota")) == "no_credit"
    assert s.classify(RuntimeError("model `x` does not exist")) == "model_not_found"
    assert s.classify(RuntimeError("something else")) == "unknown"


def test_every_message_has_both_languages():
    for code, m in s.MESSAGES.items():
        assert m["en"] and m["ru"], code
    assert s.message("cert", "ru").startswith("HTTPS")
    assert s.message("no-such-code", "en") == s.MESSAGES["unknown"]["en"]


def test_hint_is_empty_for_unknown_errors():
    assert s.hint(RuntimeError("weird"), "en") == ""
    assert s.hint(AuthenticationError("401"), "ru").startswith("💡 Ключ")


def test_localhost_becomes_the_host_address(monkeypatch):
    monkeypatch.delenv("HOST_GATEWAY", raising=False)
    assert s.container_url("http://localhost:11434") == "http://host.docker.internal:11434"
    assert s.container_url("http://127.0.0.1:8000/v1") == "http://host.docker.internal:8000/v1"
    assert s.container_url("https://api.openai.com/v1") == "https://api.openai.com/v1"
    assert s.container_url("http://llm.corp.local:8000/v1") == "http://llm.corp.local:8000/v1"
    assert s.container_url("") == ""
    monkeypatch.setenv("HOST_GATEWAY", "192.168.144.1")       # Windows, wslc
    assert s.container_url("http://localhost:11434") == "http://192.168.144.1:11434"
    assert s.container_url("http://host.docker.internal:11434/api") == "http://192.168.144.1:11434/api"
    assert "192.168.144.1" in s.message("local_unreachable", "ru")
    assert "OLLAMA_HOST=0.0.0.0" in s.message("local_unreachable", "en")


def test_check_llm_needs_a_model():
    assert s.check_llm("", "", "")["code"] == "missing"
