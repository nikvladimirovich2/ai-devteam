"""First-run setup: model provider presets, connection checks and human-readable errors.

Used by the lead (model checks, the web wizard) and by devops (Git checks, in repo_tool). Every
check returns {"ok": bool, "code": str, "detail": str, ...}; the UI shows message(code, lang)
plus the technical detail, so a person who is not a developer knows what to do next.
"""
from __future__ import annotations

import json
from typing import Any

import httpx

# ---------------------------------------------------------------- providers

# key: shown in the wizard. prefix: LiteLLM model prefix. base: API address (None = the provider's
# default). models: how to list models ("openai" = GET {base}/models). key_url: where to get a key.
PROVIDERS: dict[str, dict[str, Any]] = {
    "openai": {"name": "OpenAI", "prefix": "openai/", "base": "https://api.openai.com/v1", "models": "openai",
               "key_url": "https://platform.openai.com/api-keys", "needs_key": True},
    "anthropic": {"name": "Anthropic (Claude)", "prefix": "anthropic/", "base": None, "models": "anthropic",
                  "key_url": "https://console.anthropic.com/settings/keys", "needs_key": True},
    "gemini": {"name": "Google Gemini", "prefix": "gemini/", "base": None, "models": "gemini",
               "key_url": "https://aistudio.google.com/apikey", "needs_key": True},
    "openrouter": {"name": "OpenRouter (many models, one key)", "prefix": "openai/",
                   "base": "https://openrouter.ai/api/v1", "models": "openai",
                   "key_url": "https://openrouter.ai/keys", "needs_key": True},
    "deepseek": {"name": "DeepSeek", "prefix": "openai/", "base": "https://api.deepseek.com/v1", "models": "openai",
                 "key_url": "https://platform.deepseek.com/api_keys", "needs_key": True},
    "ollama": {"name": "Ollama (own server)", "prefix": "ollama/", "base": "http://host.docker.internal:11434",
               "models": "ollama", "key_url": "", "needs_key": False},
    "vllm": {"name": "vLLM / OpenAI-compatible (own server)", "prefix": "hosted_vllm/",
             "base": "http://llm-server:8000/v1", "models": "openai", "key_url": "", "needs_key": False},
    "custom": {"name": "Other (LiteLLM model name)", "prefix": "", "base": "", "models": "", "key_url": "",
               "needs_key": False},
}


def model_id(provider: str, model: str) -> str:
    """Full LiteLLM model name: the provider prefix + the model, unless the user typed a prefix."""
    p = PROVIDERS.get(provider, PROVIDERS["custom"])["prefix"]
    model = model.strip()
    if not p or model.startswith(p):
        return model
    return p + model


# ---------------------------------------------------------------- messages

MESSAGES: dict[str, dict[str, str]] = {
    "ok": {"en": "Connected.", "ru": "Подключено."},
    "key_invalid": {"en": "The key was rejected. Copy it again from the provider's site (no spaces) and check that it is active.",
                    "ru": "Ключ не принят. Скопируйте его заново с сайта провайдера (без пробелов) и проверьте, что он активен."},
    "no_credit": {"en": "The provider refused because of the balance or limits. Top up the account or check the plan.",
                  "ru": "Провайдер отказал из-за баланса или лимитов. Пополните счёт или проверьте тариф."},
    "rate_limited": {"en": "Too many requests right now. Wait a minute and try again.",
                     "ru": "Слишком много запросов. Подождите минуту и попробуйте снова."},
    "model_not_found": {"en": "This model is not available for the key. Pick another one from the list.",
                        "ru": "Эта модель недоступна для ключа. Выберите другую из списка."},
    "unreachable": {"en": "The server does not answer. Check the address, the internet connection or the VPN.",
                    "ru": "Сервер не отвечает. Проверьте адрес, интернет или VPN."},
    "cert": {"en": "The server's HTTPS certificate is not trusted. Put the company CA certificate into the certs folder and restart.",
             "ru": "HTTPS-сертификат сервера не доверенный. Положите сертификат компании в папку certs и перезапустите."},
    "no_tools": {"en": "The model answered but does not call tools. The team needs a model with tool calling (function calling).",
                 "ru": "Модель отвечает, но не вызывает инструменты. Команде нужна модель с вызовом инструментов (function calling)."},
    "bad_request": {"en": "The provider rejected the request. Check the model name and the address.",
                    "ru": "Провайдер отклонил запрос. Проверьте название модели и адрес."},
    "missing": {"en": "Fill in the required fields.", "ru": "Заполните обязательные поля."},
    "token_invalid": {"en": "The Git token was rejected. Create a new one with the rights listed below.",
                      "ru": "Токен Git не принят. Создайте новый с правами, указанными ниже."},
    "namespace_not_found": {"en": "The group / organization was not found or the token has no access to it.",
                            "ru": "Группа или организация не найдена, или у токена нет к ней доступа."},
    "git_rights": {"en": "The token works but cannot create repositories. Give it the rights listed below.",
                   "ru": "Токен работает, но не может создавать репозитории. Дайте ему права, указанные ниже."},
    "devops_down": {"en": "The DevOps agent is not running. Restart the team and try again.",
                    "ru": "Агент DevOps не запущен. Перезапустите команду и попробуйте снова."},
    "context_too_long": {"en": "The task no longer fits into the model's context. Use a model with a bigger context or split the task.",
                         "ru": "Задача не помещается в контекст модели. Возьмите модель с большим контекстом или разделите задачу."},
    "unknown": {"en": "Something went wrong. The technical details are below.",
                "ru": "Что-то пошло не так. Технические подробности ниже."},
}


def message(code: str, lang: str = "en") -> str:
    m = MESSAGES.get(code, MESSAGES["unknown"])
    return m.get(lang) or m["en"]


def classify(e: BaseException) -> str:
    """Error code for an exception from LiteLLM / httpx / CrewAI (by type name and text)."""
    name = type(e).__name__.lower()
    text = str(e).lower()
    if "certificate" in text or "ssl" in name or "certificate_verify_failed" in text:
        return "cert"
    if "authentication" in name or "401" in text or "invalid api key" in text or "incorrect api key" in text \
            or "invalid x-api-key" in text or "api key not valid" in text:
        return "key_invalid"
    if "insufficient" in text or "quota" in text or "credit" in text or "billing" in text or "402" in text:
        return "no_credit"
    if "ratelimit" in name or "rate limit" in text or "429" in text:
        return "rate_limited"
    if "contextwindow" in name or "context length" in text or "maximum context" in text or "context_length" in text:
        return "context_too_long"
    if "notfound" in name or "404" in text or "does not exist" in text or "model_not_found" in text:
        return "model_not_found"
    if "connect" in name or "timeout" in name or "connection" in text or "name or service not known" in text \
            or "nodename nor servname" in text or "timed out" in text:
        return "unreachable"
    if "badrequest" in name or "400" in text:
        return "bad_request"
    return "unknown"


def hint(e: BaseException | str, lang: str = "en") -> str:
    """A one-line explanation for an error shown in the task feed, or '' when nothing specific is known."""
    code = classify(e if isinstance(e, BaseException) else RuntimeError(e))
    return "" if code == "unknown" else "💡 " + message(code, lang)


def _fail(code: str, detail: Any = "", **extra: Any) -> dict:
    return {"ok": False, "code": code, "detail": str(detail)[:600], **extra}


# ---------------------------------------------------------------- model checks

def list_models(provider: str, base_url: str, api_key: str, verify: Any = True) -> dict:
    """Model names the key can use, for the wizard's drop-down. Empty list when not supported."""
    p = PROVIDERS.get(provider, PROVIDERS["custom"])
    kind = p["models"]
    base = (base_url or p["base"] or "").rstrip("/")
    try:
        if kind == "openai":
            r = httpx.get(f"{base}/models", headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
                          timeout=20, verify=verify)
            if r.status_code in (401, 403):
                return _fail("key_invalid", f"HTTP {r.status_code}")
            r.raise_for_status()
            names = sorted(m["id"] for m in r.json().get("data", []) if m.get("id"))
        elif kind == "anthropic":
            r = httpx.get("https://api.anthropic.com/v1/models?limit=100", timeout=20,
                          headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"})
            if r.status_code in (401, 403):
                return _fail("key_invalid", f"HTTP {r.status_code}")
            r.raise_for_status()
            names = [m["id"] for m in r.json().get("data", [])]
        elif kind == "gemini":
            r = httpx.get("https://generativelanguage.googleapis.com/v1beta/models", params={"key": api_key}, timeout=20)
            if r.status_code in (400, 401, 403):
                return _fail("key_invalid", f"HTTP {r.status_code}")
            r.raise_for_status()
            names = sorted(m["name"].split("/", 1)[-1] for m in r.json().get("models", [])
                           if "generateContent" in m.get("supportedGenerationMethods", []))
        elif kind == "ollama":
            r = httpx.get(f"{base}/api/tags", timeout=20, verify=verify)
            r.raise_for_status()
            names = sorted(m["name"] for m in r.json().get("models", []))
        else:
            names = []
    except Exception as e:  # noqa: BLE001 - every failure becomes a message for the user
        return _fail(classify(e), e)
    return {"ok": True, "code": "ok", "models": names}


PING_TOOL = {"type": "function", "function": {
    "name": "devteam_ping", "description": "Confirms that tool calling works. Call it with ok=true.",
    "parameters": {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}}}


def check_llm(model: str, base_url: str, api_key: str, ssl_verify: Any = True) -> dict:
    """One small request with a tool: the key works, the model exists and it calls tools."""
    if not model:
        return _fail("missing", "model")
    import litellm
    kwargs: dict[str, Any] = {"model": model, "api_key": api_key or "none", "timeout": 90, "max_tokens": 200,
                              "tools": [PING_TOOL], "tool_choice": "auto",
                              "messages": [{"role": "user", "content": "Call the devteam_ping tool with ok=true. Do not answer with text."}]}
    if base_url:
        kwargs["api_base"] = base_url
    if ssl_verify is not True:
        kwargs["ssl_verify"] = ssl_verify
    try:
        r = litellm.completion(**kwargs)
    except Exception as e:  # noqa: BLE001
        return _fail(classify(e), e)
    msg = r.choices[0].message
    calls = getattr(msg, "tool_calls", None) or []
    if not calls:
        return _fail("no_tools", (msg.content or "")[:300], answered=True)
    try:
        args = json.loads(calls[0].function.arguments or "{}")
    except Exception:  # noqa: BLE001
        args = {}
    return {"ok": True, "code": "ok", "detail": f"tool call {calls[0].function.name}({args})"}
