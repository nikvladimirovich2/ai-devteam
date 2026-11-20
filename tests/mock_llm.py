"""Mock OpenAI-compatible LLM for end-to-end smoke tests of AI DevTeam (no real model needed).
Recognises the role from the system prompt and drives real tools:
architect/backend/frontend write files, qa runs pytest, devops calls gitlab_publish.
Supports both native function calling (request has `tools`) and ReAct text mode."""
import json
import time
import uuid

from fastapi import FastAPI, Request

app = FastAPI()

PLAN = {
    "project_name": "smoke-todo", "summary": "Tiny TODO API for the smoke test.",
    "requirements": ["GET /todos returns a list"], "acceptance_criteria": ["pytest passes"],
    "needs_backend": True, "needs_frontend": True,
    "architect_brief": "FastAPI + static html", "backend_brief": "app.py", "frontend_brief": "index.html",
    "qa_brief": "run pytest", "devops_brief": "Dockerfile + CI",
}

ACTIONS = {
    "Software Architect": ("write_file", {"path": "ARCHITECTURE.md", "content": "# Architecture\n\nbackend/app.py, frontend/index.html\n"}),
    "Backend Developer": ("write_file", {"path": "backend/test_app.py",
                                         "content": "def add(a, b):\n    return a + b\n\n\ndef test_add():\n    assert add(2, 2) == 4\n"}),
    "Frontend Developer": ("write_file", {"path": "frontend/index.html", "content": "<!doctype html><h1>TODO</h1>\n"}),
    "QA Engineer": ("run_shell", {"command": "pip install -q pytest >/dev/null 2>&1; python -m pytest -q backend"}),
    "DevOps Engineer": ("publish_repository", {"project_name": "smoke-todo", "description": "AI DevTeam smoke test",
                                           "commit_message": "AI DevTeam smoke test"}),
}

FINAL = {
    "QA Engineer": 'Tests passed.\n{"passed": true, "summary": "pytest ok", "issues": []}',
}


def role_of(msgs):
    import re
    sys = " ".join(m.get("content") or "" for m in msgs if m.get("role") == "system")
    if not sys and msgs:
        sys = str(msgs[0].get("content") or "")
    m = re.search(r"You are (Team Lead|Software Architect|Backend Developer|Frontend Developer|QA Engineer|DevOps Engineer)", sys)
    return m.group(1) if m else "Team Lead"


def reply(content=None, tool_calls=None):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {"id": "mock-" + uuid.uuid4().hex[:8], "object": "chat.completion", "created": int(time.time()),
            "model": "mock", "choices": [{"index": 0, "message": msg,
                                         "finish_reason": "tool_calls" if tool_calls else "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}}


@app.post("/v1/chat/completions")
async def chat(req: Request):
    body = await req.json()
    msgs = body.get("messages", [])
    auth = req.headers.get("authorization", "")
    role = role_of(msgs)
    text = json.dumps(msgs, ensure_ascii=False)
    print(f"[mock] role={role} key=...{auth[-6:]} tools={'tools' in body} n={len(msgs)}", flush=True)
    used_tool = any(m.get("role") == "tool" for m in msgs) or "Observation:" in text

    if role == "Team Lead":
        if "Make a plan" in text:
            final = json.dumps(PLAN)
        else:
            final = "## Report\nSmoke test completed."
        return reply(final if body.get("tools") else f"Thought: done\nFinal Answer: {final}")

    if not used_tool:
        name, args = ACTIONS[role]
        if body.get("tools"):
            return reply(None, [{"id": "call_" + uuid.uuid4().hex[:8], "type": "function",
                                 "function": {"name": name, "arguments": json.dumps(args)}}])
        return reply(f"Thought: I will use a tool\nAction: {name}\nAction Input: {json.dumps(args)}")
    final = FINAL.get(role, f"{role}: done, files written and checked.")
    return reply(final if body.get("tools") else f"Thought: done\nFinal Answer: {final}")


@app.get("/v1/models")
def models():
    return {"data": [{"id": "mock", "object": "model"}]}
