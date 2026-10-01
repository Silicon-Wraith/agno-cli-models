import asyncio

import pytest
from agno.models.message import Message
from agno.tools import tool
from agno.tools.function import Function

from agno_cli_models._common import find_cli_session, session_marker
from agno_cli_models.codex.model import CodexModel
from agno_cli_models.codex.protocol import PAUSE_TEXT
from agno_cli_models.codex.rpc import DONE
from agno_cli_models.errors import CliProtocolError, ModelRateLimitError


class FakeRpc:
    """Scripted app-server. `script` maps a request method to (result, notifications to enqueue after it)."""

    def __init__(self, script, after_reply=None):
        self.script = script
        self.after_reply = after_reply or {}
        self.inbox = asyncio.Queue()
        self.requests = []
        self.replies = []
        self.sent = []
        self.closed = False

    async def request(self, method, params):
        self.requests.append((method, params))
        result, notes = self.script.get(method, ({}, []))
        for n in notes:
            await self.inbox.put(n)
        return result

    async def send(self, obj):
        self.sent.append(obj)

    async def reply(self, request_id, result):
        self.replies.append((request_id, result))
        for n in self.after_reply.get(request_id, []):
            await self.inbox.put(n)

    async def close(self, grace_s=3.0):
        self.closed = True


def note(method, params):
    return {"jsonrpc": "2.0", "method": method, "params": params}


FINAL = [
    note("item/started", {"item": {"id": "i1", "type": "agentMessage", "phase": "final_answer"}}),
    note("item/agentMessage/delta", {"itemId": "i1", "delta": "hello"}),
    note("item/completed", {"item": {"id": "i1", "type": "agentMessage", "phase": "final_answer", "text": "hello"}}),
    note("thread/tokenUsage/updated", {"tokenUsage": {"last": {"inputTokens": 50, "cachedInputTokens": 10, "outputTokens": 5}}}),
    note("turn/completed", {"turn": {"id": "u1", "status": "completed"}}),
]
THREAD = {"thread": {"id": "t1"}, "model": "gpt-5.6-sol"}


def script(turn_notes=FINAL):
    return {"initialize": ({}, []), "thread/start": (THREAD, []), "thread/resume": (THREAD, []),
            "turn/start": ({"turn": {"id": "u1"}}, list(turn_notes))}


def model(rpc, **over):
    async def spawn(argv, env):
        model.argv, model.env = argv, env
        return rpc

    return CodexModel(spawn_fn=spawn, **over)


@pytest.fixture(autouse=True)
def pinned_version(monkeypatch):
    monkeypatch.setattr("agno_cli_models.codex.model.installed_version", lambda b: "0.155.1")


def test_plain_answer_isolation_and_session_marker(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk")
    rpc = FakeRpc(script())
    m = model(rpc)
    msgs = [Message(role="system", content="SYS"), Message(role="user", content="hi")]
    out = asyncio.run(m.aresponse(msgs))
    assert out.content == "hello"
    methods = [r[0] for r in rpc.requests]
    assert methods == ["initialize", "thread/start", "turn/start"]
    start = rpc.requests[1][1]
    assert start["baseInstructions"] == "SYS" and start["allowProviderModelFallback"] is False and start["environments"] == []
    assert rpc.requests[2][1]["input"][0]["text"] == "hi"
    assert "OPENAI_API_KEY" not in model.env
    assert "features.apps=false" in model.argv
    assert find_cli_session(msgs, "codex") == "t1"
    assert m.last_run_info["observed_model"] == "gpt-5.6-sol" and m.last_run_info["cli_version"] == "0.155.1"
    assert rpc.closed


def test_resume_uses_persisted_thread():
    rpc = FakeRpc(script())
    msgs = [Message(role="user", content="q1"), Message(role="assistant", content="a1", provider_data=session_marker("codex", "t0")),
            Message(role="user", content="q2")]
    asyncio.run(model(rpc).aresponse(msgs))
    assert rpc.requests[1][0] == "thread/resume" and rpc.requests[1][1]["threadId"] == "t0"
    assert rpc.requests[2][1]["input"][0]["text"] == "q2"


def test_history_without_thread_is_replayed():
    rpc = FakeRpc(script())
    msgs = [Message(role="user", content="q1"), Message(role="assistant", content="a1"), Message(role="user", content="q2")]
    asyncio.run(model(rpc).aresponse(msgs))
    assert rpc.requests[2][1]["input"][0]["text"].startswith("Conversation so far:")


def add(a: int, b: int) -> int:
    """Add.

    Args:
        a: first
        b: second
    """
    return a + b


def test_dynamic_tool_call_runs_through_agno():
    call = {"jsonrpc": "2.0", "id": 7, "method": "item/tool/call", "params": {"callId": "c1", "tool": "add", "arguments": {"a": 2, "b": 5}}}
    rpc = FakeRpc(script([call]), after_reply={7: FINAL})
    fn = Function.from_callable(add)
    fn.process_entrypoint()
    msgs = [Message(role="user", content="add")]
    asyncio.run(model(rpc).aresponse(msgs, tools=[fn]))
    assert rpc.replies[0] == (7, {"contentItems": [{"type": "inputText", "text": "7"}], "success": True})
    assert rpc.requests[1][1]["dynamicTools"][0]["name"] == "add"
    assert any(m.role == "tool" and m.tool_call_id == "c1" for m in msgs)


@tool(requires_confirmation=True)
def wipe(path: str) -> str:
    """Delete.

    Args:
        path: what to delete
    """
    return "wiped"


def test_approval_tool_declines_interrupts_and_pauses():
    call = {"jsonrpc": "2.0", "id": 8, "method": "item/tool/call", "params": {"callId": "c9", "tool": "wipe", "arguments": {"path": "/x"}}}
    interrupted = [note("turn/completed", {"turn": {"id": "u1", "status": "interrupted"}})]
    rpc = FakeRpc({**script([call]), "turn/interrupt": ({}, interrupted)})
    wipe.process_entrypoint()

    class Run:
        session_id, model_provider_data, metrics, requirements = "a", None, None, None

    run = Run()
    msgs = [Message(role="user", content="delete /x")]
    asyncio.run(model(rpc).aresponse(msgs, tools=[wipe], run_response=run))
    assert rpc.replies[0][1]["success"] is False
    assert ("turn/interrupt", {"threadId": "t1", "turnId": "u1"}) in rpc.requests
    assert run.requirements and run.requirements[0].tool_execution.tool_name == "wipe"
    assert find_cli_session(msgs, "codex") == "t1"


def test_continue_after_approval_sends_outcome_and_reuses_result():
    rpc = FakeRpc(script())
    msgs = [Message(role="user", content="delete /x"),
            Message(role="assistant", tool_calls=[{"id": "c9", "type": "function", "function": {"name": "wipe", "arguments": "{}"}}],
                    provider_data=session_marker("codex", "t1")),
            Message(role="tool", tool_call_id="c9", tool_name="wipe", content="wiped")]
    wipe.process_entrypoint()
    asyncio.run(model(rpc).aresponse(msgs, tools=[wipe]))
    assert rpc.requests[1][0] == "thread/resume"
    text = rpc.requests[2][1]["input"][0]["text"]
    assert "c9" in text and "wiped" in text


def test_rate_limit_notification_raises_and_closes():
    rl = [note("account/rateLimits/updated", {"rateLimits": {"rateLimitReachedType": "rate_limit_reached"}})]
    rpc = FakeRpc(script(rl))
    with pytest.raises(ModelRateLimitError):
        asyncio.run(model(rpc).aresponse([Message(role="user", content="q")]))
    assert rpc.closed


def test_server_exit_mid_turn_is_protocol_error():
    rpc = FakeRpc(script([DONE]))
    with pytest.raises(CliProtocolError):
        asyncio.run(model(rpc).aresponse([Message(role="user", content="q")]))


def test_other_server_requests_are_declined():
    approval = {"jsonrpc": "2.0", "id": 3, "method": "item/commandExecution/requestApproval", "params": {}}
    rpc = FakeRpc(script([approval] + FINAL))
    asyncio.run(model(rpc).aresponse([Message(role="user", content="q")]))
    assert rpc.replies[0] == (3, {"decision": "decline"})


def test_fingerprint_follows_sandbox():
    assert CodexModel(sandbox="read-only").config_fingerprint() != CodexModel(sandbox="workspace-write").config_fingerprint()


# ---- fix round 1 ----

def approval_call(i, cid):
    return {"jsonrpc": "2.0", "id": i, "method": "item/tool/call", "params": {"callId": cid, "tool": "wipe", "arguments": {"path": "/x"}}}


class _Run:
    session_id, model_provider_data, metrics, requirements = "a", None, None, None


def test_pause_race_completed_turn_gives_empty_content_and_no_extra_message():
    rpc = FakeRpc(script([approval_call(8, "c9")]), after_reply={8: FINAL})
    rpc.script["turn/interrupt"] = ({}, [])
    wipe.process_entrypoint()
    run = _Run()
    msgs = [Message(role="user", content="delete /x")]
    out = asyncio.run(model(rpc).aresponse(msgs, tools=[wipe], run_response=run))
    assert run.requirements
    assert not out.content
    assert [m.role for m in msgs] == ["user", "assistant"]


def test_interrupt_error_is_tolerated_once_paused():
    class Rpc2(FakeRpc):
        async def request(self, method, params):
            if method == "turn/interrupt":
                self.requests.append((method, params))
                raise CliProtocolError("no active turn", "Codex", "x")
            return await super().request(method, params)

    rpc = Rpc2(script([approval_call(8, "c9")]), after_reply={8: FINAL})
    wipe.process_entrypoint()
    run = _Run()
    asyncio.run(model(rpc).aresponse([Message(role="user", content="d")], tools=[wipe], run_response=run))
    assert run.requirements


def test_two_approvals_one_interrupt_and_later_tools_not_run():
    interrupted = [note("turn/completed", {"turn": {"id": "u1", "status": "interrupted"}})]
    add_call = {"jsonrpc": "2.0", "id": 10, "method": "item/tool/call", "params": {"callId": "c3", "tool": "add", "arguments": {"a": 1, "b": 2}}}
    rpc = FakeRpc({**script([approval_call(8, "c9"), approval_call(9, "c10"), add_call]), "turn/interrupt": ({}, interrupted)})
    wipe.process_entrypoint()
    fn = Function.from_callable(add)
    fn.process_entrypoint()
    msgs = [Message(role="user", content="d")]
    asyncio.run(model(rpc).aresponse(msgs, tools=[wipe, fn], run_response=_Run()))
    assert [r[0] for r in rpc.requests].count("turn/interrupt") == 1
    assert rpc.replies[-1] == (10, {"contentItems": [{"type": "inputText", "text": PAUSE_TEXT}], "success": False})
    assert not any(m.role == "tool" and m.tool_call_id == "c3" for m in msgs)


@pytest.mark.parametrize("method,expected", [
    ("item/commandExecution/requestApproval", {"decision": "decline"}),
    ("item/fileChange/requestApproval", {"decision": "decline"}),
    ("applyPatchApproval", {"decision": "denied"}),
    ("execCommandApproval", {"decision": "denied"}),
    ("mcpServer/elicitation/request", {"action": "decline"}),
])
def test_server_request_replies_follow_schema(method, expected):
    rpc = FakeRpc(script([{"jsonrpc": "2.0", "id": 3, "method": method, "params": None}] + FINAL))
    asyncio.run(model(rpc).aresponse([Message(role="user", content="q")]))
    assert rpc.replies[0] == (3, expected)


def test_unknown_server_request_gets_jsonrpc_error():
    rpc = FakeRpc(script([{"jsonrpc": "2.0", "id": 4, "method": "weird/thing", "params": {}}] + FINAL))
    asyncio.run(model(rpc).aresponse([Message(role="user", content="q")]))
    assert {"jsonrpc": "2.0", "id": 4, "error": {"code": -32601, "message": "not supported by agno-cli-models"}} in rpc.sent
    assert not rpc.replies


# ---- final review fixes ----

class StaleRpc(FakeRpc):
    async def request(self, method, params):
        if method == "thread/resume":
            self.requests.append((method, params))
            raise CliProtocolError("codex thread/resume failed: {'code': -32600, 'message': 'no rollout found'}")
        return await super().request(method, params)


def _stale_msgs():
    return [Message(role="user", content="q1"), Message(role="assistant", content="a1", provider_data=session_marker("codex", "gone")),
            Message(role="user", content="q2")]


def test_stale_thread_falls_back_to_new_thread():
    rpc = StaleRpc(script())
    msgs = _stale_msgs()
    out = asyncio.run(model(rpc).aresponse(msgs))
    assert out.content == "hello"
    assert [r[0] for r in rpc.requests] == ["initialize", "thread/resume", "thread/start", "turn/start"]
    assert rpc.requests[3][1]["input"][0]["text"].startswith("Conversation so far:")
    assert find_cli_session(msgs, "codex") == "t1"


def test_rate_limit_after_resume_is_not_retried():
    rl = [note("account/rateLimits/updated", {"rateLimits": {"rateLimitReachedType": "rate_limit_reached"}})]
    rpc = FakeRpc(script(rl))
    with pytest.raises(ModelRateLimitError):
        asyncio.run(model(rpc).aresponse(_stale_msgs()))
    assert [r[0] for r in rpc.requests] == ["initialize", "thread/resume", "turn/start"]


def test_server_exit_during_resume_is_not_retried():
    class ExitRpc(FakeRpc):
        async def request(self, method, params):
            self.requests.append((method, params))
            if method == "thread/resume":
                raise CliProtocolError("codex app-server exited")
            return self.script.get(method, ({}, []))[0]

    rpc = ExitRpc(script())
    with pytest.raises(CliProtocolError):
        asyncio.run(model(rpc).aresponse(_stale_msgs()))
    assert "thread/start" not in [r[0] for r in rpc.requests]


def test_thread_response_without_id_is_protocol_error():
    rpc = FakeRpc({**script(), "thread/start": ({}, [])})
    with pytest.raises(CliProtocolError):
        asyncio.run(model(rpc).aresponse([Message(role="user", content="q")]))


def test_client_info_version_follows_package(monkeypatch):
    import agno_cli_models

    monkeypatch.setattr(agno_cli_models, "__version__", "9.9.9")
    rpc = FakeRpc(script())
    asyncio.run(model(rpc).aresponse([Message(role="user", content="q")]))
    assert rpc.requests[0][1]["clientInfo"]["version"] == "9.9.9"
