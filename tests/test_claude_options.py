import json

import pytest

from agno_cli_models.claude.options import ISOLATION_SETTINGS, build_options, fingerprint


def opts(**over):
    base = dict(model_id="claude-opus-5-5", effort="high", cli_path="/usr/bin/claude", cwd="/work", system_prompt="S",
                builtin_tools=(), permission_mode=None, agno_tool_names=(), mcp_server=None, output_schema=None,
                resume=None, stream=False, hooks=None, max_turns=20)
    base.update(over)
    return build_options(**base)


def test_isolation_is_always_on(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk")
    o = opts()
    assert o.setting_sources == []
    assert o.strict_mcp_config is True
    assert json.loads(o.settings) == ISOLATION_SETTINGS
    assert ISOLATION_SETTINGS == {"disableClaudeAiConnectors": True, "autoMemoryEnabled": False}
    assert "disable-slash-commands" in o.extra_args
    assert o.env["ANTHROPIC_API_KEY"] == ""
    assert o.tools == []
    assert o.cli_path == "/usr/bin/claude"


def test_model_effort_cwd_are_pinned():
    o = opts()
    assert (o.model, o.effort, str(o.cwd), o.max_turns) == ("claude-opus-5-5", "high", "/work", 20)


def test_agno_tools_are_allowed_through_the_agno_server():
    o = opts(builtin_tools=("Read",), agno_tool_names=("add",), mcp_server=object())
    assert o.tools == ["Read"]
    assert o.allowed_tools == ["Read", "mcp__agno__add"]
    assert "agno" in o.mcp_servers


def test_output_schema_and_resume():
    o = opts(output_schema={"type": "object"}, resume="sess-1", stream=True)
    assert o.output_format == {"type": "json_schema", "schema": {"type": "object"}}
    assert o.resume == "sess-1"
    assert o.include_partial_messages is True


@pytest.mark.parametrize("mode", ["bypassPermissions"])
def test_dangerous_permission_mode_is_refused(mode):
    with pytest.raises(ValueError):
        opts(permission_mode=mode)


def test_fingerprint_tracks_tools_and_mode():
    assert fingerprint(("Read",), None) == fingerprint(("Read",), None)
    assert fingerprint(("Read",), None) != fingerprint(("Read", "Edit"), None)
    assert fingerprint(("Edit",), None) != fingerprint(("Edit",), "acceptEdits")
    assert len(fingerprint((), None)) == 64
