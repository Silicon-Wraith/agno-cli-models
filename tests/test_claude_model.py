import asyncio
import json

import claude_agent_sdk as sdk
import pytest
from agno.models.message import Message
from agno.models.response import ModelResponseEvent
from agno.tools import tool

from agno_cli_models._common import find_cli_session, session_marker
from agno_cli_models.claude.model import ClaudeCodeModel
from agno_cli_models.errors import CliProtocolError, ModelRateLimitError

INIT = sdk.SystemMessage(subtype="init", data={"session_id": "sess-1", "claude_code_version": "2.1.286", "tools": [], "apiKeySource": "none"})


def result(**over):
    base = dict(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1, session_id="sess-1",
                total_cost_usd=0.01, result="hello", model_usage={"claude-opus-5-5": {"inputTokens": 5, "outputTokens": 3}})
    base.update(over)
    return sdk.ResultMessage(**base)


class FakeQuery:
    def __init__(self, *items):
        self.items = items
        self.calls = []

    def __call__(self, *, prompt, options):
        self.calls.append((prompt, options))
        items = self.items

        async def gen():
            for item in items:
                if isinstance(item, BaseException):
                    raise item
                if callable(item):
                    item = await item(options)
                yield item

        return gen()


def model(fake, **over):
    return ClaudeCodeModel(query_fn=fake, cli_path="claude-test", **over)


@pytest.fixture(autouse=True)
def pinned_version(monkeypatch):
    monkeypatch.setattr("agno_cli_models.claude.model.installed_version", lambda path: "2.1.286")


def test_plain_answer_and_run_info():
    fake = FakeQuery(INIT, result())
    m = model(fake)
    msgs = [Message(role="system", content="SYS"), Message(role="user", content="hi")]
    out = asyncio.run(m.aresponse(msgs))
    assert out.content == "hello"
    prompt, options = fake.calls[0]
    assert prompt == "hi" and options.system_prompt == "SYS" and options.resume is None
    assert m.last_run_info["observed_model"] == "claude-opus-5-5"
    assert m.last_run_info["cli_version"] == "2.1.286"
    assert find_cli_session(msgs, "claude") == "sess-1"


def test_no_history_means_fresh_session():
    fake = FakeQuery(INIT, result())
    asyncio.run(model(fake).aresponse([Message(role="user", content="first")]))
    assert fake.calls[0][1].resume is None


def test_resume_uses_persisted_session_and_sends_only_new_message():
    fake = FakeQuery(INIT, result())
    msgs = [Message(role="user", content="q1"),
            Message(role="assistant", content="a1", provider_data=session_marker("claude", "sess-0")),
            Message(role="user", content="q2")]
    asyncio.run(model(fake).aresponse(msgs))
    prompt, options = fake.calls[0]
    assert options.resume == "sess-0" and prompt == "q2"


def test_history_without_session_is_replayed():
    fake = FakeQuery(INIT, result())
    msgs = [Message(role="user", content="q1"), Message(role="assistant", content="a1"), Message(role="user", content="q2")]
    asyncio.run(model(fake).aresponse(msgs))
    assert fake.calls[0][0].startswith("Conversation so far:")


def test_structured_output_wins_over_text():
    fake = FakeQuery(INIT, result(structured_output={"city": "Oslo"}, result="ignored"))
    out = asyncio.run(model(fake).aresponse([Message(role="user", content="q")], response_format={"type": "object"}))
    assert json.loads(out.content) == {"city": "Oslo"}


def test_rejected_rate_limit_event_raises():
    ev = sdk.RateLimitEvent(rate_limit_info=sdk.RateLimitInfo(status="rejected", rate_limit_type="five_hour"), uuid="u", session_id="s")
    with pytest.raises(ModelRateLimitError):
        asyncio.run(model(FakeQuery(INIT, ev, result())).aresponse([Message(role="user", content="q")]))


def test_error_result_raises():
    with pytest.raises(ModelRateLimitError):
        asyncio.run(model(FakeQuery(INIT, result(is_error=True, api_error_status=429))).aresponse([Message(role="user", content="q")]))


def test_stream_deltas():
    delta = sdk.StreamEvent(uuid="u", session_id="s", event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hel"}})
    delta2 = sdk.StreamEvent(uuid="u", session_id="s", event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": "lo"}})
    fake = FakeQuery(INIT, delta, delta2, result())

    async def collect():
        return [e async for e in model(fake).aresponse_stream([Message(role="user", content="q")])]

    events = asyncio.run(collect())
    assert "".join(e.content or "" for e in events) == "hello"
    assert fake.calls[0][1].include_partial_messages is True


@tool(requires_confirmation=True)
def wipe(path: str) -> str:
    """Delete a path.

    Args:
        path: what to delete
    """
    return "wiped"


class Deferred:
    def __init__(self):
        self.id, self.name, self.input = "tu-1", "mcp__agno__wipe", {"path": "/x"}


def test_deferred_tool_use_pauses_the_run():
    fake = FakeQuery(INIT, result(deferred_tool_use=Deferred(), result=None))
    wipe.process_entrypoint()

    class Run:
        session_id, model_provider_data, metrics, requirements = "a", None, None, None

    run = Run()
    msgs = [Message(role="user", content="delete /x")]
    out = asyncio.run(model(fake).aresponse(msgs, tools=[wipe], run_response=run))
    assert run.requirements and run.requirements[0].tool_execution.tool_name == "wipe"
    assert find_cli_session(msgs, "claude") == "sess-1"
    assert out.tool_executions


def test_unsupported_version_warns(monkeypatch):
    monkeypatch.setattr("agno_cli_models.claude.model.installed_version", lambda path: "9.9.9")
    from agno_cli_models.versions import UnsupportedCliVersionWarning

    with pytest.warns(UnsupportedCliVersionWarning):
        asyncio.run(model(FakeQuery(INIT, result())).aresponse([Message(role="user", content="q")]))


def test_fingerprint_matches_options_module():
    from agno_cli_models.claude.options import fingerprint

    m = ClaudeCodeModel(builtin_tools=("Read",))
    assert m.config_fingerprint() == fingerprint(("Read",), None)


# ---- final review fixes ----

class _Run:
    session_id, model_provider_data, metrics, requirements = "a", None, None, None


def test_sdk_exception_is_typed():
    with pytest.raises(CliProtocolError) as info:
        asyncio.run(model(FakeQuery(INIT, sdk.ProcessError("Command failed with exit code 1", exit_code=1)))
                    .aresponse([Message(role="user", content="q")]))
    assert isinstance(info.value.__cause__, sdk.ProcessError)


def test_query_fn_raising_on_call_is_typed():
    def boom(*, prompt, options):
        raise sdk.CLIConnectionError("cannot connect")

    with pytest.raises(CliProtocolError):
        asyncio.run(model(boom, timeout_s=5).aresponse([Message(role="user", content="q")]))


def test_stream_without_result_raises():
    with pytest.raises(CliProtocolError, match="ended without a result"):
        asyncio.run(model(FakeQuery(INIT)).aresponse([Message(role="user", content="q")]))


def test_aclose_failure_does_not_hang():
    class BadGen:
        def __init__(self):
            self.items = [INIT, result()]

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self.items:
                raise StopAsyncIteration
            return self.items.pop(0)

        async def aclose(self):
            raise RuntimeError("close failed")

    out = asyncio.run(asyncio.wait_for(model(lambda **kw: BadGen()).aresponse([Message(role="user", content="q")]), 5))
    assert out.content == "hello"


@tool(name="get__value", requires_confirmation=True)
def get_value(key: str) -> str:
    """Get a value.

    Args:
        key: which
    """
    return "v"


def test_tool_name_with_double_underscore_pauses_under_full_name():
    class D:
        id, name, input = "tu-2", "mcp__agno__get__value", {"key": "k"}

    get_value.process_entrypoint()
    run = _Run()
    asyncio.run(model(FakeQuery(INIT, result(deferred_tool_use=D(), result=None)))
                .aresponse([Message(role="user", content="q")], tools=[get_value], run_response=run))
    assert run.requirements[0].tool_execution.tool_name == "get__value"


RUNS = {"n": 0}


@tool(requires_confirmation=True)
def zap(path: str) -> str:
    """Zap a path.

    Args:
        path: what to zap
    """
    RUNS["n"] += 1
    return f"zapped {path}"


def test_hook_and_mcp_handler_two_step_approval(monkeypatch):
    captured = {}
    real = sdk.create_sdk_mcp_server

    def capture(name, version="1.0.0", tools=None):
        captured.update({t.name: t.handler for t in tools or []})
        return real(name, version=version, tools=tools)

    monkeypatch.setattr(sdk, "create_sdk_mcp_server", capture)
    RUNS["n"] = 0
    zap.process_entrypoint()
    seen = {}

    async def first_call(options):
        hook = options.hooks["PreToolUse"][0].hooks[0]
        seen["first"] = await hook({"tool_name": "mcp__agno__zap", "tool_input": {"path": "/x"}}, "tu-9", None)
        return result(deferred_tool_use=type("D", (), {"id": "tu-9", "name": "mcp__agno__zap", "input": {"path": "/x"}})(), result=None)

    run = _Run()
    msgs = [Message(role="user", content="zap /x")]
    asyncio.run(model(FakeQuery(INIT, first_call)).aresponse(msgs, tools=[zap], run_response=run))
    assert seen["first"]["hookSpecificOutput"]["permissionDecision"] == "defer"
    assert RUNS["n"] == 0 and run.requirements

    # Agno ran the confirmed tool once; resume with its result in the messages.
    msgs.append(Message(role="tool", tool_call_id="tu-9", tool_name="zap", content="zapped /x"))

    async def second_call(options):
        hook = options.hooks["PreToolUse"][0].hooks[0]
        seen["second"] = await hook({"tool_name": "mcp__agno__zap", "tool_input": {"path": "/x"}}, "tu-9", None)
        seen["handler"] = await captured["zap"]({"path": "/x"})
        return result(result="done")

    fake = FakeQuery(INIT, second_call)
    out = asyncio.run(model(fake).aresponse(msgs, tools=[zap]))
    prompt, options = fake.calls[0]
    assert prompt == "Continue." and options.resume == "sess-1"
    assert seen["second"] == {}
    assert seen["handler"]["content"][0]["text"] == "zapped /x" and seen["handler"]["is_error"] is False
    assert RUNS["n"] == 0
    assert out.content == "done"


class SeqQuery:
    """One scripted item list per call."""

    def __init__(self, *runs):
        self.runs = list(runs)
        self.calls = []

    def __call__(self, *, prompt, options):
        self.calls.append((prompt, options))
        return FakeQuery(*self.runs[len(self.calls) - 1])(prompt=prompt, options=options)


STALE = [Message(role="user", content="q1"),
         Message(role="assistant", content="a1", provider_data=session_marker("claude", "gone")),
         Message(role="user", content="q2")]
NEW_INIT = sdk.SystemMessage(subtype="init", data={"session_id": "sess-new"})


def _stale_msgs():
    return [m.model_copy() for m in STALE]


@pytest.mark.parametrize("failure", [
    pytest.param([sdk.ProcessError("Command failed with exit code 1", exit_code=1)], id="raised"),
    pytest.param([result(is_error=True, subtype="error_during_execution", result=None, errors=["No conversation found"])], id="error-result"),
    pytest.param([], id="empty"),
])
def test_stale_session_falls_back_to_fresh_session(failure):
    fake = SeqQuery(failure, [NEW_INIT, result(session_id="sess-new")])
    msgs = _stale_msgs()
    out = asyncio.run(model(fake).aresponse(msgs))
    assert out.content == "hello"
    assert [c[1].resume for c in fake.calls] == ["gone", None]
    assert fake.calls[0][0] == "q2" and fake.calls[1][0].startswith("Conversation so far:")
    assert find_cli_session(msgs, "claude") == "sess-new"


def test_failure_after_init_is_not_retried():
    fake = SeqQuery([INIT, sdk.ProcessError("boom", exit_code=1)], [INIT, result()])
    with pytest.raises(CliProtocolError):
        asyncio.run(model(fake).aresponse(_stale_msgs()))
    assert len(fake.calls) == 1


def test_rate_limit_while_resuming_is_not_retried():
    fake = SeqQuery([result(is_error=True, api_error_status=429)], [INIT, result()])
    with pytest.raises(ModelRateLimitError):
        asyncio.run(model(fake).aresponse(_stale_msgs()))
    assert len(fake.calls) == 1


def test_failure_without_resume_is_not_retried():
    fake = SeqQuery([sdk.ProcessError("boom", exit_code=1)], [INIT, result()])
    with pytest.raises(CliProtocolError):
        asyncio.run(model(fake).aresponse([Message(role="user", content="q")]))
    assert len(fake.calls) == 1
