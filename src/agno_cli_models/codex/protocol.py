"""codex app-server request builders and a per-turn notification tracker.

Shapes verified against `codex app-server generate-json-schema` for
codex-cli 0.155.1.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agno.metrics import MessageMetrics
from agno.models.message import Message
from agno.models.response import ModelResponse
from agno.tools.function import Function

from agno_cli_models._common import config_hash, strict_schema, text_of
from agno_cli_models.errors import ContextWindowExceededError, ModelProviderError, ModelRateLimitError

FIXED_CONFIG = ["-c", 'web_search="disabled"', "-c", "features.apps=false"]
ALLOWED_SANDBOXES = ("read-only", "workspace-write")
PAUSE_TEXT = ("This tool call needs a human decision and has not run. Stop now and end your turn; "
              "you will be told the outcome and can continue.")
_RATE_INFOS = ("usageLimitExceeded", "rateLimitExceeded")


def app_server_argv(binary: str) -> list[str]:
    return [binary, "app-server", *FIXED_CONFIG]


def fingerprint(sandbox: str, builtin_tools: bool) -> str:
    return config_hash({"cli": "codex", "config": FIXED_CONFIG, "approval_policy": "never",
                        "allow_model_fallback": False, "sandbox": sandbox, "builtin_tools": builtin_tools,
                        "env": "clean_env/v1", "schema": "strict"})


def _check_sandbox(sandbox: str) -> None:
    if sandbox not in ALLOWED_SANDBOXES:
        raise ValueError(f"sandbox {sandbox!r} is not allowed; use one of {ALLOWED_SANDBOXES}")


def dynamic_tool(fn: Function) -> dict:
    return {"type": "function", "name": fn.name, "description": fn.description or fn.name,
            "inputSchema": fn.parameters or {"type": "object", "properties": {}}}


def thread_start_params(*, model_id: str, system: str, cwd: str, sandbox: str, builtin_tools: bool, dynamic_tools: list[dict]) -> dict:
    _check_sandbox(sandbox)
    params: dict[str, Any] = {"model": model_id, "baseInstructions": system or "You are a helpful assistant.",
                              "approvalPolicy": "never", "sandbox": sandbox, "cwd": cwd,
                              "allowProviderModelFallback": False, "dynamicTools": dynamic_tools}
    if not builtin_tools:
        params["environments"] = []
    return params


def thread_resume_params(*, thread_id: str, model_id: str, system: str, cwd: str, sandbox: str) -> dict:
    _check_sandbox(sandbox)
    return {"threadId": thread_id, "model": model_id, "baseInstructions": system or "You are a helpful assistant.",
            "approvalPolicy": "never", "sandbox": sandbox, "cwd": cwd}


def turn_start_params(*, thread_id: str, text: str, effort: str, builtin_tools: bool, output_schema: dict | None) -> dict:
    params: dict[str, Any] = {"threadId": thread_id, "input": [{"type": "text", "text": text}], "effort": effort}
    if not builtin_tools:
        params["environments"] = []
    if output_schema is not None:
        params["outputSchema"] = strict_schema(output_schema)
    return params


def continuation_text(results: list[Message]) -> str:
    lines = ["Human review of your paused tool calls is complete:"]
    for m in results:
        outcome = "was rejected or failed" if m.tool_call_error else "ran"
        lines.append(f"- {m.tool_name or 'tool'} (call {m.tool_call_id}) {outcome}; result: {text_of(m.content)}")
    lines.append("Continue from where you stopped. Do not call these tools again for the same purpose.")
    return "\n".join(lines)


@dataclass
class TurnTracker:
    model_name: str
    model_id: str
    model: str | None = None
    final_text: str = ""
    usage: dict = field(default_factory=dict)
    done: bool = False
    interrupted: bool = False
    _final_items: set = field(default_factory=set)

    def _raise(self, message: str, info: Any) -> None:
        if info in _RATE_INFOS:
            raise ModelRateLimitError(message, model_name=self.model_name, model_id=self.model_id)
        if info == "contextWindowExceeded":
            raise ContextWindowExceededError(message, model_name=self.model_name, model_id=self.model_id)
        raise ModelProviderError(message, model_name=self.model_name, model_id=self.model_id)

    def on_notification(self, method: str, params: dict) -> list[ModelResponse]:
        if method == "item/started":
            item = params.get("item", {})
            if item.get("type") == "agentMessage" and item.get("phase") == "final_answer":
                self._final_items.add(item.get("id"))
        elif method == "item/agentMessage/delta" and params.get("itemId") in self._final_items:
            return [ModelResponse(content=params.get("delta", ""))]
        elif method == "item/completed":
            item = params.get("item", {})
            if item.get("type") == "agentMessage" and item.get("phase") == "final_answer":
                self.final_text = item.get("text", "")
        elif method == "thread/tokenUsage/updated":
            self.usage = params.get("tokenUsage", {}).get("last") or {}
        elif method == "model/rerouted":
            self.model = params.get("toModel") or self.model
        elif method == "account/rateLimits/updated":
            reached = (params.get("rateLimits") or {}).get("rateLimitReachedType")
            if reached:
                raise ModelRateLimitError(f"codex rate limit reached: {reached}", model_name=self.model_name, model_id=self.model_id)
        elif method == "error":
            if not params.get("willRetry"):
                err = params.get("error") or {}
                self._raise(f"codex error: {err.get('message')}", err.get("codexErrorInfo"))
        elif method == "turn/completed":
            turn = params.get("turn", {})
            status = turn.get("status")
            if status == "failed":
                err = turn.get("error") or {}
                self._raise(f"codex turn failed: {err.get('message')}", err.get("codexErrorInfo"))
            self.interrupted = status == "interrupted"
            self.done = True
        return []

    def metrics(self) -> MessageMetrics:
        u = self.usage
        m = MessageMetrics()
        m.input_tokens = int(u.get("inputTokens", 0))
        m.cache_read_tokens = int(u.get("cachedInputTokens", 0))
        m.output_tokens = int(u.get("outputTokens", 0))
        m.reasoning_tokens = int(u.get("reasoningOutputTokens", 0))
        m.total_tokens = m.input_tokens + m.output_tokens
        return m
