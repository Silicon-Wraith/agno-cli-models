"""ClaudeCodeModel: an Agno Model whose tool loop runs inside Claude Code,
driven by the official Claude Agent SDK and the CLI's own login."""

from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable

import claude_agent_sdk as sdk
from agno.exceptions import AgnoError
from agno.models.response import ModelResponse
from pydantic import BaseModel

from agno_cli_models._base import CliModel
from agno_cli_models._bridge import ToolBridge
from agno_cli_models._common import find_cli_session, last_user_text, session_marker, split_system, transcript_prompt
from agno_cli_models.claude.matcher import CallIdMatcher
from agno_cli_models.claude.options import SERVER, build_options, fingerprint
from agno_cli_models.claude.translate import check_result, observed_model, rate_limit_error, usage_metrics
from agno_cli_models.errors import CliProtocolError
from agno_cli_models.versions import check_supported, installed_version

_DONE = object()


@dataclass
class ClaudeCodeModel(CliModel):
    id: str = "claude-opus-5-5"
    name: str = "ClaudeCode"
    provider: str = "ClaudeCode"
    effort: str = "high"
    builtin_tools: tuple[str, ...] = ()
    permission_mode: str | None = None
    max_turns: int | None = 50
    cli_path: str | None = None
    query_fn: Callable | None = field(default=None, repr=False)
    _version: str | None = field(default=None, repr=False)
    CLI = "claude"

    def config_fingerprint(self) -> str:
        return fingerprint(self.builtin_tools, self.permission_mode)

    def _cli(self) -> str:
        path = self.cli_path or shutil.which("claude")
        if not path:
            raise FileNotFoundError("claude CLI not found on PATH; install Claude Code or set cli_path")
        return path

    def _cli_version(self, path: str) -> str | None:
        if self._version is None:
            self._version = installed_version(path)
            check_supported("claude", self._version)
        return self._version

    def _typed(self, exc: Exception) -> Exception:
        """SDK and transport failures become CliProtocolError; Agno's own errors pass through."""
        if isinstance(exc, AgnoError):
            return exc
        err = CliProtocolError(str(exc) or type(exc).__name__, self.name, self.id)
        err.__cause__ = exc
        return err

    async def _drive(self, messages, response_format, tools, tool_call_limit, run_response, stream: bool) -> AsyncIterator[ModelResponse]:
        cli = self._cli()
        version = self._cli_version(cli)
        bridge = ToolBridge(self, tools, messages, tool_call_limit)
        matcher = CallIdMatcher()
        queue: asyncio.Queue = asyncio.Queue()
        system, rest = split_system(messages)
        resume = find_cli_session(messages, "claude")
        state: dict[str, Any] = {"session": resume, "result": False}

        async def pre_tool_use(inp: dict, tool_use_id: str | None, ctx: Any) -> dict:
            name = inp.get("tool_name", "")
            prefix = f"mcp__{SERVER}__"
            if not name.startswith(prefix):
                return {}
            short = name[len(prefix):]
            call_id = matcher.record(short, inp.get("tool_input"), tool_use_id)
            if bridge.needs_pause(short) and bridge.precomputed(call_id) is None:
                return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "defer"}}
            return {}

        def handler_for(fn_name: str):
            async def handler(args: dict) -> dict:
                call_id = matcher.take(fn_name, args)
                done = bridge.precomputed(call_id)
                if done is not None:
                    text, ok = done
                else:
                    marker = session_marker("claude", state["session"]) if state["session"] else None
                    events, text, ok = await bridge.run(call_id, fn_name, args, provider_data=marker)
                    for ev in events:
                        await queue.put(ev)
                return {"content": [{"type": "text", "text": text}], "is_error": not ok}

            return handler

        sdk_tools = [
            sdk.tool(fn.name, fn.description or fn.name, fn.parameters or {"type": "object", "properties": {}})(handler_for(fn.name))
            for fn in bridge.functions.values()
        ]
        schema = None
        if isinstance(response_format, type) and issubclass(response_format, BaseModel):
            schema = response_format.model_json_schema()
        elif isinstance(response_format, dict):
            schema = response_format
        options = build_options(
            model_id=self.id, effort=self.effort, cli_path=cli, cwd=self.resolved_cwd(), system_prompt=system,
            builtin_tools=self.builtin_tools, permission_mode=self.permission_mode,
            agno_tool_names=list(bridge.functions), mcp_server=sdk.create_sdk_mcp_server(SERVER, tools=sdk_tools) if sdk_tools else None,
            output_schema=schema, resume=resume, stream=stream,
            hooks={"PreToolUse": [sdk.HookMatcher(matcher=None, hooks=[pre_tool_use])]}, max_turns=self.max_turns,
        )
        if resume:
            prompt = "Continue." if rest and rest[-1].role == "tool" else last_user_text(rest)
        else:
            prompt = transcript_prompt(rest)
        resuming_after_pause = bool(resume) and bool(rest) and rest[-1].role == "tool"
        query = self.query_fn or sdk.query

        async def pump() -> None:
            gen = None
            try:
                gen = query(prompt=prompt, options=options)
                async for msg in gen:
                    await queue.put(msg)
            except Exception as exc:
                await queue.put(self._typed(exc))
            finally:
                try:
                    if gen is not None:
                        await gen.aclose()
                except Exception:
                    pass  # the run is over; a failed close must not hide its outcome
                finally:
                    await queue.put(_DONE)

        task = asyncio.create_task(pump())
        try:
            while True:
                item = await queue.get()
                if item is _DONE:
                    if not state["result"]:
                        raise CliProtocolError("claude ended without a result", self.name, self.id)
                    return
                if isinstance(item, Exception):
                    raise item
                if isinstance(item, ModelResponse):
                    yield item
                elif isinstance(item, sdk.StreamEvent):
                    ev = item.event
                    if ev.get("type") == "content_block_delta" and ev.get("delta", {}).get("type") == "text_delta":
                        yield ModelResponse(content=ev["delta"]["text"])
                elif isinstance(item, sdk.RateLimitEvent):
                    err = rate_limit_error(item, self.name, self.id)
                    if err is not None:
                        raise err
                elif isinstance(item, sdk.SystemMessage) and item.subtype == "init":
                    state["session"] = item.data.get("session_id") or state["session"]
                elif isinstance(item, sdk.ResultMessage):
                    state["result"] = True
                    state["session"] = item.session_id or state["session"]
                    check_result(item, self.name, self.id)
                    info = {"observed_model": observed_model(item), "cli_version": version, "cli_session_id": state["session"]}
                    deferred = item.deferred_tool_use
                    if deferred is not None:
                        short = deferred.name.removeprefix(f"mcp__{SERVER}__")
                        for ev in await bridge.pause(deferred.id, short, deferred.input or {}, provider_data=session_marker("claude", state["session"])):
                            yield ev
                        text = ""
                    elif item.structured_output is not None:
                        text = json.dumps(item.structured_output)
                    else:
                        text = item.result or ""
                    yield self.usage_event(usage_metrics(item), text, info)
                    if resuming_after_pause or deferred is not None:
                        return
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
