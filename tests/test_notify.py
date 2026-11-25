"""Unit tests of the notifications (agent/app/notify.py) against a local fake HTTP server: no real Telegram.
Run: python -m pytest -q tests"""
import json
import pathlib
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "agent" / "app"))

import notify as n  # noqa: E402

TOKEN = "123456:SECRET-bot-token"


@pytest.fixture()
def server():
    """Fake Telegram Bot API + webhook receiver. Records every request; /fail answers 500."""
    got = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            got.append((self.path, body))
            if self.path.startswith("/fail"):
                self.send_response(500); self.end_headers(); self.wfile.write(b"boom")
                return
            if self.path.startswith("/bot") and not body.get("chat_id"):
                out = {"ok": False, "description": "Bad Request: chat not found"}
                self.send_response(400)
            else:
                out = {"ok": True, "result": {}}
                self.send_response(200)
            self.send_header("Content-Type", "application/json"); self.end_headers()
            self.wfile.write(json.dumps(out).encode())

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    yield base, got
    srv.shutdown()


TASK = {"id": "20261125-1", "title": "URL shortener", "status": "done", "project_name": "url-shortener",
        "gitlab_url": "https://github.com/acme/url-shortener", "result": "## Report\nAll good. " + "x" * 2000}


def test_message_has_title_status_repo_cost_and_a_short_summary():
    text, payload = n.build(TASK, {"tokens": 123456, "cost": 1.234}, "en")
    assert "URL shortener" in text and "✅ done" in text
    assert "https://github.com/acme/url-shortener" in text
    assert "$1.23 · 123 456 tokens" in text
    assert len(payload["summary"]) <= 601 and payload["summary"].startswith("## Report All good.")
    assert payload["status"] == "done" and payload["repository"] == TASK["gitlab_url"] and payload["cost_usd"] == 1.234
    text_ru, _ = n.build({**TASK, "status": "budget", "gitlab_url": ""}, None, "ru")
    assert "бюджет исчерпан" in text_ru and "не опубликован" in text_ru and "Стоимость" not in text_ru


def test_telegram_and_webhook_are_sent(server, monkeypatch):
    base, got = server
    monkeypatch.setattr(n, "TELEGRAM_API", base)
    text, payload = n.build(TASK, {"tokens": 10, "cost": 0}, "en")
    res = n.send({"telegram_token": TOKEN, "telegram_chat_id": "-100500", "webhook_url": base + "/hook?key=abc"},
                 text, payload)
    assert res == {"telegram": "", "webhook": ""}
    (p1, b1), (p2, b2) = got
    assert p1 == f"/bot{TOKEN}/sendMessage" and b1["chat_id"] == "-100500" and "URL shortener" in b1["text"]
    assert p2 == "/hook?key=abc" and b2["event"] == "task_finished" and b2["task_id"] == TASK["id"]


def test_failures_are_reported_without_secrets(server, monkeypatch):
    base, got = server
    monkeypatch.setattr(n, "TELEGRAM_API", base + "/fail")
    res = n.send({"telegram_token": TOKEN, "telegram_chat_id": "1", "webhook_url": base + "/fail/hook?key=s3cr3t"},
                 *n.probe_message("en"))
    assert res["telegram"].startswith("HTTP 500") and res["webhook"].startswith("HTTP 500")
    assert all(TOKEN not in v and "s3cr3t" not in v for v in res.values())
    # Telegram's own error text is passed on
    monkeypatch.setattr(n, "TELEGRAM_API", base)
    with pytest.raises(RuntimeError, match="chat not found"):
        n.send_telegram(TOKEN, "", "x")
    # nothing listens there: a readable error, no token in it
    monkeypatch.setattr(n, "TELEGRAM_API", "http://127.0.0.1:9")
    res = n.send({"telegram_token": TOKEN, "telegram_chat_id": "1"}, "x", {})
    assert res["telegram"] and TOKEN not in res["telegram"]


def test_nothing_configured_sends_nothing():
    assert n.channels({}) == [] and n.send({"telegram_token": TOKEN}, "x", {}) == {}
    assert n.webhook_host("https://hooks.slack.com/services/T0/B0/XYZ") == "hooks.slack.com"
