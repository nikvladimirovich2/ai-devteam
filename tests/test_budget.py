"""Unit tests of the task budget (agent/app/budget.py): no network, no model. Run: python -m pytest -q tests"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "agent" / "app"))

import budget as b  # noqa: E402


def fake_prices(model, prompt_tokens, completion_tokens):
    table = {"gpt-4.1": (2e-6, 8e-6), "anthropic/claude-sonnet-4-5": (3e-6, 15e-6)}
    if model not in table:
        raise ValueError(f"This model isn't mapped yet. model={model}")
    p, c = table[model]
    return prompt_tokens * p, completion_tokens * c


def test_parse_limit_empty_or_bad_means_no_limit():
    for v in ("", None, "0", "-3", "abc", "  "):
        assert b.parse_limit(v) == 0.0, v
    assert b.parse_limit("1.5") == 1.5
    assert b.parse_limit("1,5") == 1.5
    assert b.parse_limit(" 20000 ") == 20000


def test_cost_of_known_and_unknown_models():
    assert b.cost_of("anthropic/claude-sonnet-4-5", 1000, 100, fake_prices) == 0.0045
    # 'openai/<name>' is looked up as '<name>' too
    assert b.cost_of("openai/gpt-4.1", 1000, 1000, fake_prices) == 0.01
    # self-hosted / unknown models are free, never an error
    assert b.cost_of("hosted_vllm/Qwen3-32B", 10_000, 10_000, fake_prices) == 0.0
    assert b.cost_of("ollama/qwen2.5-coder", 10, 10, fake_prices) == 0.0
    assert b.cost_of("", 10, 10, fake_prices) == 0.0
    assert b.cost_of("openai/gpt-4.1", 0, 0, fake_prices) == 0.0


def test_totals_sum_tokens_and_cost():
    s = b.totals([{"total_tokens": 100, "cost": 0.01}, {"total_tokens": 50}, {}, None, {"cost": "0.02"}])
    assert s == {"tokens": 150, "cost": 0.03}


def test_exceeded():
    s = {"tokens": 1000, "cost": 0.5}
    assert b.exceeded(s, 0, 0) == ""                  # no limits
    assert b.exceeded(s, 1.0, 0) == ""
    assert b.exceeded(s, 0.5, 0) == "usd"             # reaching the limit stops
    assert b.exceeded(s, 0, 5000) == ""
    assert b.exceeded(s, 0, 1000) == "tokens"
    assert b.exceeded({"tokens": 10**6, "cost": 0}, 1.0, 0) == ""   # free local model: only tokens can stop it
    assert b.exceeded({"tokens": 10**6, "cost": 0}, 1.0, 500_000) == "tokens"


def test_describe():
    assert b.describe({"tokens": 12345, "cost": 0.4213}) == "$0.42 · 12 345 tokens"
    assert b.describe({"tokens": 10, "cost": 0.0012}, 1, 50000) == "$0.0012 of $1.00 · 10 of 50 000 tokens"
    assert b.describe({"tokens": 0, "cost": 0}, 2, 0, "ru") == "$0.00 из $2.00 · 0 токенов"
