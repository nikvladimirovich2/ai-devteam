"""Cost budget of a task run: price of the tokens and the limit check. Pure functions (no database, no network),
so they are unit-tested without the agents (tests/test_budget.py).

Prices come from LiteLLM's model table (litellm.cost_per_token). A model LiteLLM does not know (self-hosted vLLM,
Ollama, a local server) costs $0, but its tokens still count against the token limit.
"""
from __future__ import annotations

from typing import Callable, Iterable, Optional

CostFn = Callable[..., tuple]


def parse_limit(value: object) -> float:
    """'' / '0' / garbage / negative -> 0.0 (= no limit); '1.5' or '1,5' -> 1.5."""
    try:
        v = float(str(value if value is not None else "").strip().replace(",", ".") or 0)
    except ValueError:
        return 0.0
    return v if v > 0 else 0.0


def _litellm_cost_fn() -> Optional[CostFn]:
    try:
        import litellm
        return litellm.cost_per_token
    except Exception:  # noqa: BLE001 - no litellm (unit tests) = unknown prices
        return None


def cost_of(model: str, prompt_tokens: int, completion_tokens: int, cost_fn: Optional[CostFn] = None) -> float:
    """Price in USD of one usage record; 0.0 when the model has no known price."""
    if not model or (prompt_tokens <= 0 and completion_tokens <= 0):
        return 0.0
    fn = cost_fn or _litellm_cost_fn()
    if fn is None:
        return 0.0
    names = [model]
    if model.startswith("openai/"):
        names.append(model.split("/", 1)[1])          # 'openai/gpt-4.1' is listed as 'gpt-4.1'
    for name in names:
        try:
            p, c = fn(model=name, prompt_tokens=int(prompt_tokens), completion_tokens=int(completion_tokens))
            total = float(p or 0) + float(c or 0)
            if total > 0:
                return round(total, 6)
        except Exception:  # noqa: BLE001 - "model isn't mapped yet" and friends
            continue
    return 0.0


def totals(usages: Iterable[dict]) -> dict:
    """Sum of usage records {'total_tokens', 'cost'} -> {'tokens': int, 'cost': float}."""
    tokens, cost = 0, 0.0
    for u in usages:
        tokens += int((u or {}).get("total_tokens") or 0)
        cost += float((u or {}).get("cost") or 0)
    return {"tokens": tokens, "cost": round(cost, 6)}


def exceeded(spent: dict, max_usd: float, max_tokens: float) -> str:
    """'' while within the budget, else 'usd' or 'tokens' (which limit was reached). 0 = no limit."""
    if max_usd > 0 and float(spent.get("cost") or 0) >= max_usd:
        return "usd"
    if max_tokens > 0 and int(spent.get("tokens") or 0) >= max_tokens:
        return "tokens"
    return ""


def fmt_usd(v: float) -> str:
    v = float(v or 0)
    return f"${v:.4f}" if 0 < v < 0.01 else f"${v:.2f}"


def describe(spent: dict, max_usd: float = 0, max_tokens: float = 0, lang: str = "en") -> str:
    """'$0.42 of $1.00 · 12 345 of 50 000 tokens' (limits only when set)."""
    of = " из " if lang == "ru" else " of "
    tok = "токенов" if lang == "ru" else "tokens"
    n = f"{int(spent.get('tokens') or 0):,}".replace(",", " ")
    usd = fmt_usd(spent.get("cost") or 0) + (of + fmt_usd(max_usd) if max_usd > 0 else "")
    tks = n + (of + f"{int(max_tokens):,}".replace(",", " ") if max_tokens > 0 else "") + " " + tok
    return f"{usd} · {tks}"
