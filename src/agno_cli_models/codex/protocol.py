"""codex app-server request builders and a per-turn notification tracker.

Shapes verified against `codex app-server generate-json-schema` for
codex-cli 0.155.1.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agno.metrics import MessageMetrics
from agno.models.message import Message
from agno.models.response import ModelResponse
from agno.tools.function import Function

from agno_cli_models._common import config_hash, strict_schema, text_of
from agno_cli_models.errors import CliProtocolError, ContextWindowExceededError, ModelProviderError, ModelRateLimitError

# Everything codex-cli 0.155.1 lets a client switch off that would otherwise carry the user's
# setup (skills, plugins, hooks, memories, personality) or extra agent behaviour into a call.
# Measured in reports/2026-10-01-codex-isolation-findings.md; the README lists what remains.
_ISOLATION = (
    'web_search="disabled"',
    "features.apps=false",
    "include_apps_instructions=false",
    "skills.include_instructions=false",
    "skills.bundled.enabled=false",
    "features.skill_search=false",
    "features.skill_mcp_dependency_install=false",
    "features.plugins=false",
    "features.remote_plugin=false",
    "features.hooks=false",
    "features.personality=false",
    "memories.use_memories=false",
    "memories.generate_memories=false",
    "features.sleep_tool=false",
    "features.shell_snapshot=false",
    "features.image_generation=false",
    "features.browser_use=false",
    "features.computer_use=false",
    "features.goals=false",
    "features.tool_suggest=false",
    "tools.experimental_request_user_input.enabled=false",
    "features.default_mode_request_user_input=false",
    # Multi-agent: the model catalog still offers the collaboration tools for gpt-5.6-sol and no
    # config key stops a spawn (README, Known limits). Blank the "/root" prompt that teaches
    # delegation, but keep <multi_agent_mode>: it tells the model not to spawn unless asked.
    "features.multi_agent=false",
    "features.multi_agent_v2.enabled=false",
    "features.multi_agent_v2.usage_hint_enabled=false",
    'features.multi_agent_v2.root_agent_usage_hint_text=""',
    "include_collaboration_mode_instructions=false",
)
FIXED_CONFIG = [arg for kv in _ISOLATION for arg in ("-c", kv)]
PERSONALITY = "none"
ALLOWED_SANDBOXES = ("read-only", "workspace-write")
PAUSE_TEXT = ("This tool call needs a human decision and has not run. Stop now and end your turn; "
              "you will be told the outcome and can continue.")
_RATE_INFOS = ("usageLimitExceeded", "rateLimitExceeded")


def _codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def _user_mcp_overrides() -> list[str]:
    """`codex app-server` has no --ignore-user-config, and `-c mcp_servers={}` does not clear
    servers from the user's config.toml, so disable each one by name."""
    path = _codex_home() / "config.toml"
    try:
        servers = tomllib.loads(path.read_text()).get("mcp_servers", {})
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        # Codex would still load this file, so its servers cannot be disabled by name.
        raise CliProtocolError(f"cannot read Codex user config {path} to disable its MCP servers: {exc}") from exc
    if not isinstance(servers, dict):
        raise CliProtocolError(f"Codex user config {path} has a non-table mcp_servers")
    out: list[str] = []
    for name in servers:
        key = name if re.fullmatch(r"[A-Za-z0-9_-]+", name) else json.dumps(name)
        out += ["-c", f"mcp_servers.{key}.enabled=false"]
    return out


def _user_skill_overrides() -> list[str]:
    """Codex has no switch for user skills, only `skills.config` entries that disable one
    SKILL.md by path, so disable every one under $CODEX_HOME/skills. The bundled skills in
    `.system` are covered by `skills.bundled.enabled=false`. A missing or unreadable directory
    yields nothing, which Codex cannot read either."""
    root = _codex_home() / "skills"
    paths = sorted(p for p in root.rglob("SKILL.md")
                   if not any(part.startswith(".") for part in p.relative_to(root).parts))
    if not paths:
        return []
    entries = ", ".join(f"{{path={json.dumps(str(p))}, enabled=false}}" for p in paths)
    return ["-c", f"skills.config=[{entries}]"]


def app_server_argv(binary: str) -> list[str]:
    return [binary, "app-server", *FIXED_CONFIG, *_user_mcp_overrides(), *_user_skill_overrides()]


def fingerprint(sandbox: str, builtin_tools: bool) -> str:
    return config_hash({"cli": "codex", "config": FIXED_CONFIG, "approval_policy": "never",
                        "allow_model_fallback": False, "sandbox": sandbox, "builtin_tools": builtin_tools,
                        "env": "clean_env/v2", "schema": "strict", "user_mcp_servers": "disabled",
                        "user_skills": "disabled", "personality": PERSONALITY})


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
                              "allowProviderModelFallback": False, "dynamicTools": dynamic_tools,
                              "personality": PERSONALITY}
    if not builtin_tools:
        params["environments"] = []
    return params


def thread_resume_params(*, thread_id: str, model_id: str, system: str, cwd: str, sandbox: str) -> dict:
    _check_sandbox(sandbox)
    return {"threadId": thread_id, "model": model_id, "baseInstructions": system or "You are a helpful assistant.",
            "approvalPolicy": "never", "sandbox": sandbox, "cwd": cwd, "personality": PERSONALITY}


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
            # `last` is per model call, not per turn: sum across the turn's calls.
            last = (params.get("tokenUsage") or {}).get("last") or {}
            for k, v in last.items():
                if isinstance(v, int):
                    self.usage[k] = self.usage.get(k, 0) + v
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
