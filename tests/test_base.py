import asyncio
from dataclasses import dataclass

import pytest
from agno.metrics import MessageMetrics
from agno.models.message import Message
from agno.metrics import RunMetrics
from agno.models.response import ModelResponse, ModelResponseEvent, ToolExecution
from pydantic import BaseModel

from agno_cli_models._base import CliModel
from agno_cli_models._common import find_cli_session
from agno_cli_models.errors import CliTimeoutError


class City(BaseModel):
    city: str


@dataclass
class Scripted(CliModel):
    id: str = "scripted"
    CLI = "claude"
    final: str = "done"
    delay: float = 0.0

    def config_fingerprint(self) -> str:
        return "f" * 64

    async def _drive(self, messages, response_format, tools, tool_call_limit, run_response, stream):
        await asyncio.sleep(self.delay)
        yield ModelResponse(content="do")
        yield ModelResponse(content="ne")
        m = MessageMetrics()
        m.input_tokens, m.output_tokens = 10, 2
        yield self.usage_event(m, self.final, {"observed_model": "m-1", "cli_version": "2.1.286", "cli_session_id": "sess-1"})


class FakeRun:
    session_id = "agno-1"
    model_provider_data = None
    metrics = None
    requirements = None


def test_aresponse_returns_final_content_and_marks_session():
    msgs = [Message(role="user", content="hi")]
    run = FakeRun()
    out = asyncio.run(Scripted().aresponse(msgs, run_response=run))
    assert out.content == "done"
    assert msgs[-1].role == "assistant" and msgs[-1].content == "done"
    assert find_cli_session(msgs, "claude") == "sess-1"
    info = run.model_provider_data["agno_cli_models"]
    assert info["observed_model"] == "m-1" and info["config_fingerprint"] == "f" * 64 and info["cli"] == "claude"


def test_structured_output_is_parsed():
    out = asyncio.run(Scripted(final='{"city": "Oslo"}').aresponse([Message(role="user", content="q")], response_format=City))
    assert out.parsed == City(city="Oslo")


def test_stream_yields_deltas_and_records_message():
    msgs = [Message(role="user", content="hi")]

    async def collect():
        return [e async for e in Scripted().aresponse_stream(msgs)]

    events = asyncio.run(collect())
    assert "".join(e.content or "" for e in events) == "done"
    assert msgs[-1].content == "done"
    assert find_cli_session(msgs, "claude") == "sess-1"


def test_aresponse_times_out():
    with pytest.raises(CliTimeoutError):
        asyncio.run(Scripted(delay=5, timeout_s=0.2).aresponse([Message(role="user", content="hi")]))


def test_sync_response_works_inside_running_loop():
    async def outer():
        return Scripted().response([Message(role="user", content="hi")])

    assert asyncio.run(outer()).content == "done"


def test_empty_final_content_appends_no_assistant_message():
    msgs = [Message(role="user", content="hi")]
    asyncio.run(Scripted(final="").aresponse(msgs))
    assert [m.role for m in msgs] == ["user"]


def test_resolved_cwd_defaults_to_private_empty_dir(tmp_path):
    assert Scripted(cwd=str(tmp_path)).resolved_cwd() == str(tmp_path)
    default = Scripted().resolved_cwd()
    assert "agno-cli-models" in default


@dataclass
class Pausing(Scripted):
    async def _drive(self, messages, response_format, tools, tool_call_limit, run_response, stream):
        yield ModelResponse(content="pre")
        yield ModelResponse(event=ModelResponseEvent.tool_call_paused.value, tool_executions=[ToolExecution(tool_name="t")])
        yield self.usage_event(MessageMetrics(), "", {"observed_model": "m", "cli_version": "1", "cli_session_id": "s"})


def test_stream_with_empty_final_appends_no_assistant_message():
    msgs = [Message(role="user", content="hi")]

    async def collect():
        return [e async for e in Pausing().aresponse_stream(msgs)]

    asyncio.run(collect())
    assert [m.role for m in msgs] == ["user"]


def test_pause_adds_requirement_in_both_paths():
    run = FakeRun()
    run.requirements = None
    out = asyncio.run(Pausing().aresponse([Message(role="user", content="hi")], run_response=run))
    assert len(run.requirements) == 1 and out.tool_executions[0].tool_name == "t"
    run2 = FakeRun()
    run2.requirements = None

    async def collect():
        return [e async for e in Pausing().aresponse_stream([Message(role="user", content="hi")], run_response=run2)]

    asyncio.run(collect())
    assert len(run2.requirements) == 1


def test_metrics_accumulate_into_run_metrics():
    run = FakeRun()
    run.metrics = RunMetrics()
    asyncio.run(Scripted().aresponse([Message(role="user", content="hi")], run_response=run))
    assert run.metrics.input_tokens == 10 and run.metrics.output_tokens == 2


def test_last_run_info_contents():
    m = Scripted()
    asyncio.run(m.aresponse([Message(role="user", content="hi")]))
    assert m.last_run_info == {
        "observed_model": "m-1", "cli_version": "2.1.286", "cli_session_id": "sess-1",
        "cli": "claude", "config_fingerprint": "f" * 64,
    }


def test_parse_failure_is_recorded_not_raised(monkeypatch):
    import agno_cli_models._base as base

    warnings = []
    monkeypatch.setattr(base, "log_warning", warnings.append)
    m = Scripted(final="not json")
    out = asyncio.run(m.aresponse([Message(role="user", content="q")], response_format=City))
    assert out.parsed is None and out.content == "not json"
    assert warnings and m.last_run_info["parse_error"]


def test_stream_times_out():
    async def collect():
        return [e async for e in Scripted(delay=5, timeout_s=0.2).aresponse_stream([Message(role="user", content="hi")])]

    with pytest.raises(CliTimeoutError):
        asyncio.run(collect())
