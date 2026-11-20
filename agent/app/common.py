"""Shared pieces: LLM factory, event emitter, workspace tools."""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import threading
import time
from typing import Any, Optional, Type

import httpx
from pydantic import BaseModel, Field

# Import LiteLLM eagerly in the main thread, BEFORE uvicorn starts.
# CrewAI imports it lazily on first LLM() — that happens in a worker thread, while
# LiteLLM's logging filter (installed on the root logger) lazily imports its own
# modules from the event-loop thread -> importlib _DeadlockError / KeyError: 'litellm'
# and a wedged event loop. Pre-importing everything removes the race.
os.environ.setdefault("LITELLM_LOG", "ERROR")
import litellm  # noqa: E402,F401
for _m in ("litellm.types.secret_managers.main", "litellm.rust_bridge.catalog",
           "litellm.rust_bridge.diagnostics", "litellm.main"):
    try:
        __import__(_m)
    except Exception:
        pass
litellm.suppress_debug_info = True

from crewai import LLM  # noqa: E402
from crewai.tools import BaseTool  # noqa: E402

ROLE = os.environ.get("ROLE", "unknown")
LEAD_URL = os.environ.get("LEAD_URL", "http://lead:8000")
WORKSPACE = pathlib.Path(os.environ.get("WORKSPACE", "/workspace"))
SHELL_TIMEOUT = int(os.environ.get("SHELL_TIMEOUT", "300") or 300)

# ---------------------------------------------------------------- env helpers

def env(name: str, default: str = "") -> str:
    v = os.environ.get(name, "")
    return v if v.strip() else default


def env_bool(name: str) -> bool:
    return env(name, "0").lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------- LLM

_PREFIX_ROLE = {
    "lead": "LEAD", "architect": "ARCHITECT", "backend": "BACKEND",
    "frontend": "FRONTEND", "qa": "QA", "devops": "DEVOPS",
}

_CTX_CACHE: dict[str, int] = {}


def _context_window(model: str, api_key: str) -> int:
    """Context window of the model: LLM_CONTEXT_WINDOW, else vLLM /v1/models max_model_len.
    Without it CrewAI assumes 8192 tokens for unknown models and trims agent context hard."""
    forced = int(env("LLM_CONTEXT_WINDOW", "0") or 0)
    if forced > 0:
        return forced
    base = env("LLM_BASE_URL").rstrip("/")
    if not base:
        return 0
    name = model.split("/", 1)[1] if "/" in model else model
    if name in _CTX_CACHE:
        return _CTX_CACHE[name]
    val = 0
    try:
        verify = False if env_bool("LLM_SSL_VERIFY_DISABLE") else (os.environ.get("SSL_CERT_FILE") or True)
        r = httpx.get(f"{base}/models", headers={"Authorization": f"Bearer {api_key}"}, timeout=10, verify=verify)
        for m in r.json().get("data", []):
            if m.get("id") == name and m.get("max_model_len"):
                val = int(m["max_model_len"])
    except Exception:
        val = 0
    _CTX_CACHE[name] = val
    return val


def build_llm(role: str = ROLE) -> LLM:
    """Each role uses its OWN api key (<ROLE>_API_KEY) and optionally its own model."""
    p = _PREFIX_ROLE.get(role, role.upper())
    model = env(f"{p}_MODEL") or env("LLM_MODEL")
    if not model:
        raise RuntimeError("LLM_MODEL is not set in .env")
    api_key = env(f"{p}_API_KEY") or env("LLM_API_KEY")
    if not api_key:
        raise RuntimeError(f"{p}_API_KEY is not set in .env")
    kwargs: dict[str, Any] = {
        "model": model,
        "api_key": api_key,
        "temperature": float(env("LLM_TEMPERATURE", "0.2")),
        "timeout": float(env("LLM_TIMEOUT", "600")),
    }
    base_url = env("LLM_BASE_URL")
    if base_url:
        kwargs["base_url"] = base_url
    max_tokens = int(env("LLM_MAX_TOKENS", "0") or 0)
    if max_tokens > 0:
        kwargs["max_tokens"] = max_tokens
    if env_bool("LLM_SSL_VERIFY_DISABLE"):
        litellm.ssl_verify = False
    else:
        # Trust the system bundle (includes agent/certs/*.crt added at build time),
        # or an explicit LLM_CA_BUNDLE. litellm/httpx default to certifi otherwise.
        ca = env("LLM_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE", "")
        if ca and os.path.isfile(ca):
            litellm.ssl_verify = ca
    # Native CrewAI providers: openai/anthropic/gemini/azure/bedrock. Everything else -> LiteLLM.
    provider = model.split("/", 1)[0].lower() if "/" in model else ""
    if provider not in ("openai", "anthropic", "gemini", "azure", "bedrock", ""):
        kwargs["is_litellm"] = True
    llm = LLM(**kwargs)
    ctx = _context_window(model, api_key)
    if ctx:
        try:
            llm.context_window_size = ctx
        except Exception:
            pass
    # LiteLLM's model catalogue doesn't know self-hosted models (hosted_vllm/…, ollama/…)
    # and reports "no function calling" -> CrewAI falls back to text ReAct, where
    # reasoning models (Qwen3, DeepSeek-R1) often just *describe* tool use instead of
    # calling tools. vLLM with --enable-auto-tool-choice supports native tool calls.
    # LLM_FUNCTION_CALLING=auto|1|0 (auto = 1 for non-native providers).
    fc = env("LLM_FUNCTION_CALLING", "auto").lower()
    force = fc in ("1", "true", "yes", "on") or (fc == "auto" and kwargs.get("is_litellm"))
    if force:
        try:
            object.__setattr__(llm, "supports_function_calling", lambda: True)
        except Exception:
            pass
    return llm


# ---------------------------------------------------------------- events

# Process-wide (not thread-local): CrewAI may run tools in its own threads,
# and each worker container handles exactly one job at a time (lock in worker.py).
_ctx = {"task_id": "", "project_dir": str(WORKSPACE)}


def set_task_context(task_id: str, project_dir: str) -> None:
    _ctx["task_id"] = task_id
    _ctx["project_dir"] = project_dir


def current_task_id() -> str:
    return _ctx["task_id"]


def current_project_dir() -> pathlib.Path:
    return pathlib.Path(_ctx["project_dir"])


def emit(kind: str, message: str, task_id: str | None = None, role: str = ROLE, **extra: Any) -> None:
    """Send an event to the lead (web UI). Never raises."""
    payload = {
        "task_id": task_id or current_task_id(),
        "role": role,
        "kind": kind,
        "message": (message or "")[:8000],
        "ts": time.time(),
        "extra": extra,
    }
    try:
        httpx.post(f"{LEAD_URL}/api/internal/events", json=payload, timeout=5)
    except Exception:
        pass


def step_callback(step: Any) -> None:
    """CrewAI step callback -> UI event (thoughts, tool calls, observations)."""
    try:
        thought = getattr(step, "thought", "") or ""
        tool = getattr(step, "tool", "") or ""
        tool_input = getattr(step, "tool_input", "") or ""
        result = getattr(step, "result", None) or getattr(step, "output", None) or ""
        if tool:
            msg = f"🛠 {tool}({str(tool_input)[:600]})"
            if thought:
                msg = f"💭 {thought.strip()[:1200]}\n" + msg
            emit("step", msg)
        elif thought or result:
            emit("thought", (thought or str(result)).strip()[:3000])
        else:
            emit("step", str(step)[:2000])
    except Exception as e:  # pragma: no cover
        emit("step", f"(step) {e}")


# ---------------------------------------------------------------- workspace tools

def _safe_path(rel: str) -> pathlib.Path:
    root = current_project_dir().resolve()
    p = (root / (rel or ".").lstrip("/")).resolve()
    if p != root and root not in p.parents:
        raise ValueError(f"path escapes project dir: {rel}")
    return p


class _PathArg(BaseModel):
    path: str = Field(".", description="Path relative to the project root")


class _WriteArg(BaseModel):
    path: str = Field(..., description="File path relative to the project root")
    content: str = Field(..., description="Full file content")


class _ShellArg(BaseModel):
    command: str = Field(..., description="Shell command, runs in the project root (bash -lc)")


class ListDirTool(BaseTool):
    name: str = "list_files"
    description: str = "Recursively list project files (excluding .git, node_modules, venv). Argument: path."
    args_schema: Type[BaseModel] = _PathArg

    def _run(self, path: str = ".") -> str:
        base = _safe_path(path)
        if not base.exists():
            return f"(not found: {path})"
        out = []
        skip = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".pytest_cache"}
        for p in sorted(base.rglob("*")):
            if any(part in skip for part in p.relative_to(base).parts):
                continue
            if p.is_file():
                out.append(f"{p.relative_to(current_project_dir())}  ({p.stat().st_size} B)")
            if len(out) >= 400:
                out.append("... (truncated)")
                break
        return "\n".join(out) or "(empty)"


class ReadFileTool(BaseTool):
    name: str = "read_file"
    description: str = "Read a project file. Argument: path (relative to the project root)."
    args_schema: Type[BaseModel] = _PathArg

    def _run(self, path: str = ".") -> str:
        p = _safe_path(path)
        if not p.is_file():
            return f"(file not found: {path})"
        data = p.read_text(encoding="utf-8", errors="replace")
        if len(data) > 60000:
            data = data[:60000] + "\n... (truncated)"
        return data


class WriteFileTool(BaseTool):
    name: str = "write_file"
    description: str = ("Create/overwrite a project file with the full content. "
                        "Arguments: path (relative to the project root), content (whole file).")
    args_schema: Type[BaseModel] = _WriteArg

    def _run(self, path: str, content: str) -> str:
        p = _safe_path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        emit("file", f"📝 {path} ({len(content)} B)", path=path)
        return f"written {path} ({len(content)} bytes)"


class ShellTool(BaseTool):
    name: str = "run_shell"
    description: str = (
        "Run a shell command in the project root inside your container "
        "(python3, pip, node, npm, git, pytest, curl available). Returns exit code and output. "
        "Do not start long-running servers in the foreground — use timeout or & with a check."
    )
    args_schema: Type[BaseModel] = _ShellArg

    def _run(self, command: str) -> str:
        emit("shell", f"$ {command}")
        # Never hand secrets to commands the model writes (keys/tokens stay in the agent process only)
        safe_env = {k: v for k, v in os.environ.items()
                    if not any(s in k.upper() for s in ("API_KEY", "TOKEN", "PASSWORD", "SECRET"))}
        try:
            r = subprocess.run(
                ["bash", "-lc", command], cwd=str(current_project_dir()), env=safe_env,
                capture_output=True, text=True, timeout=SHELL_TIMEOUT,
            )
            out = (r.stdout + ("\n" + r.stderr if r.stderr else "")).strip()
            if len(out) > 15000:
                out = out[:5000] + "\n...\n" + out[-10000:]
            emit("shell", f"exit={r.returncode}\n{out[-3000:]}")
            return f"exit_code={r.returncode}\n{out}"
        except subprocess.TimeoutExpired:
            emit("shell", f"timeout {SHELL_TIMEOUT}s")
            return f"TIMEOUT after {SHELL_TIMEOUT}s"


def workspace_tools(with_shell: bool = True) -> list[BaseTool]:
    tools: list[BaseTool] = [ListDirTool(), ReadFileTool(), WriteFileTool()]
    if with_shell:
        tools.append(ShellTool())
    return tools


def extract_json(text: str) -> Optional[dict]:
    """Pull the first JSON object out of an LLM answer."""
    if not text:
        return None
    t = text.strip()
    if "```" in t:
        for chunk in t.split("```"):
            c = chunk.strip()
            if c.startswith("json"):
                c = c[4:].strip()
            if c.startswith("{"):
                t = c
                break
    start = t.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(t)):
            if t[i] == "{":
                depth += 1
            elif t[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(t[start:i + 1])
                    except Exception:
                        break
        start = t.find("{", start + 1)
    return None
