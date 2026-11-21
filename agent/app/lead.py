"""Team Lead service: web UI + API + orchestration of the team.

Pipeline for each task:
  1. Lead (LLM)   -> plan: project name, requirements, acceptance criteria, scope
  2. Architect    -> ARCHITECTURE.md + project skeleton
  3. Backend + Frontend (in parallel)
  4. QA           -> tests, verdict; on failure -> fixes by owners (up to MAX_QA_ROUNDS)
  5. DevOps       -> Dockerfile/CI/README + push to corporate GitLab
  6. Lead (LLM)   -> final acceptance report
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import re
import sqlite3
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

from crewai import Agent, Crew, Process, Task

from common import WORKSPACE, build_llm, env, extract_json, internal_headers, require_secret, token_ok
from roles import ROLES

DB_PATH = pathlib.Path(os.environ.get("DB_PATH", "/data/devteam.db"))
STATIC = pathlib.Path(__file__).parent / "static"
WORKERS = {r: os.environ.get(f"{r.upper()}_URL", f"http://{r}:8000")
           for r in ("architect", "backend", "frontend", "qa", "devops")}
MAX_QA_ROUNDS = int(env("MAX_QA_ROUNDS", "2"))
MAX_CI_ROUNDS = int(env("MAX_CI_ROUNDS", "3"))      # 0 = do not wait for CI
CLEAN_ENV_HINT = (
    "IMPORTANT: run the tests the same way CI does — in a CLEAN virtual environment, with dependencies "
    "only from the project's files, otherwise you will not see missing packages "
    "(your container already has fastapi/httpx etc. installed):\n"
    "  python -m venv /tmp/cienv && /tmp/cienv/bin/pip install -q -r requirements.txt "
    "(+ requirements-dev.txt if present) && /tmp/cienv/bin/python -m pytest -q\n"
    "Every package the tests import (pytest, httpx/httpx2 for starlette TestClient, etc.) must be in requirements.")
_gu = env("GIT_URL")
GIT_PROVIDER = (env("GIT_PROVIDER") or ("github" if "github" in _gu.lower() else "gitlab")).lower()

require_secret()
app = FastAPI(title="AI DevTeam — Team Lead")
security = HTTPBasic(auto_error=False)


def auth(creds: Optional[HTTPBasicCredentials] = Depends(security)):
    u, p = env("UI_USER"), env("UI_PASSWORD")
    if not (u and p):
        return
    import secrets
    if not creds or not (secrets.compare_digest(creds.username, u) and secrets.compare_digest(creds.password, p)):
        raise HTTPException(401, "auth required", headers={"WWW-Authenticate": "Basic"})


# ------------------------------------------------------------------ storage

_db_lock = threading.Lock()


def db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    return c


_conn = db()
with _db_lock:
    _conn.executescript("""
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS tasks(
        id TEXT PRIMARY KEY, title TEXT, prompt TEXT, status TEXT, stage TEXT,
        project_dir TEXT, project_name TEXT, gitlab_url TEXT, result TEXT,
        created REAL, updated REAL);
    CREATE TABLE IF NOT EXISTS events(
        id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, ts REAL, role TEXT,
        kind TEXT, message TEXT, extra TEXT);
    CREATE INDEX IF NOT EXISTS ev_task ON events(task_id, id);
    """)
    # tasks that were running when the container stopped
    _conn.execute("UPDATE tasks SET status='interrupted' WHERE status IN ('running','queued')")
    _conn.commit()


def q(sql: str, args: tuple = ()) -> list[dict]:
    with _db_lock:
        cur = _conn.execute(sql, args)
        rows = [dict(r) for r in cur.fetchall()]
        _conn.commit()
        return rows


def add_event(task_id: str, role: str, kind: str, message: str, extra: dict | None = None) -> None:
    q("INSERT INTO events(task_id, ts, role, kind, message, extra) VALUES (?,?,?,?,?,?)",
      (task_id, time.time(), role, kind, message, json.dumps(extra or {}, ensure_ascii=False)))


def update_task(task_id: str, **fields: Any) -> None:
    fields["updated"] = time.time()
    cols = ", ".join(f"{k}=?" for k in fields)
    q(f"UPDATE tasks SET {cols} WHERE id=?", (*fields.values(), task_id))


def get_task(task_id: str) -> dict:
    rows = q("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not rows:
        raise HTTPException(404, "task not found")
    return rows[0]


# ------------------------------------------------------------------ orchestration

_cancel: set[str] = set()
_runner = ThreadPoolExecutor(max_workers=1)  # one project at a time: the team is shared


class Cancelled(Exception):
    pass


def check_cancel(task_id: str) -> None:
    if task_id in _cancel:
        raise Cancelled()


def lead_say(task_id: str, kind: str, msg: str, **extra: Any) -> None:
    add_event(task_id, "lead", kind, msg, extra)


def lead_llm(task_id: str, instruction: str, expected: str) -> str:
    """The Team Lead thinks with its OWN key (LEAD_API_KEY)."""
    spec = ROLES["lead"]
    agent = Agent(role=spec["role"], goal=spec["goal"], backstory=spec["backstory"],
                  llm=build_llm("lead"), tools=[], allow_delegation=False, max_iter=5, verbose=True)
    crew = Crew(agents=[agent], tasks=[Task(description=instruction, expected_output=expected, agent=agent)],
                process=Process.sequential, verbose=True)
    t0 = time.time()
    try:
        return str(getattr(crew.kickoff(), "raw", ""))
    finally:
        try:
            m = crew.calculate_usage_metrics() if hasattr(crew, "calculate_usage_metrics") else crew.usage_metrics
            d = m.model_dump() if m is not None else {}
        except Exception:
            d = {}
        u = {k: int(d.get(k) or 0) for k in ("prompt_tokens", "completion_tokens", "total_tokens",
                                              "cached_prompt_tokens", "reasoning_tokens", "successful_requests")}
        u["seconds"] = int(time.time() - t0)
        add_event(task_id, "lead", "usage", f"🔢 {expected}: {u['total_tokens']:,} tokens".replace(",", " "),
                  {"usage": u, "title": expected})


def call_worker(task_id: str, role: str, title: str, instruction: str, context: str = "",
                expected: str = "Short report: what was done, which files, how it was checked") -> str:
    check_cancel(task_id)
    t = get_task(task_id)
    update_task(task_id, stage=f"{role}: {title}")
    lead_say(task_id, "assign", f"→ {ROLES[role]['role']}: {title}", to=role)
    payload = dict(task_id=task_id, project_dir=t["project_dir"], title=title,
                   instruction=instruction, expected_output=expected, context=context)
    for attempt in range(3):
        try:
            with httpx.Client(timeout=httpx.Timeout(None, connect=10)) as c:
                r = c.post(f"{WORKERS[role]}/run", json=payload, headers=internal_headers(role))
            if r.status_code == 409:
                time.sleep(10)
                continue
            r.raise_for_status()
            data = r.json()
            if not data.get("ok"):
                raise RuntimeError(f"{role}: {data.get('output', '')[:1000]}")
            return data.get("output", "")
        except httpx.TransportError as e:
            lead_say(task_id, "warn", f"{role} unavailable ({e}), retrying {attempt + 1}/3")
            time.sleep(15)
    raise RuntimeError(f"agent {role} did not respond")


SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache"}


def project_files(pdir: pathlib.Path) -> list[str]:
    out = []
    if pdir.exists():
        for p in pdir.rglob("*"):
            rel = p.relative_to(pdir)
            if p.is_file() and not any(x in SKIP_DIRS for x in rel.parts):
                out.append(str(rel))
    return sorted(out)


def require_files(task_id: str, pdir: pathlib.Path, role: str, before: set[str], min_new: int = 1) -> None:
    """Agents' reports are not proof. Fail fast if a role claimed work but wrote nothing."""
    now = set(project_files(pdir))
    new = now - before
    if len(new) < min_new:
        raise RuntimeError(
            f"{role} reported success but did not create files in the project "
            f"(files: {len(now)}). The model is most likely not calling tools "
            f"(write_file/run_shell) and only describes the work in text. Check that the model "
            f"supports tool calling (vLLM: --enable-auto-tool-choice --tool-call-parser hermes) "
            f"and LLM_FUNCTION_CALLING=auto|1 in .env.")
    lead_say(task_id, "check", f"🔎 {role}: +{len(new)} files ({', '.join(sorted(new)[:8])}{'…' if len(new) > 8 else ''})")


def _last_pushed_sha(task_id: str) -> str:
    rows = q("SELECT extra FROM events WHERE task_id=? AND role='devops' AND kind='gitlab' "
             "AND message LIKE '✅%' ORDER BY id DESC LIMIT 1", (task_id,))
    return json.loads(rows[0]["extra"] or "{}").get("commit", "") if rows else ""


ROLE_TITLES = {"lead": "Team Lead", "architect": "Architect", "backend": "Backend", "frontend": "Frontend",
               "qa": "QA", "devops": "DevOps"}


def _fmt(n: int) -> str:
    return f"{int(n):,}".replace(",", " ")


def _dur(sec: float) -> str:
    sec = int(sec); h, r = divmod(sec, 3600); m, s_ = divmod(r, 60)
    return f"{h}h {m:02d}m {s_:02d}s" if h else f"{m}m {s_:02d}s"


def build_report(task_id: str, since_event: int = 0) -> str:
    """Plain-text report: what was done, tests, CI, files, tokens per role. Facts from events only."""
    t = get_task(task_id)
    ev = q("SELECT * FROM events WHERE task_id=? AND id>? ORDER BY id", (task_id, since_event))
    for e in ev:
        e["extra"] = json.loads(e["extra"] or "{}")
    if not ev:
        return ""
    t0, t1 = ev[0]["ts"], ev[-1]["ts"]

    # tokens
    per: dict[str, dict] = {}
    for e in ev:
        if e["kind"] != "usage":
            continue
        u = e["extra"].get("usage", {})
        r = per.setdefault(e["role"], {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
                                       "total_tokens": 0, "successful_requests": 0, "seconds": 0})
        r["calls"] += 1
        for k in ("prompt_tokens", "completion_tokens", "total_tokens", "successful_requests", "seconds"):
            r[k] += int(u.get(k) or 0)
    tot = {k: sum(r[k] for r in per.values()) for k in ("prompt_tokens", "completion_tokens", "total_tokens",
                                                       "successful_requests", "calls")}

    # stages
    stages = [e["message"].split("\n")[0].lstrip("✔ ").strip() for e in ev if e["kind"] == "done"]
    qa = [e for e in ev if e["kind"] == "qa"]
    ci = [e for e in ev if e["kind"] == "ci" and e["message"][:1] in ("✅", "❌")]
    pushes = [e["extra"] for e in ev if e["kind"] == "gitlab" and e["message"].startswith("✅")]
    errors = [e["message"].split("\n")[0][:200] for e in ev if e["kind"] == "error"]

    # tests: every pytest/npm test run with its result line
    tests, last_cmd = [], {}
    for e in ev:
        if e["kind"] != "shell":
            continue
        m = e["message"]
        if m.startswith("$ "):
            last_cmd[e["role"]] = m[2:].strip()
            continue
        cmd = last_cmd.get(e["role"], "")
        if not re.search(r"(^|[;&|]\s*|/)(python3? -m pytest|pytest|npm (run )?test|npx (jest|vitest)|jest|vitest|"
                         r"python3? -m unittest)(\s|$)", cmd):
            continue
        res = re.findall(r"(\d+ (?:passed|failed|error|errors|skipped)[^\n]*?)(?: in [\d.]+s|$)", m, re.M)
        code = re.match(r"exit=(-?\d+)", m)
        tests.append(f"[{ROLE_TITLES.get(e['role'], e['role'])}] {cmd[:90]} → "
                     f"{'exit ' + code.group(1) if code else ''} {res[-1] if res else ''}".rstrip())

    files = []
    try:
        files = project_files(pathlib.Path(t["project_dir"]))
    except Exception:
        pass

    L = []
    L.append("=" * 64)
    L.append(f"REPORT · {t['project_name'] or t['title'][:50]} · {task_id}")
    L.append("=" * 64)
    L.append(f"Status:       {t['status']}")
    L.append(f"Duration:     {_dur(t1 - t0)}")
    L.append(f"Repository:   {t['gitlab_url'] or 'not published'}")
    if pushes:
        L.append(f"Commits:      {len(pushes)} (last {pushes[-1].get('commit', '')[:10]}, branch {pushes[-1].get('branch', '')})")
    L.append("")
    L.append("DONE:")
    for s_ in stages:
        L.append(f"  - {s_}")
    if not stages:
        L.append("  (no completed stages)")
    L.append("")
    L.append("TESTS:")
    if qa:
        for e in qa:
            L.append("  " + e["message"].split("\n")[0] + (" — " + e["message"].split("\n", 1)[1][:200].replace("\n", " ")
                                                         if "\n" in e["message"] else ""))
    if tests:
        L.append(f"  Test runs: {len(tests)} (last 8):")
        for x in tests[-8:]:
            L.append(f"    {x}")
    if not qa and not tests:
        L.append("  (no tests were run)")
    L.append("")
    L.append("CI:")
    if ci:
        for e in ci:
            L.append("  " + e["message"].split("\n")[0])
    else:
        L.append("  (not checked)")
    if errors:
        L.append("")
        L.append(f"ERRORS ({len(errors)}):")
        for x in errors[-5:]:
            L.append(f"  - {x}")
    L.append("")
    L.append(f"FILES IN PROJECT: {len(files)}")
    if files:
        L.append("  " + ", ".join(files[:25]) + (" …" if len(files) > 25 else ""))
    L.append("")
    L.append("TOKENS:")
    L.append(f"  {'role':<11}{'calls':>7}{'requests':>10}{'input':>13}{'output':>12}{'total':>13}")
    for role in ("lead", "architect", "backend", "frontend", "qa", "devops"):
        if role in per:
            r = per[role]
            L.append(f"  {ROLE_TITLES[role]:<11}{r['calls']:>7}{r['successful_requests']:>10}"
                     f"{_fmt(r['prompt_tokens']):>13}{_fmt(r['completion_tokens']):>12}{_fmt(r['total_tokens']):>13}")
    L.append(f"  {'-' * 64}")
    L.append(f"  {'TOTAL':<11}{tot['calls']:>7}{tot['successful_requests']:>10}"
             f"{_fmt(tot['prompt_tokens']):>13}{_fmt(tot['completion_tokens']):>12}{_fmt(tot['total_tokens']):>13}")
    if since_event:
        allu = [json.loads(r["extra"] or "{}").get("usage", {}) for r in
                q("SELECT extra FROM events WHERE task_id=? AND kind='usage'", (task_id,))]
        L.append(f"  Whole task (all runs incl. revisions): {_fmt(sum(int(u.get('total_tokens') or 0) for u in allu))} tokens")
    if not per:
        L.append("  (the provider did not return token counts)")
    L.append("=" * 64)
    return "\n".join(L)


def save_report(task_id: str, since_event: int = 0) -> str:
    rep_ = build_report(task_id, since_event)
    if rep_:
        try:
            pdir = pathlib.Path(get_task(task_id)["project_dir"])
            (pdir.parent / ".reports").mkdir(exist_ok=True)
            (pdir.parent / ".reports" / f"{task_id}.txt").write_text(rep_, encoding="utf-8")
        except Exception:
            pass
        add_event(task_id, "lead", "report", rep_)
    return rep_


def _slug(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9-]+", "-", s.lower()).strip("-")
    return s[:60] or "project"


PLAN_PROMPT = """The customer has assigned a task to your development team:

<<<
{prompt}
>>>
{followup}
Team: Architect, Backend Developer, Frontend Developer, QA Engineer, DevOps Engineer.
Make a plan. Answer STRICTLY with a single JSON object, no other text:
{{
  "project_name": "short-latin-kebab-case-repo-name",
  "summary": "what we are building, 2-4 sentences",
  "requirements": ["functional requirement", "..."],
  "acceptance_criteria": ["verifiable criterion", "..."],
  "needs_backend": true,
  "needs_frontend": true,
  "architect_brief": "what the architect must decide/fix (stack, structure, contracts)",
  "backend_brief": "backend tasks (empty string if not needed)",
  "frontend_brief": "frontend tasks (empty string if not needed)",
  "qa_brief": "what QA should check",
  "devops_brief": "what DevOps should prepare"
}}"""


def ci_loop(task_id: str, name: str, summary: str, ctx: str) -> dict:
    """Wait for CI of the last pushed commit; on failure hand logs to DevOps, re-push, repeat."""
    t = get_task(task_id)
    plan = {"summary": summary}
    ci: dict = {}
    for rnd in range(MAX_CI_ROUNDS + 1):
        check_cancel(task_id)
        sha = _last_pushed_sha(task_id)
        if not sha:
            break
        update_task(task_id, stage=f"CI: waiting for pipeline ({sha[:8]})")
        lead_say(task_id, "ci", f"⏳ Waiting for CI for commit {sha[:10]}…")
        try:
            ci = httpx.post(f"{WORKERS['devops']}/ci", timeout=None, headers=internal_headers("devops"),
                            json={"task_id": task_id, "project_name": name, "sha": sha}).json()
        except Exception as e:
            ci = {"status": "error", "details": str(e)}
        st = ci.get("status")
        if st == "success":
            lead_say(task_id, "ci", f"✅ CI passed: {ci.get('web_url', '')}", ci=ci)
            break
        if st in ("no_pipeline", "timeout", "error", "canceled", "skipped", "manual"):
            lead_say(task_id, "warn", f"⚠ CI: {st}. {ci.get('details', '')} {ci.get('web_url', '')}", ci=ci)
            break
        # failed
        jobs = ci.get("jobs") or []
        summary = "\n".join(f"- {j['name']} ({j.get('failure_reason') or 'failed'})" for j in jobs) or "-"
        lead_say(task_id, "ci", f"❌ CI failed (round {rnd + 1}): {ci.get('web_url', '')}\n{summary}", ci=ci)
        if rnd == MAX_CI_ROUNDS:
            break
        logs = "\n\n".join(f"=== job {j['name']} (stage {j.get('stage') or '-'}) ===\n{j['log_tail']}"
                             for j in jobs)[-14000:]
        call_worker(task_id, "devops", f"fixing CI (round {rnd + 1})",
            "The CI pipeline in the repository FAILED. Below are the logs of the failed jobs.\n"
            "1) Find the root cause from the log (missing dependency, wrong command/path, "
            "test error, CI configuration error).\n"
            "2) Reproduce it locally the same way as in CI. " + CLEAN_ENV_HINT + "\n"
            "3) Fix the cause in the code / requirements / CI config (do not disable or delete tests "
            "just to make CI green).\n"
            f"4) Make sure everything passes locally and call publish_repository with project_name='{name}' "
            "and commit_message 'fix(ci): <what was fixed>'.\n\n" + logs,
            ctx, expected="What was the cause, what was fixed, local check result")
        if _last_pushed_sha(task_id) == sha:
            lead_say(task_id, "warn", "DevOps did not push the fix — publishing directly")
            try:
                httpx.post(f"{WORKERS['devops']}/publish", timeout=600, headers=internal_headers("devops"), json={
                    "task_id": task_id, "project_dir": t["project_dir"], "project_name": name,
                    "description": plan.get("summary", "")[:300],
                    "commit_message": f"fix(ci): round {rnd + 1}"})
            except Exception as e:
                lead_say(task_id, "error", f"Publication failed: {e}")
            if _last_pushed_sha(task_id) == sha:
                lead_say(task_id, "warn", "No new commit (no changes) — stopping the CI loop")
                break

    return ci


def run_ci_only(task_id: str) -> None:
    """'Check/fix CI' button: only the CI loop for an already published project."""
    _since = max(0, _last_event_id(task_id) - 1)
    t = get_task(task_id)
    update_task(task_id, status="running", stage="CI")
    try:
        if not _last_pushed_sha(task_id):
            raise RuntimeError("the project has not been published to the repository yet")
        ctx = f"PROJECT: {t['project_name']}\nCUSTOMER'S TASK:\n{t['prompt']}\n"
        ci = ci_loop(task_id, t["project_name"], t["title"], ctx)
        st = {"success": "done", "failed": "ci_failed"}.get(ci.get("status"), "done")
        update_task(task_id, status=st, stage=f"CI: {ci.get('status', '?')}")
    except Cancelled:
        update_task(task_id, status="cancelled", stage="cancelled")
        lead_say(task_id, "warn", "⏹ Cancelled")
    except Exception as e:
        update_task(task_id, status="failed", stage="error", result=str(e))
        lead_say(task_id, "error", f"💥 CI: {e}")
    finally:
        _cancel.discard(task_id)
        try:
            save_report(task_id, _since)
        except Exception as e:
            lead_say(task_id, "warn", f"report: {e}")


def _last_event_id(task_id: str) -> int:
    r = q("SELECT MAX(id) AS m FROM events WHERE task_id=?", (task_id,))
    return int(r[0]["m"] or 0) if r else 0


def run_pipeline(task_id: str, followup: str = "") -> None:
    # the user's prompt event was already written by the API → include it in this run's report
    _since = max(0, _last_event_id(task_id) - 1)
    t = get_task(task_id)
    update_task(task_id, status="running", stage="planning")
    try:
        # ---------------- 1. plan
        lead_say(task_id, "start", "📋 Analysing the task and building a plan…")
        fu = f"\nThis is a REVISION of an existing project ({t['project_name']}). Customer's new request:\n<<<\n{followup}\n>>>\nThe project is already in the working directory — change it, do not rewrite from scratch.\n" if followup else ""
        raw = lead_llm(task_id, PLAN_PROMPT.format(prompt=t["prompt"], followup=fu), "JSON plan")
        plan = extract_json(raw) or {}
        if not plan:
            lead_say(task_id, "warn", "Could not parse the plan as JSON, using the answer as is")
            plan = {"summary": raw[:2000], "needs_backend": True, "needs_frontend": True}
        name = t["project_name"] or _slug(plan.get("project_name") or t["title"])
        update_task(task_id, project_name=name)
        crit = "\n".join(f"- {c}" for c in plan.get("acceptance_criteria", []))
        reqs = "\n".join(f"- {c}" for c in plan.get("requirements", []))
        brief = (f"PROJECT: {name}\nCUSTOMER'S TASK:\n{t['prompt']}\n"
                 + (f"\nREVISION REQUEST:\n{followup}\n" if followup else "")
                 + f"\nSUMMARY: {plan.get('summary', '')}\nREQUIREMENTS:\n{reqs}\nACCEPTANCE CRITERIA:\n{crit}\n")
        pdir = pathlib.Path(t["project_dir"])
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "docs").mkdir(exist_ok=True)
        (pdir / "docs" / "PLAN.md").write_text(
            f"# {name}\n\n{plan.get('summary', '')}\n\n## Requirements\n{reqs}\n\n## Acceptance criteria\n{crit}\n",
            encoding="utf-8")
        lead_say(task_id, "plan", f"🗺 Plan «{name}»\n\n{plan.get('summary', '')}\n\nAcceptance criteria:\n{crit}", plan=plan)

        # ---------------- 2. architect
        before = set(project_files(pdir))
        arch = call_worker(task_id, "architect", "architecture and skeleton",
            "Design the solution and create the project skeleton.\n"
            f"{plan.get('architect_brief', '')}\n"
            "Required: ARCHITECTURE.md (stack, directory structure, API contracts with request/response examples, "
            "data model, run and test commands), directory skeleton and dependency files. "
            "Clearly split the area of responsibility of backend and frontend (folders).", brief)
        if not followup:
            require_files(task_id, pdir, "architect", before)

        # ---------------- 3. dev (parallel)
        dev_jobs = {}
        if plan.get("needs_backend", True):
            dev_jobs["backend"] = ("server-side implementation",
                f"Implement the server side.\n{plan.get('backend_brief', '')}\n"
                "Strictly follow ARCHITECTURE.md (read it first). Write unit tests and run them.")
        if plan.get("needs_frontend", True):
            dev_jobs["frontend"] = ("user interface implementation",
                f"Implement the frontend.\n{plan.get('frontend_brief', '')}\n"
                "Strictly follow ARCHITECTURE.md and the API contracts (read it first). "
                "Do not touch the backend folders.")
        ctx = brief + "\n\nARCHITECT'S REPORT:\n" + arch[:6000]
        reports: dict[str, str] = {}
        before = set(project_files(pdir))
        with ThreadPoolExecutor(max_workers=2) as ex:
            futs = {r: ex.submit(call_worker, task_id, r, title, instr, ctx) for r, (title, instr) in dev_jobs.items()}
            for r, f in futs.items():
                reports[r] = f.result()
        if not followup:
            require_files(task_id, pdir, "backend/frontend", before, min_new=len(dev_jobs))

        # ---------------- 4. QA loop
        verdict: dict = {}
        for rnd in range(MAX_QA_ROUNDS + 1):
            qa_raw = call_worker(task_id, "qa", f"testing (round {rnd + 1})",
                f"Check the project against the acceptance criteria.\n{plan.get('qa_brief', '')}\n"
                "Install dependencies, write/supplement automated tests in tests/, run them and the application. "
                + CLEAN_ENV_HINT + "\n"
                "Your final answer MUST end with a JSON object:\n"
                '{"passed": true|false, "summary": "...", "issues": [{"owner": "backend|frontend|architect", '
                '"title": "...", "details": "steps to reproduce / expected / actual"}]}',
                ctx + "\n\nDEVELOPER REPORTS:\n" + "\n---\n".join(f"[{k}]\n{v[:4000]}" for k, v in reports.items()),
                expected="Test report ending with a JSON verdict")
            verdict = extract_json(qa_raw) or {"passed": False, "summary": qa_raw[-1500:], "issues": []}
            issues = verdict.get("issues") or []
            lead_say(task_id, "qa", ("✅ QA: passed" if verdict.get("passed") else f"❌ QA: issues found ({len(issues)})")
                     + f"\n{verdict.get('summary', '')}", verdict=verdict)
            if verdict.get("passed") or rnd == MAX_QA_ROUNDS:
                break
            by_owner: dict[str, list] = {}
            for it in issues:
                o = it.get("owner", "backend")
                o = o if o in ("backend", "frontend", "architect") else "backend"
                by_owner.setdefault(o, []).append(it)
            if not by_owner and dev_jobs:
                by_owner[next(iter(dev_jobs))] = [{"title": "QA did not pass", "details": verdict.get("summary", "")}]
            with ThreadPoolExecutor(max_workers=3) as ex:
                futs = {o: ex.submit(call_worker, task_id, o, f"fixes after QA (round {rnd + 1})",
                        "Fix the defects found by QA, then check by running tests:\n"
                        + json.dumps(items, ensure_ascii=False, indent=2), ctx)
                        for o, items in by_owner.items()}
                for o, f in futs.items():
                    reports[o] = f.result()

        # ---------------- 5. DevOps + GitLab
        devops = call_worker(task_id, "devops", "packaging and publishing to the repository",
            f"{plan.get('devops_brief', '')}\n"
            f"Target repository host: {GIT_PROVIDER}.\n"
            "1) Add Dockerfile (and docker-compose.yml if there is more than one service), CI config "
            "(stages test and build), a README.md with description, run instructions and API; .gitignore.\n"
            "2) Check the tests locally exactly as CI will run them. " + CLEAN_ENV_HINT + "\n"
            "   The CI job must install dependencies from the same files (pip install -r requirements.txt …).\n"
            "   CI: if the repository is on GitHub — .github/workflows/ci.yml instead of .gitlab-ci.yml.\n"
            f"3) Call the publish_repository tool with project_name='{name}' and a meaningful commit message.\n"
            "Give the URL of the repository in your answer.",
            ctx + "\n\nQA VERDICT:\n" + json.dumps(verdict, ensure_ascii=False)[:3000])
        # The URL is taken ONLY from the publish tool's own event (real push), never from the
        # agent's text: models like to "report" invented links such as gitlab.com/your-username/...
        gl = q("SELECT extra FROM events WHERE task_id=? AND role='devops' AND kind='gitlab' "
               "AND message LIKE '✅%' ORDER BY id DESC LIMIT 1", (task_id,))
        url = json.loads(gl[0]["extra"] or "{}").get("web_url", "") if gl else ""
        if not url:
            lead_say(task_id, "warn", "⚠ DevOps did not call publish_repository (no real push). "
                     "Trying to publish directly…")
            try:
                r = httpx.post(f"{WORKERS['devops']}/publish", timeout=600, headers=internal_headers("devops"), json={
                    "task_id": task_id, "project_dir": t["project_dir"], "project_name": name,
                    "description": plan.get("summary", "")[:300],
                    "commit_message": f"AI DevTeam: {t['title'][:200]}"})
                d = r.json()
                url = d.get("web_url", "") if d.get("ok") else ""
                if not url:
                    lead_say(task_id, "error", f"Publication failed: {d.get('error', r.text[:500])}")
            except Exception as e:
                lead_say(task_id, "error", f"Publication failed: {e}")

        # ---------------- 5b. CI: wait for the pipeline, on failure -> fix -> push again
        ci: dict = {}
        if url and MAX_CI_ROUNDS > 0:
            ci = ci_loop(task_id, name, plan.get("summary", ""), ctx)

        # ---------------- 6. final acceptance
        final = lead_llm(task_id,
            "Prepare a short final report for the customer: what was built, how to run it, QA result, "
            "known limitations, repository link.\n\n" + brief
            + f"\nQA: {json.dumps(verdict, ensure_ascii=False)[:2000]}\nDevOps: {devops[:3000]}\nRepository: {url or 'not published'}"
            + f"\nCI: {ci.get('status', 'not checked')} {ci.get('web_url', '')}",
            "Final report in markdown")
        status = "done" if url else "done_no_push"
        if url and ci.get("status") == "failed":
            status = "ci_failed"
        update_task(task_id, status=status, stage="finished", gitlab_url=url, result=final)
        lead_say(task_id, "final", final, gitlab_url=url)
    except Cancelled:
        update_task(task_id, status="cancelled", stage="cancelled")
        lead_say(task_id, "warn", "⏹ Task cancelled by the user")
    except Exception as e:
        update_task(task_id, status="failed", stage="error", result=str(e))
        lead_say(task_id, "error", f"💥 {e}\n{traceback.format_exc()[-2500:]}")
    finally:
        _cancel.discard(task_id)
        try:
            save_report(task_id, _since)
        except Exception as e:
            lead_say(task_id, "warn", f"report: {e}")


# ------------------------------------------------------------------ API

class NewTask(BaseModel):
    prompt: str
    title: str = ""


class FollowUp(BaseModel):
    prompt: str


class InEvent(BaseModel):
    task_id: str = ""
    role: str
    kind: str
    message: str
    ts: float | None = None
    extra: dict = {}


@app.post("/api/internal/events")
def internal_event(ev: InEvent, x_devteam_role: str = Header(""), x_devteam_token: str = Header("")):
    """Events from the workers. The role is the one the token proves, never the one in the body:
    otherwise anything that can reach this port could, for example, post a fake "pushed" event
    as devops, and the lead takes the repository link and commit from exactly those events."""
    if x_devteam_role not in WORKERS or not token_ok(x_devteam_role, x_devteam_token):
        raise HTTPException(403, "internal endpoint")
    if ev.task_id:
        add_event(ev.task_id, x_devteam_role, ev.kind, ev.message, ev.extra)
    return {"ok": True}


@app.get("/api/team", dependencies=[Depends(auth)])
async def team():
    out = {"lead": {"role": "lead", "busy": bool(q("SELECT 1 FROM tasks WHERE status='running'")), "ok": True}}
    async with httpx.AsyncClient(timeout=3) as c:
        async def one(r, u):
            try:
                d = (await c.get(f"{u}/health")).json()
                d["ok"] = True
            except Exception:
                d = {"role": r, "ok": False, "busy": False}
            return r, d
        for r, d in await asyncio.gather(*(one(r, u) for r, u in WORKERS.items())):
            out[r] = d
    return out


@app.get("/api/tasks", dependencies=[Depends(auth)])
def list_tasks():
    return q("SELECT id,title,status,stage,project_name,gitlab_url,created,updated FROM tasks ORDER BY created DESC LIMIT 200")


@app.post("/api/tasks", dependencies=[Depends(auth)])
def create_task(body: NewTask):
    if not body.prompt.strip():
        raise HTTPException(400, "empty prompt")
    tid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    title = body.title.strip() or body.prompt.strip().splitlines()[0][:80]
    pdir = WORKSPACE / tid
    q("INSERT INTO tasks(id,title,prompt,status,stage,project_dir,project_name,gitlab_url,result,created,updated)"
      " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
      (tid, title, body.prompt, "queued", "queued", str(pdir), "", "", "", time.time(), time.time()))
    add_event(tid, "user", "prompt", body.prompt)
    _runner.submit(run_pipeline, tid)
    return get_task(tid)


@app.post("/api/tasks/{tid}/followup", dependencies=[Depends(auth)])
def followup(tid: str, body: FollowUp):
    t = get_task(tid)
    if t["status"] in ("running", "queued"):
        raise HTTPException(409, "task is still running")
    add_event(tid, "user", "prompt", body.prompt)
    update_task(tid, status="queued", stage="queued (revision)")
    _runner.submit(run_pipeline, tid, body.prompt)
    return get_task(tid)


@app.get("/api/tasks/{tid}/report", dependencies=[Depends(auth)])
def task_report(tid: str, full: int = 0):
    """Last report text (or a fresh one for the whole task with ?full=1)."""
    get_task(tid)
    if full:
        return {"report": build_report(tid, 0)}
    r = q("SELECT message FROM events WHERE task_id=? AND kind='report' ORDER BY id DESC LIMIT 1", (tid,))
    return {"report": r[0]["message"] if r else build_report(tid, 0)}


@app.post("/api/tasks/{tid}/ci", dependencies=[Depends(auth)])
def check_ci(tid: str):
    t = get_task(tid)
    if t["status"] in ("running", "queued"):
        raise HTTPException(409, "task is still running")
    if not _last_pushed_sha(tid):
        raise HTTPException(400, "the project has not been published yet")
    add_event(tid, "user", "prompt", "🔧 Check CI and fix errors")
    update_task(tid, status="queued", stage="queued (CI)")
    _runner.submit(run_ci_only, tid)
    return get_task(tid)


@app.post("/api/tasks/{tid}/cancel", dependencies=[Depends(auth)])
def cancel(tid: str):
    get_task(tid)
    _cancel.add(tid)
    add_event(tid, "user", "warn", "Cancellation requested (will stop after the current agent step finishes)")
    return {"ok": True}


class BulkDelete(BaseModel):
    ids: list[str] = []
    statuses: list[str] = []          # e.g. ["failed","cancelled","interrupted"]


def _delete_task(tid: str) -> bool:
    """Delete a task: DB rows + its working directory. Running/queued tasks are refused."""
    rows = q("SELECT status, project_dir FROM tasks WHERE id=?", (tid,))
    if not rows:
        return False
    if rows[0]["status"] in ("running", "queued"):
        raise HTTPException(409, f"task {tid} is running — stop it first")
    pdir = pathlib.Path(rows[0]["project_dir"] or "")
    q("DELETE FROM events WHERE task_id=?", (tid,))
    q("DELETE FROM tasks WHERE id=?", (tid,))
    # remove the project's working copy (only inside /workspace, never follow symlinks out)
    try:
        root = WORKSPACE.resolve()
        real = pdir.resolve()
        if pdir.name and root in real.parents and not pdir.is_symlink():
            import shutil
            shutil.rmtree(real, ignore_errors=True)
    except Exception:
        pass
    return True


@app.delete("/api/tasks/{tid}", dependencies=[Depends(auth)])
def delete_task(tid: str):
    if not _delete_task(tid):
        raise HTTPException(404, "task not found")
    return {"ok": True, "deleted": [tid]}


@app.post("/api/tasks/delete", dependencies=[Depends(auth)])
def delete_tasks(body: BulkDelete):
    ids = set(body.ids)
    if body.statuses:
        marks = ",".join("?" * len(body.statuses))
        ids |= {r["id"] for r in q(f"SELECT id FROM tasks WHERE status IN ({marks})", tuple(body.statuses))}
    deleted, skipped = [], []
    for tid in sorted(ids):
        try:
            if _delete_task(tid):
                deleted.append(tid)
        except HTTPException:
            skipped.append(tid)
    return {"ok": True, "deleted": deleted, "skipped_running": skipped}


@app.get("/api/tasks/{tid}", dependencies=[Depends(auth)])
def task_detail(tid: str):
    return get_task(tid)


@app.get("/api/tasks/{tid}/files", dependencies=[Depends(auth)])
def task_files(tid: str):
    root = pathlib.Path(get_task(tid)["project_dir"])
    skip = {".git", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache"}
    files = []
    if root.exists():
        for p in sorted(root.rglob("*")):
            rel = p.relative_to(root)
            if p.is_file() and not any(x in skip for x in rel.parts):
                files.append({"path": str(rel), "size": p.stat().st_size})
    return files[:1000]


@app.get("/api/tasks/{tid}/file", dependencies=[Depends(auth)])
def task_file(tid: str, path: str):
    root = pathlib.Path(get_task(tid)["project_dir"]).resolve()
    p = (root / path).resolve()
    if root not in p.parents or not p.is_file():
        raise HTTPException(404)
    return {"path": path, "content": p.read_text(encoding="utf-8", errors="replace")[:200000]}


@app.get("/api/tasks/{tid}/events", dependencies=[Depends(auth)])
async def stream(tid: str, request: Request, after: int = 0):
    get_task(tid)

    async def gen():
        last = after
        idle = 0
        while not await request.is_disconnected():
            rows = q("SELECT * FROM events WHERE task_id=? AND id>? ORDER BY id LIMIT 500", (tid, last))
            for r in rows:
                last = r["id"]
                r["extra"] = json.loads(r["extra"] or "{}")
                yield f"id: {r['id']}\ndata: {json.dumps(r, ensure_ascii=False)}\n\n"
            if not rows:
                idle += 1
                if idle % 15 == 0:
                    yield ": ping\n\n"
                await asyncio.sleep(1)
            else:
                idle = 0
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/health")
def health():
    return {"role": "lead", "ok": True}


@app.get("/", dependencies=[Depends(auth)])
def index():
    return FileResponse(STATIC / "index.html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), log_level="info", loop="asyncio")
