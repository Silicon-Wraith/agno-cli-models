"""Agno Model plumbing shared by both CLI models.

The CLI owns the whole tool loop, so Agno's provider hooks (invoke,
_parse_provider_response, ...) are never called; aresponse and
aresponse_stream are overridden instead.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, ClassVar, Iterator

from agno.metrics import MessageMetrics, accumulate_model_metrics
from agno.models.base import Model
from agno.models.message import Message
from agno.models.response import ModelResponse, ModelResponseEvent
from agno.run.requirement import RunRequirement
from pydantic import BaseModel

from agno_cli_models._common import run_sync, session_marker
from agno_cli_models.errors import CliTimeoutError

USAGE = "_agno_cli_models_usage"
DEFAULT_CWD = Path.home() / ".cache" / "agno-cli-models" / "empty"


@dataclass
class CliModel(Model):
    supports_native_structured_outputs: bool = True
    cwd: str | None = None
    timeout_s: float = 600.0
    last_run_info: dict = field(default_factory=dict, repr=False)
    CLI: ClassVar[str] = ""

    # Unused provider hooks: the loop lives in the CLI.
    def invoke(self, *args, **kwargs):  # type: ignore[override]
        raise NotImplementedError

    async def ainvoke(self, *args, **kwargs):  # type: ignore[override]
        raise NotImplementedError

    def invoke_stream(self, *args, **kwargs):  # type: ignore[override]
        raise NotImplementedError

    def ainvoke_stream(self, *args, **kwargs):  # type: ignore[override]
        raise NotImplementedError

    def _parse_provider_response(self, response: Any, **kwargs) -> ModelResponse:
        raise NotImplementedError

    def _parse_provider_response_delta(self, response: Any) -> ModelResponse:
        raise NotImplementedError

    # Subclass contract.
    def config_fingerprint(self) -> str:
        raise NotImplementedError

    def _drive(self, messages, response_format, tools, tool_call_limit, run_response, stream: bool) -> AsyncIterator[ModelResponse]:
        raise NotImplementedError

    def resolved_cwd(self) -> str:
        if self.cwd:
            return self.cwd
        DEFAULT_CWD.mkdir(parents=True, exist_ok=True)
        return str(DEFAULT_CWD)

    def usage_event(self, metrics: MessageMetrics, content: str, info: dict) -> ModelResponse:
        return ModelResponse(event=USAGE, response_usage=metrics, content=content, provider_data=info)

    # Shared machinery.
    async def _timed(self, gen: AsyncIterator[ModelResponse]) -> AsyncIterator[ModelResponse]:
        try:
            async with asyncio.timeout(self.timeout_s):
                async for ev in gen:
                    yield ev
        except TimeoutError as exc:
            raise CliTimeoutError(f"{self.CLI} gave no complete answer within {self.timeout_s}s", self.name, self.id) from exc
        finally:
            await gen.aclose()

    def _finish(self, ev: ModelResponse, messages: list[Message], content: str, run_response: Any) -> None:
        info = dict(ev.provider_data or {})
        info.update(cli=self.CLI, config_fingerprint=self.config_fingerprint())
        self.last_run_info = info
        if run_response is not None:
            if ev.response_usage is not None and getattr(run_response, "metrics", None) is not None:
                accumulate_model_metrics(ModelResponse(response_usage=ev.response_usage), self, self.model_type, run_response.metrics)
            data = dict(run_response.model_provider_data or {})
            data["agno_cli_models"] = info
            run_response.model_provider_data = data
        # A paused run ends with no answer; its session marker already sits on the
        # assistant tool-call message, so no empty assistant message is added.
        if content:
            marker = session_marker(self.CLI, info["cli_session_id"]) if info.get("cli_session_id") else None
            messages.append(Message(role="assistant", content=content, provider_data=marker))

    @staticmethod
    def _track_pause(ev: ModelResponse, run_response: Any) -> None:
        if ev.event == ModelResponseEvent.tool_call_paused.value and run_response is not None and ev.tool_executions:
            if run_response.requirements is None:
                run_response.requirements = []
            run_response.requirements.append(RunRequirement(tool_execution=ev.tool_executions[-1]))

    async def aresponse(  # type: ignore[override]
        self, messages: list[Message], response_format: Any = None, tools: list | None = None,
        tool_choice: Any = None, tool_call_limit: int | None = None, run_response: Any = None, **kwargs: Any,
    ) -> ModelResponse:
        out = ModelResponse(content="")
        gen = self._drive(messages, response_format, tools, tool_call_limit, run_response, False)
        async for ev in self._timed(gen):
            if ev.event == USAGE:
                out.content = ev.content or ""
                out.response_usage = ev.response_usage
                self._finish(ev, messages, out.content, run_response)
                continue
            if ev.event in (ModelResponseEvent.tool_call_completed.value, ModelResponseEvent.tool_call_paused.value):
                out.tool_executions = (out.tool_executions or []) + list(ev.tool_executions or [])
                self._track_pause(ev, run_response)
            if ev.updated_session_state is not None:
                out.updated_session_state = ev.updated_session_state
        if isinstance(response_format, type) and issubclass(response_format, BaseModel) and out.content:
            try:
                out.parsed = response_format.model_validate_json(out.content)
            except Exception:
                pass
        return out

    async def aresponse_stream(  # type: ignore[override]
        self, messages: list[Message], response_format: Any = None, tools: list | None = None,
        tool_choice: Any = None, tool_call_limit: int | None = None, stream_model_response: bool = True,
        run_response: Any = None, **kwargs: Any,
    ) -> AsyncIterator[ModelResponse]:
        streamed = ""
        gen = self._drive(messages, response_format, tools, tool_call_limit, run_response, True)
        async for ev in self._timed(gen):
            if ev.event == USAGE:
                final = ev.content or ""
                if not streamed and final:
                    streamed = final
                    yield ModelResponse(content=final)
                self._finish(ev, messages, streamed or final, run_response)
                continue
            if ev.event == ModelResponseEvent.assistant_response.value and ev.content:
                streamed += ev.content
            self._track_pause(ev, run_response)
            yield ev

    def response(self, *args: Any, **kwargs: Any) -> ModelResponse:  # type: ignore[override]
        return run_sync(self.aresponse(*args, **kwargs))

    def response_stream(self, *args: Any, **kwargs: Any) -> Iterator[ModelResponse]:  # type: ignore[override]
        async def collect() -> list[ModelResponse]:
            return [ev async for ev in self.aresponse_stream(*args, **kwargs)]

        yield from run_sync(collect())
