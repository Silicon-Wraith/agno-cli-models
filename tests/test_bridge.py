import asyncio
from dataclasses import dataclass

from agno.models.message import Message
from agno.models.response import ModelResponseEvent
from agno.tools import tool
from agno.tools.function import Function

from agno_cli_models import CliModel
from agno_cli_models._bridge import ToolBridge


@dataclass
class Dummy(CliModel):
    id: str = "dummy"
    CLI = "claude"

CALLS = []


def add(a: int, b: int) -> int:
    """Add two numbers.

    Args:
        a: first
        b: second
    """
    CALLS.append((a, b))
    return a + b


@tool(requires_confirmation=True)
def wipe(path: str) -> str:
    """Delete a path.

    Args:
        path: what to delete
    """
    CALLS.append(("wipe", path))
    return "wiped"


def bridge(messages=None, limit=None):
    functions = [Function.from_callable(add), wipe]
    for f in functions:
        f.process_entrypoint()
    return ToolBridge(Dummy(), functions, messages if messages is not None else [], limit)


def test_run_executes_through_agno_and_records_messages():
    CALLS.clear()
    msgs = []
    events, text, ok = asyncio.run(bridge(msgs).run("call-1", "add", {"a": 2, "b": 3}, provider_data={"k": "v"}))
    assert (text, ok) == ("5", True)
    assert CALLS == [(2, 3)]
    assert msgs[0].role == "assistant" and msgs[0].tool_calls[0]["id"] == "call-1"
    assert msgs[0].provider_data == {"k": "v"}
    assert msgs[1].role == "tool" and msgs[1].tool_call_id == "call-1"
    assert any(e.event == ModelResponseEvent.tool_call_completed.value for e in events)


def test_unknown_tool_reports_failure_without_raising():
    events, text, ok = asyncio.run(bridge().run("c", "nope", {}))
    assert ok is False and "unknown tool" in text and events == []


def test_needs_pause():
    b = bridge()
    assert b.needs_pause("wipe") is True
    assert b.needs_pause("add") is False
    assert b.needs_pause("nope") is False


def test_pause_emits_paused_event_without_running_the_tool():
    CALLS.clear()
    msgs = []
    events = asyncio.run(bridge(msgs).pause("call-9", "wipe", {"path": "/tmp/x"}))
    assert CALLS == []
    assert any(e.event == ModelResponseEvent.tool_call_paused.value for e in events)
    assert msgs[0].tool_calls[0]["id"] == "call-9"


def test_precomputed_results_come_from_continue_run_messages():
    msgs = [Message(role="tool", tool_call_id="call-9", content="wiped", tool_call_error=False),
            Message(role="tool", tool_call_id="call-8", content="denied", tool_call_error=True)]
    b = bridge(msgs)
    assert b.precomputed("call-9") == ("wiped", True)
    assert b.precomputed("call-8") == ("denied", False)
    assert b.precomputed("call-7") is None


def test_tool_call_limit_counts_across_calls():
    CALLS.clear()
    b = bridge(limit=1)
    _, text1, ok1 = asyncio.run(b.run("c1", "add", {"a": 1, "b": 1}))
    _, text2, ok2 = asyncio.run(b.run("c2", "add", {"a": 1, "b": 1}))
    assert (text1, ok1) == ("2", True)
    # Agno answers the over-limit call with a failed tool result and does not run the tool.
    assert ok2 is False and "tool call limit reached" in text2.lower()
    assert CALLS == [(1, 1)]
