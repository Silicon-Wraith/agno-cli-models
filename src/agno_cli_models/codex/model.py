"""CodexModel: an Agno Model whose tool loop runs inside `codex app-server`,
using the CLI's own ChatGPT login."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable

from agno.models.response import ModelResponse
from agno.utils.log import log_warning
from pydantic import BaseModel

from agno_cli_models._base import CliModel
from agno_cli_models._bridge import ToolBridge
from agno_cli_models._common import find_cli_session, last_user_text, session_marker, split_system, strip_nulls, transcript_prompt
from agno_cli_models._env import clean_env
from agno_cli_models.codex.protocol import (
    PAUSE_TEXT,
    TurnTracker,
    app_server_argv,
    continuation_text,
    dynamic_tool,
    fingerprint,
    thread_resume_params,
    thread_start_params,
    turn_start_params,
)
from agno_cli_models.codex.rpc import DONE, Rpc
from agno_cli_models.errors import CliProtocolError
from agno_cli_models.versions import check_supported, installed_version


_DECLINES = {
    "item/commandExecution/requestApproval": {"decision": "decline"},
    "item/fileChange/requestApproval": {"decision": "decline"},
    "applyPatchApproval": {"decision": "denied"},
    "execCommandApproval": {"decision": "denied"},
    "mcpServer/elicitation/request": {"action": "decline"},
}


@dataclass
class CodexModel(CliModel):
    id: str = "gpt-5.6-sol"
    name: str = "Codex"
    provider: str = "Codex"
    effort: str = "high"
    sandbox: str = "read-only"
    builtin_tools: bool = False
    codex_bin: str = "codex"
    spawn_fn: Callable[[list[str], dict], Awaitable[Any]] | None = field(default=None, repr=False)
    _version: str | None = field(default=None, repr=False)
    _version_checked: bool = field(default=False, repr=False)
    CLI = "codex"

    def config_fingerprint(self) -> str:
        return fingerprint(self.sandbox, self.builtin_tools)

    def _cli_version(self) -> str | None:
        if not self._version_checked:
            self._version_checked = True
            self._version = installed_version(self.codex_bin)
            check_supported("codex", self._version)
        return self._version

    @staticmethod
    def _pending_results(rest: list) -> list:
        """Tool results Agno appended after the last assistant tool call (continue_run)."""
        out = []
        for m in reversed(rest):
            if m.role == "tool":
                out.append(m)
            else:
                break
        return list(reversed(out))

    async def _drive(self, messages, response_format, tools, tool_call_limit, run_response, stream: bool) -> AsyncIterator[ModelResponse]:
        version = self._cli_version()
        bridge = ToolBridge(self, tools, messages, tool_call_limit)
        system, rest = split_system(messages)
        thread_id = find_cli_session(messages, "codex")
        schema = None
        if isinstance(response_format, type) and issubclass(response_format, BaseModel):
            schema = response_format.model_json_schema()
        elif isinstance(response_format, dict):
            schema = response_format

        spawn = self.spawn_fn or Rpc.spawn
        rpc = await spawn(app_server_argv(self.codex_bin), clean_env())
        tracker = TurnTracker(model_name=self.name, model_id=self.id)
        try:
            from agno_cli_models import __version__

            await rpc.request("initialize", {"clientInfo": {"name": "agno_cli_models", "version": __version__},
                                             "capabilities": {"experimentalApi": True}})
            await rpc.send({"jsonrpc": "2.0", "method": "initialized"})
            cwd = self.resolved_cwd()
            res = None
            if thread_id:
                try:
                    res = await rpc.request("thread/resume", thread_resume_params(thread_id=thread_id, model_id=self.id, system=system, cwd=cwd, sandbox=self.sandbox))
                except CliProtocolError as exc:
                    # Only an error reply means the server is up and refused the thread
                    # (for example its session file is gone); anything else is fatal.
                    if not str(exc).startswith("codex thread/resume failed"):
                        raise
                    log_warning(f"codex: could not resume thread {thread_id} ({exc}); starting a new thread from the transcript")
                else:
                    pending = self._pending_results(rest)
                    text = continuation_text(pending) if pending else last_user_text(rest)
            if res is None:
                res = await rpc.request("thread/start", thread_start_params(
                    model_id=self.id, system=system, cwd=cwd, sandbox=self.sandbox, builtin_tools=self.builtin_tools,
                    dynamic_tools=[dynamic_tool(f) for f in bridge.functions.values()]))
                text = transcript_prompt(rest)
            try:
                thread_id = res["thread"]["id"]
            except (KeyError, TypeError) as exc:
                raise CliProtocolError(f"codex thread response has no thread id: {res!r}"[:500], self.name, self.id) from exc
            tracker.model = res.get("model") or self.id
            marker = session_marker("codex", thread_id)
            turn = await rpc.request("turn/start", turn_start_params(thread_id=thread_id, text=text, effort=self.effort,
                                                                    builtin_tools=self.builtin_tools, output_schema=schema))
            turn_id = turn.get("turn", {}).get("id")

            paused = False
            while not tracker.done:
                msg = await rpc.inbox.get()
                if msg is DONE:
                    raise CliProtocolError("codex app-server exited mid-turn", self.name, self.id)
                method, params = msg.get("method"), msg.get("params") or {}
                if method == "item/tool/call" and "id" in msg:
                    name, call_id, args = params.get("tool"), params.get("callId"), params.get("arguments") or {}
                    done = bridge.precomputed(call_id)
                    if done is None and paused and not bridge.needs_pause(name):
                        await rpc.reply(msg["id"], {"contentItems": [{"type": "inputText", "text": PAUSE_TEXT}], "success": False})
                        continue
                    if done is not None:
                        text_out, ok = done
                    elif bridge.needs_pause(name):
                        await rpc.reply(msg["id"], {"contentItems": [{"type": "inputText", "text": PAUSE_TEXT}], "success": False})
                        for ev in await bridge.pause(call_id, name, args, provider_data=marker):
                            yield ev
                        if not paused:
                            paused = True
                            try:
                                await rpc.request("turn/interrupt", {"threadId": thread_id, "turnId": params.get("turnId") or turn_id})
                            except CliProtocolError:
                                pass  # the turn may already be over; the pause is recorded either way
                        continue
                    else:
                        events, text_out, ok = await bridge.run(call_id, name, args, provider_data=marker)
                        for ev in events:
                            yield ev
                    await rpc.reply(msg["id"], {"contentItems": [{"type": "inputText", "text": text_out}], "success": ok})
                elif "id" in msg and method:
                    if method in _DECLINES:
                        await rpc.reply(msg["id"], _DECLINES[method])
                    else:
                        await rpc.send({"jsonrpc": "2.0", "id": msg["id"],
                                        "error": {"code": -32601, "message": "not supported by agno-cli-models"}})
                else:
                    for ev in tracker.on_notification(method, params):
                        yield ev

            final = tracker.final_text
            if schema is not None and final:
                try:
                    final = json.dumps(strip_nulls(json.loads(final)))
                except json.JSONDecodeError:
                    pass
            info = {"observed_model": tracker.model, "cli_version": version, "cli_session_id": thread_id}
            yield self.usage_event(tracker.metrics(), "" if (paused or tracker.interrupted) else final, info)
        finally:
            await rpc.close()
