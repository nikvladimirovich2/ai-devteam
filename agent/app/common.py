"""Shared pieces: LLM factory, event emitter, workspace tools."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import pathlib
import re
import signal
import subprocess
import threading
import time
from typing import Any, Callable, Optional, Type

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


# ---------------------------------------------------------------- text tool-call fallback
#
# Some self-hosted servers (observed with vLLM's --tool-call-parser hermes + Qwen3, see
# ai-devteam-test-report.md 2026-11-26) intermittently return a well-formed tool call as plain
# text instead of a structured tool_calls entry, e.g.:
#   <tool_call>
#   {"name": "write_file", "arguments": {"path": "ARCHITECTURE.md", "content": "..."}}
#   </tool_call>
# CrewAI's LLM._handle_non_streaming_response() then treats it as the agent's final answer: no
# tool runs, the lead sees "no files changed and no tools called" and the whole run fails, even
# though the SAME prompt against the SAME model often returns a proper tool call a moment later.
# Recovering it here (where every completion call passes through) covers every role and every
# code path (sync/async) without touching CrewAI's prompting or AgentExecutor loop.
_TOOL_CALL_TAG = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)
_recovered_text_tool_calls = 0


def _recover_text_tool_call(response: Any) -> None:
    """If choices[0].message has no tool_calls but its text contains a <tool_call>{...}</tool_call>
    block, parse the first one and set it as a real tool_calls entry, in place. Only the first
    one matters: CrewAI's _handle_tool_call only ever executes tool_calls[0] (a real native call
    is one tool per turn too), so the agent loop asks again for the next step as usual."""
    global _recovered_text_tool_calls
    try:
        choices = getattr(response, "choices", None)
        if not choices:
            return
        message = choices[0].message
        if getattr(message, "tool_calls", None):
            return
        content = getattr(message, "content", None) or ""
        m = _TOOL_CALL_TAG.search(content)
        if not m:
            return
        call = json.loads(m.group(1))
        name = call.get("name") or call.get("function", {}).get("name")
        args = call.get("arguments", call.get("function", {}).get("arguments", {}))
        if not name:
            return
        if not isinstance(args, str):
            args = json.dumps(args)
        from litellm.types.utils import ChatCompletionMessageToolCall, Function
        message.tool_calls = [ChatCompletionMessageToolCall(
            id=f"recovered-{_recovered_text_tool_calls}", type="function",
            function=Function(name=name, arguments=args),
        )]
        _recovered_text_tool_calls += 1
        print(f"[tool-call-fallback] recovered a text tool call: {name} (total so far: {_recovered_text_tool_calls})", flush=True)
        try:
            emit("warn", f"⚠ the model wrote a tool call as text instead of calling it; recovered: {name}")
        except Exception:
            pass
    except Exception as e:  # noqa: BLE001 - never let the fallback itself break a call
        print(f"[tool-call-fallback] could not recover a text tool call: {e}", flush=True)


def _patch_text_tool_call_fallback() -> None:
    """Wrap litellm.completion/acompletion once so every CrewAI LLM call (every role) benefits."""
    if getattr(litellm, "_devteam_tool_call_fallback", False):
        return
    litellm._devteam_tool_call_fallback = True
    _orig_completion = litellm.completion
    _orig_acompletion = litellm.acompletion

    def _completion(*args: Any, **kwargs: Any) -> Any:
        response = _orig_completion(*args, **kwargs)
        _recover_text_tool_call(response)
        return response

    async def _acompletion(*args: Any, **kwargs: Any) -> Any:
        response = await _orig_acompletion(*args, **kwargs)
        _recover_text_tool_call(response)
        return response

    litellm.completion = _completion
    litellm.acompletion = _acompletion


_patch_text_tool_call_fallback()
litellm.suppress_debug_info = True

from crewai import LLM  # noqa: E402
from crewai.tools import BaseTool  # noqa: E402

ROLE = os.environ.get("ROLE", "unknown")
LEAD_URL = os.environ.get("LEAD_URL", "http://lead:8000")
WORKSPACE = pathlib.Path(os.environ.get("WORKSPACE", "/workspace"))
SHELL_TIMEOUT = int(os.environ.get("SHELL_TIMEOUT", "300") or 300)   # one command; it is killed with its children
# Commands written by the model run as this separate, unprivileged user (see ShellTool). It cannot
# read the agent process (/proc/<pid>/environ, memory), where the API key and tokens live.
# Empty = run as the agent user (only for running the code outside the image, e.g. unit tests).
SHELL_USER = os.environ.get("SHELL_USER", "runner")

# ---------------------------------------------------------------- internal auth
# The services talk to each other over plain HTTP inside the compose network. Every internal
# request carries a per-role token derived from INTERNAL_SECRET, so that nothing else on that
# network (in particular a command an agent runs in its shell) can post events in a role's name
# or ask DevOps to publish. A worker only ever holds ITS OWN token; the lead holds the secret.
_SECRET = os.environ.pop("INTERNAL_SECRET", "")
if ROLE == "lead":
    os.environ["INTERNAL_SECRET"] = _SECRET   # the lead derives every role's token
MIN_SECRET_LEN = 32
_SECRET_OK = len(_SECRET) >= MIN_SECRET_LEN   # checked before a worker drops the secret


def require_secret() -> None:
    if not _SECRET_OK:
        raise SystemExit(
            f"INTERNAL_SECRET is missing or shorter than {MIN_SECRET_LEN} characters. "
            "Generate one and put it into .env:  INTERNAL_SECRET=$(openssl rand -hex 32)")


def role_token(role: str) -> str:
    return hmac.new(_SECRET.encode(), f"devteam-internal:{role}".encode(), hashlib.sha256).hexdigest()


# A worker keeps only its own token; the secret itself is dropped from its memory.
_OWN_TOKEN = role_token(ROLE) if _SECRET_OK else ""
if ROLE != "lead":
    _SECRET = ""


def internal_headers(role: str) -> dict:
    """Headers for an internal request made AS [role] (worker -> lead) or TO [role] (lead -> worker)."""
    token = _OWN_TOKEN if role == ROLE else role_token(role)
    return {"X-Devteam-Role": role, "X-Devteam-Token": token}


def token_ok(role: str, token: str) -> bool:
    if not token or not role:
        return False
    expected = _OWN_TOKEN if role == ROLE else (role_token(role) if _SECRET else "")
    return bool(expected) and hmac.compare_digest(expected, token)

# ---------------------------------------------------------------- env helpers

# Settings from the web setup wizard. The lead keeps them in its database and sends each worker
# what it needs with every job; devops keeps its Git settings in its own volume. A value set in
# .env (the container environment) always wins, so existing installations keep working unchanged.
# They are NOT in os.environ, so commands the agents run never see them (see _shell_env).
_overrides: dict[str, str] = {}
_overrides_lock = threading.Lock()


def set_overrides(values: dict[str, str], replace: bool = True) -> None:
    clean = {str(k): str(v) for k, v in values.items() if v is not None and str(v) != ""}
    with _overrides_lock:
        if replace:
            _overrides.clear()
        _overrides.update(clean)


def env(name: str, default: str = "") -> str:
    v = os.environ.get(name, "")
    if v.strip():
        return v
    o = _overrides.get(name, "")
    return o if o.strip() else default


def ui_lang() -> str:
    """Language of the hints in the feed: chosen in the setup wizard (UI_LANG), default English."""
    return "ru" if env("UI_LANG", "en").lower().startswith("ru") else "en"


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
    from setup_checks import container_url
    base = container_url(env("LLM_BASE_URL").rstrip("/"))
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


def model_of(role: str = ROLE) -> str:
    """The model a role works with: its own <ROLE>_MODEL, else the team's LLM_MODEL."""
    p = _PREFIX_ROLE.get(role, role.upper())
    return env(f"{p}_MODEL") or env("LLM_MODEL")


def build_llm(role: str = ROLE) -> LLM:
    """Each role uses its OWN api key (<ROLE>_API_KEY) and optionally its own model."""
    p = _PREFIX_ROLE.get(role, role.upper())
    model = env(f"{p}_MODEL") or env("LLM_MODEL")
    if not model:
        raise RuntimeError("the model is not set: open ⚙ Settings in the web interface (or LLM_MODEL in .env)")
    # one key for the whole team (LLM_API_KEY) or a personal key per role (<ROLE>_API_KEY).
    # Self-hosted servers often need no key at all.
    api_key = env(f"{p}_API_KEY") or env("LLM_API_KEY") or "none"
    kwargs: dict[str, Any] = {
        "model": model,
        "api_key": api_key,
        "temperature": float(env("LLM_TEMPERATURE", "0.2")),
        "timeout": float(env("LLM_TIMEOUT", "600")),     # one model request, seconds
    }
    litellm.request_timeout = kwargs["timeout"]           # the LiteLLM path uses the global default too
    from setup_checks import container_url
    base_url = container_url(env("LLM_BASE_URL"))    # localhost -> the host, see setup_checks
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
_tool_calls: dict[str, int] = {}   # tool name -> calls in the current job (the lead's proof of real work)


def set_task_context(task_id: str, project_dir: str) -> None:
    _ctx["task_id"] = task_id
    _ctx["project_dir"] = project_dir
    _tool_calls.clear()


def count_tool(name: str) -> None:
    if cancel_requested():
        raise RuntimeError("the task was stopped (Stop / time limit): do not call more tools, finish now")
    _tool_calls[name] = _tool_calls.get(name, 0) + 1


# ---------------------------------------------------------------- stop inside a step
# The lead asks a worker to stop (POST /cancel): the running shell command is killed with its children, every
# further tool call fails and the next model call is aborted (CrewAI before-hooks raise HookAborted), so the
# step ends within seconds. A model request already in flight is not interrupted: it finishes or hits LLM_TIMEOUT.
_cancel_evt = threading.Event()
_shell_procs: set = set()            # running ShellTool processes (session leaders)
_shell_lock = threading.Lock()


def cancel_requested() -> bool:
    return _cancel_evt.is_set()


def reset_cancel() -> None:
    _cancel_evt.clear()


def request_cancel() -> int:
    """Stop the current job: flag + kill the running shell commands. Returns how many commands were killed."""
    _cancel_evt.set()
    with _shell_lock:
        procs = list(_shell_procs)
    for p in procs:
        kill_tree(p)
    return len(procs)


def kill_tree(p: subprocess.Popen) -> None:
    """Kill a ShellTool command with everything it started. The command runs in its own session (start_new_session),
    as the shell user: the agent user may not signal those processes, so the kill itself runs as that user too."""
    sid = p.pid
    if SHELL_USER:
        try:
            subprocess.run(["sudo", "-n", "-u", SHELL_USER, "--", "/usr/bin/pkill", "-KILL", "-s", str(sid)],
                           capture_output=True, timeout=15)
        except Exception:  # noqa: BLE001
            pass
    try:
        os.killpg(sid, signal.SIGKILL)          # sudo itself / the command when SHELL_USER is empty
    except (OSError, AttributeError):
        pass
    try:
        p.kill()
    except OSError:
        pass


def install_cancel_hooks(is_cancelled: Callable[[], bool]) -> bool:
    """Abort the agent's next model / tool call once [is_cancelled]() is true (CrewAI global hooks)."""
    try:
        from crewai.hooks import HookAborted, register_before_llm_call_hook, register_before_tool_call_hook
    except Exception:  # noqa: BLE001 - an older CrewAI without hooks: tools still refuse to run
        return False

    def stop_llm(_ctx: Any) -> None:
        if is_cancelled():
            raise HookAborted("the task was stopped (Stop / time limit)", source="devteam")

    def stop_tool(_ctx: Any) -> None:
        if is_cancelled():
            raise HookAborted("the task was stopped (Stop / time limit)", source="devteam")

    register_before_llm_call_hook(stop_llm)
    register_before_tool_call_hook(stop_tool)
    return True


def tool_calls() -> dict[str, int]:
    return dict(_tool_calls)


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
        httpx.post(f"{LEAD_URL}/api/internal/events", json=payload, timeout=5, headers=internal_headers(role))
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
        count_tool(self.name)
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
        count_tool(self.name)
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
        count_tool(self.name)
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
        count_tool(self.name)
        emit("shell", f"$ {command}")
        code, out, why = run_command(command, SHELL_TIMEOUT)
        if len(out) > 15000:
            out = out[:5000] + "\n...\n" + out[-10000:]
        if why == "timeout":
            emit("shell", f"exit={code}\n⏱ timeout {SHELL_TIMEOUT}s: the command and its child processes were killed"
                          f"\n{out[-2000:]}")
            return (f"exit_code={code}\nTIMEOUT: the command was still running after {SHELL_TIMEOUT}s (SHELL_TIMEOUT) "
                    "and was killed together with its child processes. Output so far:\n"
                    f"{out}\n"
                    "Hint: start servers / watchers in the background (`cmd > server.log 2>&1 &`) and check them "
                    "with curl; wrap commands that may hang with `timeout 120 cmd`; never wait for user input.")
        if why == "cancelled":
            emit("shell", f"exit={code}\n⏹ the task was stopped: the command was killed")
            return f"exit_code={code}\nCANCELLED: the task was stopped, the command was killed. Do not continue."
        emit("shell", f"exit={code}\n{out[-3000:]}")
        return f"exit_code={code}\n{out}"


def run_command(command: str, timeout: float) -> tuple[int, str, str]:
    """Run a model-written command as the shell user, in its own session so that it can be killed with all its
    children. Returns (exit code, stdout+stderr, '' | 'timeout' | 'cancelled'). Exit code 124 = timeout (like
    coreutils timeout), 130 = cancelled."""
    if cancel_requested():
        return 130, "", "cancelled"
    p = subprocess.Popen(shell_argv(command), cwd=str(current_project_dir()), env=_shell_env(),
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True,
                         start_new_session=True)
    with _shell_lock:
        _shell_procs.add(p)
    deadline, why = time.monotonic() + timeout, ""
    try:
        while True:
            try:
                so, se = p.communicate(timeout=1)
                break
            except subprocess.TimeoutExpired:
                if cancel_requested():
                    why = "cancelled"
                elif time.monotonic() > deadline:
                    why = "timeout"
                if why:
                    kill_tree(p)
                    try:
                        so, se = p.communicate(timeout=10)
                    except subprocess.TimeoutExpired:   # a detached process still holds the pipe: give up on it
                        so, se = "", ""
                    break
    finally:
        with _shell_lock:
            _shell_procs.discard(p)
    if not why and cancel_requested() and p.returncode != 0:
        why = "cancelled"                       # request_cancel() killed it while we were waiting
    out = ((so or "") + ("\n" + se if se else "")).strip()
    code = {"timeout": 124, "cancelled": 130}.get(why, p.returncode if p.returncode is not None else -1)
    return code, out, why


def _shell_env() -> dict:
    """Environment for model-written commands: never keys, tokens, passwords or secrets."""
    return {k: v for k, v in os.environ.items()
            if not any(s in k.upper() for s in ("API_KEY", "TOKEN", "PASSWORD", "SECRET"))}


def shell_argv(command: str) -> list[str]:
    """Run [command] as SHELL_USER via sudo (a sudoers rule allows exactly this), with a clean
    environment and group-writable files, so the agent and the shell can both edit the project.
    Only the variables from _shell_env() are passed; they are not secret, so seeing them in the
    process list is harmless."""
    script = "umask 002; " + command
    if not SHELL_USER:
        return ["bash", "-lc", script]
    env = _shell_env()
    env.update(HOME=f"/home/{SHELL_USER}", USER=SHELL_USER, LOGNAME=SHELL_USER)
    return ["sudo", "-n", "-u", SHELL_USER, "--", "/usr/bin/env", "-i",
            *[f"{k}={v}" for k, v in env.items()], "bash", "-lc", script]


def share_with_shell(path: pathlib.Path) -> None:
    """Make the agent's own files under [path] group-writable for the shell user (shared group).
    Needed for workspaces created by older versions; cheap no-op for new ones."""
    try:
        gid = os.stat(WORKSPACE).st_gid
        uid = os.getuid()
        for p in [path, *path.rglob("*")]:
            try:
                st = p.lstat()
                if st.st_uid != uid or p.is_symlink():
                    continue
                if st.st_gid != gid:
                    os.chown(p, -1, gid)
                mode = st.st_mode | 0o060 | (0o2010 if p.is_dir() else 0)
                if mode != st.st_mode:
                    os.chmod(p, mode & 0o7777)
            except OSError:
                pass
    except OSError:
        pass


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
