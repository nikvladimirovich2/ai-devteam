"""Worker agent service (architect/backend/frontend/qa/devops).
Takes a task from the lead, runs a single-agent CrewAI Crew with its own LLM key."""
from __future__ import annotations

import json
import os
import pathlib
import threading
import time
import traceback

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from crewai import Agent, Crew, Process, Task

from common import (ROLE, WORKSPACE, build_llm, cancel_requested, current_task_id, tool_calls, emit, env,
                    install_cancel_hooks, model_of, request_cancel, require_secret, reset_cancel, set_overrides,
                    set_task_context, share_with_shell, step_callback, token_ok, ui_lang, workspace_tools)
from setup_checks import hint
from roles import COMMON_RULES, ROLES

require_secret()
os.umask(0o002)                      # files the agent writes stay editable by the shell user
share_with_shell(WORKSPACE)          # only the top level here; each project on /run
install_cancel_hooks(cancel_requested)   # Stop: the agent's next model / tool call is aborted

app = FastAPI(title=f"devteam-{ROLE}")


def from_lead(x_devteam_role: str = Header(""), x_devteam_token: str = Header("")) -> None:
    """Only the lead may give this worker jobs: it signs requests with this worker's role token.
    A command an agent runs in its shell has no token, so it cannot call /run, /publish or /ci."""
    if x_devteam_role != ROLE or not token_ok(ROLE, x_devteam_token):
        raise HTTPException(403, "internal endpoint")
_lock = threading.Lock()
_state = {"busy": False, "task_id": "", "since": 0.0}

# Settings a job may carry from the lead's setup wizard (never Git secrets: those live in devops).
_P = ROLE.upper()
CONFIG_KEYS = {"LLM_MODEL", "LLM_BASE_URL", "LLM_API_KEY", f"{_P}_API_KEY", f"{_P}_MODEL", "LLM_SSL_VERIFY_DISABLE",
               "LLM_FUNCTION_CALLING", "LLM_CONTEXT_WINDOW", "LLM_MAX_TOKENS", "UI_LANG"}

# devops only: Git settings from the wizard, in a volume only the agent user can read (0700/0600;
# the shell user "runner" cannot). A GIT_TOKEN in .env still wins over it.
GIT_STORE = pathlib.Path(os.environ.get("GIT_STORE", "/devops-data/git.json"))
GIT_KEYS = ("GIT_PROVIDER", "GIT_URL", "GIT_TOKEN", "GIT_NAMESPACE", "GIT_VISIBILITY", "GIT_SSL_VERIFY_DISABLE")


def _git_saved() -> dict:
    if ROLE != "devops":
        return {}
    try:
        return {k: v for k, v in json.loads(GIT_STORE.read_text()).items() if k in GIT_KEYS}
    except (OSError, ValueError):
        return {}


def _save_git(values: dict) -> None:
    GIT_STORE.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(GIT_STORE.parent, 0o700)
    except OSError:
        pass
    tmp = GIT_STORE.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(values, f)
    os.replace(tmp, GIT_STORE)


set_overrides(_git_saved())


def apply_config(config: dict) -> None:
    """The settings of one job: saved Git settings (devops) + what the lead sent for this role."""
    set_overrides({**_git_saved(), **{k: v for k, v in (config or {}).items() if k in CONFIG_KEYS}})


class RunReq(BaseModel):
    task_id: str
    project_dir: str
    title: str = "task"
    instruction: str
    expected_output: str = "Short report on what was done"
    context: str = ""
    config: dict = {}


def make_agent() -> Agent:
    spec = ROLES[ROLE]
    tools = workspace_tools(with_shell=True)
    if ROLE == "devops":
        from repo_tool import RepoPublishTool
        tools.append(RepoPublishTool())
    return Agent(
        role=spec["role"],
        goal=spec["goal"],
        backstory=spec["backstory"] + "\n\n" + COMMON_RULES,
        llm=build_llm(ROLE),
        tools=tools,
        allow_delegation=False,
        max_iter=int(env("AGENT_MAX_ITER", "40")),
        max_retry_limit=3,
        respect_context_window=True,
        verbose=True,
        step_callback=step_callback,
    )


class PublishReq(BaseModel):
    task_id: str
    project_dir: str
    project_name: str
    description: str = ""
    commit_message: str = "AI DevTeam"


@app.post("/publish", dependencies=[Depends(from_lead)])
def publish_direct(req: PublishReq):
    """Deterministic fallback used by the lead when the model didn't call the tool."""
    if ROLE != "devops":
        raise HTTPException(404)
    proj = pathlib.Path(req.project_dir).resolve()
    if WORKSPACE.resolve() not in proj.parents or not proj.is_dir():
        raise HTTPException(400, "bad project_dir")
    if not _lock.acquire(timeout=600):
        raise HTTPException(409, "devops busy")
    try:
        set_task_context(req.task_id, str(proj))
        from repo_tool import publish
        emit("gitlab", f"⏫ publishing {req.project_name} (directly)…")
        res = publish(req.project_name, req.description, req.commit_message)
        emit("gitlab", f"✅ pushed to {res['provider']}: {res['web_url']} ({res['branch']} @ {res['commit'][:10]})", **res)
        return {"ok": True, **res}
    except Exception as e:
        emit("error", f"publish: {e}")
        return {"ok": False, "error": str(e)}
    finally:
        _lock.release()


class CiReq(BaseModel):
    task_id: str
    project_name: str
    sha: str


@app.post("/ci", dependencies=[Depends(from_lead)])
def ci(req: CiReq):
    """Wait for CI of the pushed commit; returns status + failed job logs (devops only)."""
    if ROLE != "devops":
        raise HTTPException(404)
    set_task_context(req.task_id, _state.get("project_dir") or str(WORKSPACE))
    reset_cancel()
    from repo_tool import ci_status
    _state["ci"] = True
    try:
        return ci_status(req.project_name, req.sha)
    finally:
        _state["ci"] = False


class CancelReq(BaseModel):
    task_id: str


@app.post("/cancel", dependencies=[Depends(from_lead)])
def cancel(req: CancelReq):
    """Stop: kill the running command and abort the agent's next model / tool call (only for this task)."""
    if not req.task_id or current_task_id() != req.task_id or not (_state["busy"] or _state.get("ci")):
        return {"ok": True, "running": False}
    killed = request_cancel()
    emit("warn", "⏹ stop requested" + (f": {killed} running command(s) killed" if killed else ""), task_id=req.task_id)
    return {"ok": True, "running": True, "killed": killed}


class GitConfig(BaseModel):
    provider: str = ""
    url: str = ""
    token: str = ""          # empty = keep the saved token
    namespace: str = ""
    visibility: str = "private"
    insecure: bool = False
    save: bool = True


@app.post("/config/git", dependencies=[Depends(from_lead)])
def config_git(req: GitConfig):
    """Setup wizard: check the Git access and (when it works) keep it for publishing. devops only."""
    if ROLE != "devops":
        raise HTTPException(404)
    from repo_tool import check_access, detect_provider
    saved = _git_saved()
    url = (req.url or ("https://github.com" if req.provider == "github" else "")).strip().rstrip("/")
    values = {"GIT_PROVIDER": detect_provider(url, req.provider), "GIT_URL": url,
              "GIT_TOKEN": req.token.strip() or saved.get("GIT_TOKEN", ""), "GIT_NAMESPACE": req.namespace.strip(),
              "GIT_VISIBILITY": req.visibility if req.visibility in ("private", "internal", "public") else "private",
              "GIT_SSL_VERIFY_DISABLE": "1" if req.insecure else "0"}
    c = {"url": url, "token": values["GIT_TOKEN"], "namespace": values["GIT_NAMESPACE"],
         "insecure": req.insecure, "provider": values["GIT_PROVIDER"]}
    res = check_access(c)
    if res.get("ok") and req.save:
        _save_git(values)
        set_overrides(values)
    return res


def _git_status() -> dict:
    if ROLE != "devops":
        return {}
    from repo_tool import cfg
    c = cfg()
    return {"git": {"configured": bool(c["url"] and c["token"]), "provider": c["provider"], "url": c["url"],
                    "namespace": c["namespace"], "from_env": bool(os.environ.get("GIT_TOKEN", "").strip())}}


@app.get("/health")
def health():
    return {"role": ROLE, **_state, **_git_status()}


def usage_of(obj) -> dict:
    """Token counters from a CrewAI Crew/CrewOutput/UsageMetrics (works after errors too)."""
    if obj is None:
        return {}
    m = getattr(obj, "token_usage", None) or getattr(obj, "usage_metrics", None)
    if m is None and hasattr(obj, "calculate_usage_metrics"):
        try:
            m = obj.calculate_usage_metrics()
        except Exception:
            m = None
    if m is None:
        return {}
    d = m.model_dump() if hasattr(m, "model_dump") else dict(m)
    return {k: int(d.get(k) or 0) for k in
            ("prompt_tokens", "completion_tokens", "total_tokens", "cached_prompt_tokens",
             "reasoning_tokens", "successful_requests")}


def report_usage(req, crew, t0: float) -> dict:
    u = usage_of(crew)
    u["seconds"] = int(time.time() - t0)
    u["model"] = model_of(ROLE)
    emit("usage", f"🔢 {req.title}: {u.get('total_tokens', 0):,} tokens "
                  f"(in {u.get('prompt_tokens', 0):,} / out {u.get('completion_tokens', 0):,}, "
                  f"requests {u.get('successful_requests', 0)})".replace(",", " "),
         usage=u, title=req.title)
    return u


@app.post("/run", dependencies=[Depends(from_lead)])
def run(req: RunReq):
    proj = pathlib.Path(req.project_dir).resolve()
    if WORKSPACE.resolve() not in proj.parents:
        raise HTTPException(400, "project_dir must be under /workspace")
    if not _lock.acquire(blocking=False):
        raise HTTPException(409, f"{ROLE} is busy with {_state['task_id']}")
    try:
        _state.update(busy=True, task_id=req.task_id, since=time.time())
        apply_config(req.config)
        proj.mkdir(parents=True, exist_ok=True)
        share_with_shell(proj)
        set_task_context(req.task_id, str(proj))
        reset_cancel()
        emit("start", f"▶ {req.title}")
        description = req.instruction
        if req.context:
            description += "\n\n=== Context from the team ===\n" + req.context
        description += f"\n\nProject root (all paths are relative to it): {proj}"
        agent = make_agent()
        task = Task(description=description, expected_output=req.expected_output, agent=agent)
        crew = Crew(agents=[agent], tasks=[task], process=Process.sequential, verbose=True)
        t0 = time.time()
        result = crew.kickoff()
        text = str(getattr(result, "raw", result))
        if cancel_requested():
            emit("warn", f"⏹ {req.title}: stopped")
            return {"ok": False, "cancelled": True, "output": "CANCELLED", "tools": tool_calls(),
                    "usage": report_usage(req, crew, t0)}
        emit("done", f"✔ {req.title} ({int(time.time() - t0)}s)\n{text[:4000]}")
        return {"ok": True, "output": text, "usage": report_usage(req, crew, t0), "tools": tool_calls()}
    except Exception as e:
        if cancel_requested():          # aborted by Stop / the time limit: not an error of the agent
            emit("warn", f"⏹ {req.title}: stopped")
            return {"ok": False, "cancelled": True, "output": "CANCELLED", "tools": tool_calls(),
                    "usage": report_usage(req, locals().get("crew"), locals().get("t0", time.time()))}
        tb = traceback.format_exc()
        h = hint(e, ui_lang())
        emit("error", f"✖ {req.title}: {e}" + (f"\n{h}" if h else "") + f"\n{tb[-2000:]}")
        return {"ok": False, "output": f"ERROR: {e}", "tools": tool_calls(),
                "usage": report_usage(req, locals().get("crew"), locals().get("t0", time.time()))}
    finally:
        _state.update(busy=False, task_id="", since=0.0)
        _lock.release()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), log_level="info", loop="asyncio")
