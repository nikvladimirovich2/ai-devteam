"""Unit tests of the shell tool's timeout and Stop (agent/app/common.py run_command), without CrewAI / LiteLLM:
they are replaced by stubs. POSIX only (process groups); CI runs them on Linux. Run: python -m pytest -q tests"""
import os
import pathlib
import sys
import threading
import time
import types

import pytest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="process sessions / killpg are POSIX only")

APP = pathlib.Path(__file__).resolve().parent.parent / "agent" / "app"


@pytest.fixture(scope="module")
def common(tmp_path_factory):
    ws = tmp_path_factory.mktemp("ws")
    os.environ.update(SHELL_USER="", WORKSPACE=str(ws), ROLE="unittest", LEAD_URL="http://127.0.0.1:9")
    lit = types.ModuleType("litellm")
    lit.suppress_debug_info = True
    cr = types.ModuleType("crewai")
    cr.LLM = type("LLM", (), {})
    tools = types.ModuleType("crewai.tools")
    from pydantic import BaseModel

    class BaseTool(BaseModel):
        name: str = ""
        description: str = ""
    tools.BaseTool = BaseTool
    saved = {k: sys.modules.get(k) for k in ("litellm", "crewai", "crewai.tools")}
    sys.modules.update({"litellm": lit, "crewai": cr, "crewai.tools": tools})
    sys.path.insert(0, str(APP))
    import common as c
    c.emit = lambda *a, **k: None          # no lead to send events to
    c.set_task_context("t1", str(ws))
    yield c
    for k, v in saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # a zombie (killed, not yet reaped by its parent) counts as dead
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split(")")[-1].split()[0] != "Z"
    except OSError:
        return True


def test_command_output_and_exit_code(common):
    code, out, why = common.run_command("echo hello; echo oops >&2; exit 3", 10)
    assert (code, why) == (3, "")
    assert "hello" in out and "oops" in out


def test_timeout_kills_the_command_and_its_children(common, tmp_path):
    pidfile = tmp_path / "bg.pid"
    t0 = time.monotonic()
    code, out, why = common.run_command(f"sleep 300 & echo $! > {pidfile}; echo started; sleep 300", 2)
    assert why == "timeout" and code == 124
    assert time.monotonic() - t0 < 20
    assert "started" in out
    pid = int(pidfile.read_text())
    for _ in range(50):
        if not _alive(pid):
            break
        time.sleep(0.1)
    assert not _alive(pid), "the background child survived the timeout"


def test_stop_interrupts_a_running_command(common):
    common.reset_cancel()
    threading.Timer(1.0, common.request_cancel).start()
    t0 = time.monotonic()
    code, _, why = common.run_command("sleep 300", 600)
    assert why == "cancelled" and code == 130
    assert time.monotonic() - t0 < 15
    # after Stop no new command starts and the tools refuse to run, until the next job resets the flag
    assert common.run_command("echo x", 10)[2] == "cancelled"
    with pytest.raises(RuntimeError):
        common.count_tool("run_shell")
    common.reset_cancel()
    assert common.run_command("echo x", 10) == (0, "x", "")
