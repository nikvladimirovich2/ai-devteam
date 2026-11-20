"""Repository publishing tool — available ONLY in the devops container.
Provider is chosen by the URL in GIT_URL:
  * host contains "github" -> GitHub (REST v3; GitHub Enterprise Server: https://ghe.host -> /api/v3)
  * anything else          -> GitLab (REST v4)
Override with GIT_PROVIDER=gitlab|github. The token is never shown to the model:
the tool creates the repository and pushes itself (token only in a transient HTTP header)."""
from __future__ import annotations

import base64
import os
import re
import subprocess
import time
import urllib.parse
from typing import Type

import httpx
from pydantic import BaseModel, Field
from crewai.tools import BaseTool

from common import current_project_dir, emit, env, env_bool


# ------------------------------------------------------------------ config

def detect_provider(url: str, forced: str = "") -> str:
    forced = (forced or "").strip().lower()
    if forced in ("gitlab", "github"):
        return forced
    host = urllib.parse.urlparse(url).hostname or ""
    return "github" if "github" in host else "gitlab"


def cfg() -> dict:
    url = env("GIT_URL").rstrip("/")
    return {
        "url": url,
        "token": env("GIT_TOKEN"),
        "namespace": env("GIT_NAMESPACE"),
        "branch": env("GIT_DEFAULT_BRANCH", "main"),
        "visibility": env("GIT_VISIBILITY", "private"),
        "insecure": env_bool("GIT_SSL_VERIFY_DISABLE"),
        "provider": detect_provider(url, env("GIT_PROVIDER")),
    }


def _slug(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9._-]+", "-", name.strip().lower()).strip("-.")
    return s[:100] or "ai-project"


# ------------------------------------------------------------------ providers

def _ensure_gitlab(c: dict, name: str, path: str, description: str) -> dict:
    api = f"{c['url']}/api/v4"
    with httpx.Client(headers={"PRIVATE-TOKEN": c["token"]}, verify=not c["insecure"], timeout=60) as h:
        r = h.get(f"{api}/namespaces/{urllib.parse.quote(c['namespace'], safe='')}")
        if r.status_code != 200:
            raise RuntimeError(f"GitLab namespace '{c['namespace']}' not found: HTTP {r.status_code} {r.text[:300]}")
        ns_id = r.json()["id"]
        r = h.get(f"{api}/projects/{urllib.parse.quote(c['namespace'] + '/' + path, safe='')}")
        if r.status_code == 200:
            p, created = r.json(), False
        else:
            r = h.post(f"{api}/projects", json={
                "name": name[:250], "path": path, "namespace_id": ns_id, "description": description[:2000],
                "visibility": c["visibility"], "initialize_with_readme": False})
            if r.status_code not in (200, 201):
                raise RuntimeError(f"GitLab: project creation failed: HTTP {r.status_code} {r.text[:500]}")
            p, created = r.json(), True
    return {"web_url": p["web_url"], "clone_url": p["http_url_to_repo"], "created": created,
            "basic_user": "oauth2"}


def _ensure_github(c: dict, name: str, path: str, description: str) -> dict:
    host = urllib.parse.urlparse(c["url"]).hostname or "github.com"
    api = "https://api.github.com" if host in ("github.com", "www.github.com", "api.github.com") \
        else f"{c['url']}/api/v3"
    hdr = {"Authorization": f"Bearer {c['token']}", "Accept": "application/vnd.github+json",
           "X-GitHub-Api-Version": "2022-11-28"}
    with httpx.Client(headers=hdr, verify=not c["insecure"], timeout=60) as h:
        me = h.get(f"{api}/user")
        if me.status_code != 200:
            raise RuntimeError(f"GitHub: invalid token: HTTP {me.status_code} {me.text[:300]}")
        login = me.json()["login"]
        owner = c["namespace"] or login
        r = h.get(f"{api}/repos/{owner}/{path}")
        if r.status_code == 200:
            repo, created = r.json(), False
        else:
            body = {"name": path, "description": description[:350],
                    "private": c["visibility"] != "public", "auto_init": False}
            if c["visibility"] == "internal":
                body["visibility"] = "internal"   # only GitHub Enterprise organizations
            url = f"{api}/user/repos" if owner == login else f"{api}/orgs/{owner}/repos"
            r = h.post(url, json=body)
            if r.status_code not in (200, 201):
                raise RuntimeError(f"GitHub: repository creation in '{owner}' failed: HTTP {r.status_code} {r.text[:500]}")
            repo, created = r.json(), True
    return {"web_url": repo["html_url"], "clone_url": repo["clone_url"], "created": created,
            "basic_user": "x-access-token"}


# ------------------------------------------------------------------ git

def _git(*args: str, cwd: str, insecure: bool = False) -> str:
    e = dict(os.environ)
    who, mail = env("GIT_AUTHOR_NAME", "AI DevTeam"), env("GIT_AUTHOR_EMAIL", "ai-devteam@example.com")
    e.update({"GIT_AUTHOR_NAME": who, "GIT_AUTHOR_EMAIL": mail, "GIT_COMMITTER_NAME": who,
              "GIT_COMMITTER_EMAIL": mail, "GIT_TERMINAL_PROMPT": "0"})
    if insecure:
        e["GIT_SSL_NO_VERIFY"] = "1"
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=e, timeout=600)
    if r.returncode != 0:
        cmd = next((a for a in args if not a.startswith("-") and "=" not in a), "git")
        raise RuntimeError(f"git {cmd} failed: {(r.stderr or r.stdout).strip()[:1500]}")
    return r.stdout.strip()


def publish(project_name: str, description: str, commit_message: str) -> dict:
    c = cfg()
    if not (c["url"] and c["token"]):
        raise RuntimeError("GIT_URL / GIT_TOKEN are not set in .env")
    if c["provider"] == "gitlab" and not c["namespace"]:
        raise RuntimeError("GIT_NAMESPACE (GitLab group) is not set in .env")
    path = _slug(project_name)
    ensure = _ensure_github if c["provider"] == "github" else _ensure_gitlab
    remote = ensure(c, project_name, path, description)

    repo, branch = str(current_project_dir()), c["branch"]
    if not os.path.isdir(os.path.join(repo, ".git")):
        _git("init", "-b", branch, cwd=repo)
    gi = os.path.join(repo, ".gitignore")
    if not os.path.exists(gi):
        with open(gi, "w") as f:
            f.write("node_modules/\n__pycache__/\n.venv/\nvenv/\n.pytest_cache/\ndist/\n*.pyc\n.env\n")
    _git("add", "-A", cwd=repo)
    if _git("status", "--porcelain", cwd=repo):
        _git("commit", "-m", commit_message[:500] or "AI DevTeam: initial commit", cwd=repo)
    _git("branch", "-M", branch, cwd=repo)
    if "origin" in _git("remote", cwd=repo).split():
        _git("remote", "set-url", "origin", remote["clone_url"], cwd=repo)
    else:
        _git("remote", "add", "origin", remote["clone_url"], cwd=repo)
    basic = base64.b64encode(f"{remote['basic_user']}:{c['token']}".encode()).decode()
    _git("-c", f"http.extraHeader=Authorization: Basic {basic}",
         "push", "-u", "origin", f"{branch}:{branch}", cwd=repo, insecure=c["insecure"])
    sha = _git("rev-parse", "HEAD", cwd=repo)
    return {"provider": c["provider"], "web_url": remote["web_url"], "repo": remote["clone_url"],
            "branch": branch, "commit": sha, "created": remote["created"]}


# ------------------------------------------------------------------ CI status

_GL_DONE = {"success", "failed", "canceled", "skipped", "manual"}


def _gl_project(c: dict, path: str) -> str:
    return urllib.parse.quote(f"{c['namespace']}/{path}", safe="")


def _ci_gitlab(c: dict, path: str, sha: str, timeout_s: int, poll: int) -> dict:
    api = f"{c['url']}/api/v4/projects/{_gl_project(c, path)}"
    h = {"PRIVATE-TOKEN": c["token"]}
    start = time.time(); deadline = start + timeout_s
    with httpx.Client(headers=h, verify=not c["insecure"], timeout=60) as cl:
        pipe = None
        last = ""
        while time.time() < deadline:
            r = cl.get(f"{api}/pipelines", params={"sha": sha, "order_by": "id", "sort": "desc"})
            if r.status_code == 200 and r.json():
                pipe = r.json()[0]
                if pipe["status"] in _GL_DONE:
                    break
                if pipe["status"] != last:
                    last = pipe["status"]
                    emit("ci", f"⏳ CI pipeline #{pipe['id']}: {pipe['status']} {pipe.get('web_url', '')}")
            elif time.time() > start + 180:
                # no pipeline 3 minutes after the push: CI is not configured or there are no runners
                return {"status": "no_pipeline", "details": "No pipeline appeared for the commit "
                        "(no .gitlab-ci.yml, CI disabled in the project, or no runners)."}
            time.sleep(poll)
        if not pipe:
            return {"status": "no_pipeline", "details": "pipeline not found"}
        if pipe["status"] not in _GL_DONE:
            return {"status": "timeout", "web_url": pipe.get("web_url", ""),
                    "details": f"pipeline still {pipe['status']} after {timeout_s}s (no free runners?)"}
        res = {"status": pipe["status"], "web_url": pipe.get("web_url", ""), "pipeline_id": pipe["id"], "jobs": []}
        if pipe["status"] == "failed":
            jobs = cl.get(f"{api}/pipelines/{pipe['id']}/jobs", params={"per_page": 50}).json()
            for j in jobs:
                if j.get("status") != "failed":
                    continue
                log = cl.get(f"{api}/jobs/{j['id']}/trace").text
                res["jobs"].append({"name": j["name"], "stage": j.get("stage"),
                                    "failure_reason": j.get("failure_reason"),
                                    "log_tail": _clean_log(log)[-6000:]})
        return res


def _ci_github(c: dict, path: str, sha: str, timeout_s: int, poll: int) -> dict:
    host = urllib.parse.urlparse(c["url"]).hostname or "github.com"
    api = "https://api.github.com" if host in ("github.com", "www.github.com", "api.github.com") \
        else f"{c['url']}/api/v3"
    hdr = {"Authorization": f"Bearer {c['token']}", "Accept": "application/vnd.github+json",
           "X-GitHub-Api-Version": "2022-11-28"}
    start = time.time(); deadline = start + timeout_s
    with httpx.Client(headers=hdr, verify=not c["insecure"], timeout=60, follow_redirects=True) as cl:
        login = cl.get(f"{api}/user").json().get("login", "")
        owner = c["namespace"] or login
        base = f"{api}/repos/{owner}/{path}"
        runs = []
        last = ""
        while time.time() < deadline:
            r = cl.get(f"{base}/actions/runs", params={"head_sha": sha, "per_page": 20})
            runs = r.json().get("workflow_runs", []) if r.status_code == 200 else []
            if runs and all(x["status"] == "completed" for x in runs):
                break
            if not runs and time.time() > start + 180:
                return {"status": "no_pipeline", "details": "No workflow run appeared for the commit "
                        "(no .github/workflows/*.yml or Actions disabled)."}
            cur = ", ".join(f"{x['name']}={x['status']}" for x in runs)
            if runs and cur != last:
                last = cur
                emit("ci", "⏳ GitHub Actions: " + cur)
            time.sleep(poll)
        if not runs:
            return {"status": "no_pipeline", "details": "workflow run not found"}
        if not all(x["status"] == "completed" for x in runs):
            return {"status": "timeout", "web_url": runs[0]["html_url"], "details": f"still running after {timeout_s}s"}
        bad = [x for x in runs if x.get("conclusion") not in ("success", "skipped", "neutral")]
        res = {"status": "failed" if bad else "success", "web_url": (bad or runs)[0]["html_url"], "jobs": []}
        for run in bad:
            jobs = cl.get(f"{base}/actions/runs/{run['id']}/jobs").json().get("jobs", [])
            for j in jobs:
                if j.get("conclusion") in ("success", "skipped", None):
                    continue
                lr = cl.get(f"{base}/actions/jobs/{j['id']}/logs")
                res["jobs"].append({"name": f"{run['name']} / {j['name']}", "stage": "",
                                    "failure_reason": j.get("conclusion"),
                                    "log_tail": _clean_log(lr.text if lr.status_code == 200 else "")[-6000:]})
        return res


def _clean_log(s: str) -> str:
    s = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", s or "")          # ANSI colours
    s = re.sub(r"section_(start|end):\d+:\S+\r?", "", s)       # GitLab section markers
    # GitLab 17+/GitHub timestamp prefixes: "2026-11-20T14:31:40.827365Z 01O " / "2026-...Z "
    s = re.sub(r"(?m)^\d{4}-\d\d-\d\dT[\d:.]+Z (?:\d+[OE]\+? )?", "", s)
    return s.replace("\r", "")


def ci_status(project_name: str, sha: str, timeout_s: int | None = None) -> dict:
    """Wait for CI on the given commit and return status + logs of failed jobs."""
    c = cfg()
    timeout_s = timeout_s or int(env("CI_WAIT_TIMEOUT", "1200"))
    poll = int(env("CI_POLL_INTERVAL", "15"))
    fn = _ci_github if c["provider"] == "github" else _ci_gitlab
    try:
        return fn(c, _slug(project_name), sha, timeout_s, poll)
    except Exception as e:
        return {"status": "error", "details": f"{type(e).__name__}: {e}"}


# ------------------------------------------------------------------ CrewAI tool

class _PubArgs(BaseModel):
    project_name: str = Field(..., description="Project (repository) name, Latin letters, e.g. todo-service")
    description: str = Field("", description="Short project description")
    commit_message: str = Field("Initial implementation by AI DevTeam", description="Commit message")


class RepoPublishTool(BaseTool):
    name: str = "publish_repository"
    description: str = ("Create (if needed) a repository in the corporate GitLab or GitHub (chosen by settings), "
                        "commit ALL project files and push them to the main branch. Returns the repository URL. "
                        "Call once at the very end.")
    args_schema: Type[BaseModel] = _PubArgs

    def _run(self, project_name: str, description: str = "",
             commit_message: str = "Initial implementation by AI DevTeam") -> str:
        prov = cfg()["provider"]
        emit("gitlab", f"⏫ publishing {project_name} to {prov}…")
        try:
            res = publish(project_name, description, commit_message)
        except Exception as e:
            emit("error", f"{prov}: {e}")
            return f"ERROR: {e}"
        emit("gitlab", f"✅ pushed to {res['provider']}: {res['web_url']} ({res['branch']} @ {res['commit'][:10]})", **res)
        return f"OK web_url={res['web_url']} branch={res['branch']} commit={res['commit']}"
