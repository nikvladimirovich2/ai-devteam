"""Unit test for the text-tool-call fallback in agent/app/common.py (ai-devteam-test-report.md 2026-11-26:
vLLM --tool-call-parser hermes + Qwen3 intermittently returns a tool call as plain text).

Real litellm is used (to build a real response object and real tool_calls), crewai is stubbed (its
heavy import chain is not needed for this pure function) following the pattern in the ai-devteam-dev skill.
"""
import json
import pathlib
import sys
import types

APP = pathlib.Path(__file__).resolve().parent.parent / "agent" / "app"
sys.path.insert(0, str(APP))

# --- stub crewai: common.py only needs `LLM` and `tools.BaseTool` to import successfully.
crewai_stub = types.ModuleType("crewai")
crewai_stub.LLM = type("LLM", (), {})
crewai_tools_stub = types.ModuleType("crewai.tools")
crewai_tools_stub.BaseTool = type("BaseTool", (), {})
sys.modules.setdefault("crewai", crewai_stub)
sys.modules.setdefault("crewai.tools", crewai_tools_stub)

import common  # noqa: E402
import litellm  # noqa: E402
from litellm.types.utils import Choices, Message, ModelResponse  # noqa: E402


def _response(content: str | None, tool_calls=None) -> ModelResponse:
    message = Message(content=content, tool_calls=tool_calls)
    return ModelResponse(choices=[Choices(message=message)])


def test_recovers_a_hermes_style_text_tool_call():
    r = _response(
        '<tool_call>\n{"name": "write_file", "arguments": {"path": "ARCHITECTURE.md", "content": "# Plan"}}\n</tool_call>',
    )
    common._recover_text_tool_call(r)
    tc = r.choices[0].message.tool_calls
    assert tc and len(tc) == 1
    assert tc[0].function.name == "write_file"
    assert json.loads(tc[0].function.arguments) == {"path": "ARCHITECTURE.md", "content": "# Plan"}


def test_only_the_first_block_is_recovered_when_the_model_dumps_several():
    r = _response(
        '<tool_call>{"name": "write_file", "arguments": {"path": "a.py", "content": "1"}}</tool_call>\n'
        '<tool_call>{"name": "write_file", "arguments": {"path": "b.py", "content": "2"}}</tool_call>',
    )
    common._recover_text_tool_call(r)
    tc = r.choices[0].message.tool_calls
    assert len(tc) == 1 and json.loads(tc[0].function.arguments)["path"] == "a.py"


def test_a_real_structured_tool_call_is_left_untouched():
    real = [litellm.types.utils.ChatCompletionMessageToolCall(
        id="1", type="function", function=litellm.types.utils.Function(name="read_file", arguments='{"path":"x"}'),
    )]
    r = _response(content=None, tool_calls=real)
    common._recover_text_tool_call(r)
    assert r.choices[0].message.tool_calls == real


def test_plain_text_with_no_tool_call_is_left_untouched():
    r = _response("Here is my plan for the project...")
    common._recover_text_tool_call(r)
    assert not r.choices[0].message.tool_calls
    assert r.choices[0].message.content == "Here is my plan for the project..."


def test_malformed_json_inside_the_tags_does_not_raise():
    r = _response("<tool_call>\n{not json at all\n</tool_call>")
    common._recover_text_tool_call(r)   # must not raise
    assert not r.choices[0].message.tool_calls


def test_missing_name_is_ignored():
    r = _response('<tool_call>{"arguments": {"path": "a"}}</tool_call>')
    common._recover_text_tool_call(r)
    assert not r.choices[0].message.tool_calls


def test_string_arguments_are_kept_as_is():
    r = _response('<tool_call>{"name": "run_shell", "arguments": "{\\"command\\": \\"pytest\\"}"}</tool_call>')
    common._recover_text_tool_call(r)
    assert json.loads(r.choices[0].message.tool_calls[0].function.arguments) == {"command": "pytest"}


def test_completion_wrapper_recovers_in_place(monkeypatch):
    """The actual litellm.completion()/acompletion() patch applied once in common.py."""
    raw = _response('<tool_call>{"name": "write_file", "arguments": {"path": "x", "content": "y"}}</tool_call>')
    monkeypatch.setattr(litellm, "_devteam_tool_call_fallback", False, raising=False)
    monkeypatch.setattr(litellm, "completion", lambda *a, **k: raw)
    common._patch_text_tool_call_fallback()
    out = litellm.completion()
    assert out.choices[0].message.tool_calls[0].function.name == "write_file"
