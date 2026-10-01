"""Real Claude Code and Codex calls. Run on purpose only:

    .venv/bin/pytest -m integration -v
"""

import asyncio
import json
import subprocess
import sys
import textwrap

import pytest
from agno.agent import Agent
from agno.db.sqlite import SqliteDb
from agno.tools import tool
from pydantic import BaseModel

from agno_cli_models import ClaudeCodeModel, CodexModel

pytestmark = pytest.mark.integration
CALLS = {"secret": 0, "wipe": 0}


def claude(**kw):
    return ClaudeCodeModel(id="claude-haiku-4-5-20251001", effort="low", **kw)


def codex(**kw):
    return CodexModel(id="gpt-5.6-sol", effort="low", **kw)


def _recording_spawn(recorded):
    from agno_cli_models.codex.rpc import Rpc

    class Recording(asyncio.Queue):
        async def put(self, item):
            if isinstance(item, dict):
                recorded.append(item)
            await super().put(item)

    async def spawn(argv, env):
        rpc = await Rpc.spawn(argv, env)
        rpc.inbox = Recording()
        return rpc

    return spawn


MODELS = [pytest.param(claude, id="claude"), pytest.param(codex, id="codex")]


def get_secret(key: str) -> str:
    """Look up a secret by key.

    Args:
        key: secret name
    """
    CALLS["secret"] += 1
    return {"alpha": "4242"}.get(key, "not found")


@tool(requires_confirmation=True)
def wipe(path: str) -> str:
    """Delete a file.

    Args:
        path: file to delete
    """
    CALLS["wipe"] += 1
    return f"deleted {path}"


class City(BaseModel):
    city: str
    country: str


@pytest.mark.parametrize("make", MODELS)
def test_custom_tool_runs_through_agno(make, request):
    CALLS["secret"] = 0
    recorded: list = []
    kw = {}
    if request.node.callspec.id == "codex":
        kw["spawn_fn"] = _recording_spawn(recorded)
    agent = Agent(model=make(**kw), tools=[get_secret])
    out = agent.run("What is the secret for key 'alpha'? Answer with only the value.")
    assert "4242" in str(out.content) and CALLS["secret"] >= 1
    info = agent.model.last_run_info
    assert info["observed_model"] and info["cli_version"] and len(info["config_fingerprint"]) == 64
    if recorded:
        usage = [(m["params"]["tokenUsage"].get("last"), m["params"]["tokenUsage"].get("total"))
                 for m in recorded if m.get("method") == "thread/tokenUsage/updated"]
        print("CODEX_TOKEN_USAGE_SEQUENCE", json.dumps(usage))


@pytest.mark.parametrize("make", MODELS)
def test_structured_output(make):
    out = Agent(model=make(), output_schema=City).run("Capital of Norway?")
    assert isinstance(out.content, City) and out.content.city.lower() == "oslo"


@pytest.mark.parametrize("make", MODELS)
def test_session_resumes_across_agent_instances(make, tmp_path):
    db = SqliteDb(db_file=str(tmp_path / "s.db"))
    first = Agent(model=make(), db=db, session_id="s1", add_history_to_context=True)
    first.run("Remember the code word: PELICAN. Reply OK.")
    second = Agent(model=make(), db=db, session_id="s1", add_history_to_context=True)
    out = second.run("What was the code word? One word.")
    assert "pelican" in str(out.content).lower()


@pytest.mark.parametrize("make", MODELS)
def test_approval_confirm_runs_tool_exactly_once(make):
    CALLS["wipe"] = 0
    agent = Agent(model=make(), tools=[wipe])
    run = agent.run("Delete /tmp/demo.txt using the wipe tool.")
    assert run.is_paused
    for req in run.active_requirements:
        req.confirm()
    done = agent.continue_run(run_response=run, requirements=run.requirements)
    assert CALLS["wipe"] == 1 and not done.is_paused


@pytest.mark.parametrize("make", MODELS)
def test_approval_reject_never_runs_tool(make):
    CALLS["wipe"] = 0
    agent = Agent(model=make(), tools=[wipe])
    run = agent.run("Delete /tmp/demo.txt using the wipe tool.")
    for req in run.active_requirements:
        req.reject()
    agent.continue_run(run_response=run, requirements=run.requirements)
    assert CALLS["wipe"] == 0


def test_claude_isolation_sees_only_agno_tools(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-be-hidden")
    import claude_agent_sdk as sdk

    seen = {}
    real = sdk.query

    def spy(*, prompt, options):
        async def gen():
            async for msg in real(prompt=prompt, options=options):
                if isinstance(msg, sdk.SystemMessage) and msg.subtype == "init":
                    seen.update(msg.data)
                yield msg

        return gen()

    Agent(model=claude(query_fn=spy), tools=[get_secret]).run("Say OK.")
    assert seen["apiKeySource"] == "none"
    assert all(t.startswith("mcp__agno__") for t in seen["tools"])
    assert [s["name"] for s in seen.get("mcp_servers", [])] == ["agno"]
    assert not seen.get("skills")


def test_sync_run_leaves_no_event_loop_warnings():
    code = textwrap.dedent("""
        from agno.agent import Agent
        from agno_cli_models import ClaudeCodeModel
        Agent(model=ClaudeCodeModel(id="claude-haiku-4-5-20251001", effort="low")).run("Say OK.")
    """)
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stderr
    assert "Event loop is closed" not in done.stderr


def test_codex_app_server_starts_no_mcp_servers():
    from agno_cli_models._env import clean_env
    from agno_cli_models.codex.protocol import app_server_argv
    from agno_cli_models.codex.rpc import Rpc

    async def go():
        rpc = await Rpc.spawn(app_server_argv("codex"), clean_env())
        try:
            await rpc.request("initialize", {"clientInfo": {"name": "agno_cli_models_it", "version": "0"},
                                             "capabilities": {"experimentalApi": True}})
            await rpc.send({"jsonrpc": "2.0", "method": "initialized"})
            return await rpc.request("mcpServerStatus/list", {})
        finally:
            await rpc.close()

    res = asyncio.run(go())
    print("MCP_STATUS_LIST", json.dumps(res))
    for server in res.get("data", []):
        assert not server.get("runtimeStatus"), f"MCP server has runtime status: {server}"
        assert not server.get("serverInfo"), f"MCP server connected: {server}"
        assert not server.get("tools"), f"MCP server started with tools: {server}"
