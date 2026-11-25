"""Notifications when a task run ends: Telegram (bot token + chat id) and a generic webhook (POST JSON).

Only the lead uses this. A failed notification never breaks a task: the caller writes the result into the
task's feed. Secrets (the bot token, a webhook URL with a key in it) never appear in error messages.
"""
from __future__ import annotations

import time
import urllib.parse
from typing import Any

import httpx

TELEGRAM_API = "https://api.telegram.org"     # tests point it at a local fake server
TIMEOUT = 15
TG_LIMIT = 4000                               # Telegram allows 4096 characters per message

STATUS = {
    "en": {"done": "✅ done", "done_no_push": "⚠ done, not published", "ci_failed": "❌ CI failed",
           "failed": "💥 failed", "cancelled": "⏹ cancelled", "interrupted": "⏹ interrupted",
           "budget": "💸 budget used up", "timeout": "⏱ time limit"},
    "ru": {"done": "✅ готово", "done_no_push": "⚠ готово, не опубликовано", "ci_failed": "❌ CI не прошёл",
           "failed": "💥 ошибка", "cancelled": "⏹ остановлена", "interrupted": "⏹ прервана",
           "budget": "💸 бюджет исчерпан", "timeout": "⏱ лимит времени"},
}
L = {
    "en": {"task": "AI DevTeam: task finished", "status": "Status", "repo": "Repository", "none": "not published",
           "cost": "Cost", "tokens": "tokens", "test": "AI DevTeam: test notification — it works."},
    "ru": {"task": "AI DevTeam: задача завершена", "status": "Статус", "repo": "Репозиторий", "none": "не опубликован",
           "cost": "Стоимость", "tokens": "токенов", "test": "AI DevTeam: тестовое уведомление — всё работает."},
}


def status_text(status: str, lang: str = "en") -> str:
    return STATUS.get(lang, STATUS["en"]).get(status, status)


def build(task: dict, spent: dict | None = None, lang: str = "en", summary_len: int = 600) -> tuple[str, dict]:
    """(plain text for chats, JSON payload for the webhook) of a finished task run."""
    lang = lang if lang in L else "en"
    t = L[lang]
    repo = task.get("gitlab_url") or ""
    summary = " ".join((task.get("result") or "").split())
    if len(summary) > summary_len:
        summary = summary[:summary_len].rstrip() + "…"
    spent = spent or {}
    cost, tokens = float(spent.get("cost") or 0), int(spent.get("tokens") or 0)
    lines = [f"🤖 {t['task']}", f"«{task.get('title') or task.get('id', '')}»",
             f"{t['status']}: {status_text(task.get('status', ''), lang)}",
             f"{t['repo']}: {repo or t['none']}"]
    if tokens or cost:
        lines.append(f"{t['cost']}: ${cost:.2f} · {tokens:,} {t['tokens']}".replace(",", " "))
    if summary:
        lines += ["", summary]
    text = "\n".join(lines)
    payload = {"event": "task_finished", "task_id": task.get("id", ""), "title": task.get("title", ""),
               "status": task.get("status", ""), "status_text": status_text(task.get("status", ""), lang),
               "project": task.get("project_name", ""), "repository": repo, "cost_usd": round(cost, 6),
               "tokens": tokens, "summary": summary, "finished_at": int(time.time()),
               "text": text}                  # "text": Slack / Mattermost / Rocket.Chat incoming webhooks show it
    return text, payload


def _clean(err: Exception | str, *secrets: str) -> str:
    s = str(err)
    for x in secrets:
        if x:
            s = s.replace(x, "***")
            s = s.replace(urllib.parse.quote(x, safe=""), "***")
    return s[:300]


def send_telegram(token: str, chat_id: str, text: str, timeout: float = TIMEOUT) -> None:
    """Raises RuntimeError with a readable reason (the token is never in it)."""
    url = f"{TELEGRAM_API.rstrip('/')}/bot{token}/sendMessage"
    try:
        r = httpx.post(url, json={"chat_id": chat_id, "text": text[:TG_LIMIT], "disable_web_page_preview": True},
                       timeout=timeout)
    except httpx.HTTPError as e:
        raise RuntimeError(_clean(f"{type(e).__name__}: {e}", token)) from None
    try:
        data = r.json()
    except ValueError:
        data = {}
    if r.status_code != 200 or not data.get("ok"):
        raise RuntimeError(_clean(f"HTTP {r.status_code}: {data.get('description') or r.text[:200]}", token))


def send_webhook(url: str, payload: dict, timeout: float = TIMEOUT) -> None:
    try:
        r = httpx.post(url, json=payload, timeout=timeout)
    except httpx.HTTPError as e:
        raise RuntimeError(_clean(f"{type(e).__name__}: {e}", url)) from None
    if r.status_code >= 300:
        raise RuntimeError(_clean(f"HTTP {r.status_code}: {r.text[:200]}", url))


def channels(cfg: dict) -> list[str]:
    out = []
    if cfg.get("telegram_token") and cfg.get("telegram_chat_id"):
        out.append("telegram")
    if cfg.get("webhook_url"):
        out.append("webhook")
    return out


def send(cfg: dict, text: str, payload: dict) -> dict[str, str]:
    """Send to every configured channel. {channel: '' (sent) | error}. Never raises."""
    res: dict[str, str] = {}
    for ch in channels(cfg):
        try:
            if ch == "telegram":
                send_telegram(cfg["telegram_token"], cfg["telegram_chat_id"], text)
            else:
                send_webhook(cfg["webhook_url"], payload)
            res[ch] = ""
        except Exception as e:  # noqa: BLE001
            res[ch] = _clean(e, cfg.get("telegram_token", ""), cfg.get("webhook_url", "")) or type(e).__name__
    return res


def probe_message(lang: str = "en") -> tuple[str, dict[str, Any]]:
    text = L.get(lang, L["en"])["test"]
    return text, {"event": "test", "text": text, "finished_at": int(time.time())}


def webhook_host(url: str) -> str:
    """What the wizard may show of a saved webhook URL (it can contain a key): the host only."""
    try:
        return urllib.parse.urlsplit(url).hostname or ""
    except ValueError:
        return ""
