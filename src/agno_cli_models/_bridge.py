"""Run CLI tool calls through Agno's own executor.

Both CLIs call our tools by name with JSON arguments. Routing each call
through `Model.arun_function_calls` keeps Agno's hooks, tool-call limits,
events and human-approval pauses identical to any other Agno model.
"""

from __future__ import annotations

import json
from typing import Any

from agno.models.base import Model
from agno.models.message import Message
from agno.models.response import ModelResponse
from agno.tools.function import Function
from agno.utils.tools import get_function_call_for_tool_call

from agno_cli_models._common import text_of


class ToolBridge:
    def __init__(self, model: Model, tools: list | None, messages: list[Message], tool_call_limit: int | None) -> None:
        self.model = model
        self.functions: dict[str, Function] = {t.name: t for t in (tools or []) if isinstance(t, Function)}
        self.messages = messages
        self.tool_call_limit = tool_call_limit
        self.count = 0
        self._done = {m.tool_call_id: m for m in messages if m.role == "tool" and m.tool_call_id}

    def needs_pause(self, name: str) -> bool:
        fn = self.functions.get(name)
        return bool(fn and (fn.requires_confirmation or fn.requires_user_input or fn.external_execution))

    def precomputed(self, call_id: str) -> tuple[str, bool] | None:
        msg = self._done.get(call_id)
        if msg is None:
            return None
        return text_of(msg.content), not bool(msg.tool_call_error)

    def _call(self, call_id: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
        return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}

    async def run(self, call_id: str, name: str, args: dict[str, Any], provider_data: dict | None = None) -> tuple[list[ModelResponse], str, bool]:
        call = self._call(call_id, name, args)
        fc = get_function_call_for_tool_call(call, self.functions)
        if fc is None:
            return [], f"unknown tool {name}", False
        results: list[Message] = []
        events: list[ModelResponse] = []
        async for ev in self.model.arun_function_calls(
            [fc], results, current_function_call_count=self.count, function_call_limit=self.tool_call_limit
        ):
            events.append(ev)
        self.count += len(results)
        self.messages.append(Message(role="assistant", tool_calls=[call], provider_data=provider_data))
        self.messages.extend(results)
        res = results[0] if results else None
        return events, text_of(res.content) if res else "", not bool(res and res.tool_call_error)

    async def pause(self, call_id: str, name: str, args: dict[str, Any], provider_data: dict | None = None) -> list[ModelResponse]:
        call = self._call(call_id, name, args)
        fc = get_function_call_for_tool_call(call, self.functions)
        if fc is None:
            return []
        self.messages.append(Message(role="assistant", tool_calls=[call], provider_data=provider_data))
        results: list[Message] = []
        return [ev async for ev in self.model.arun_function_calls([fc], results)]
