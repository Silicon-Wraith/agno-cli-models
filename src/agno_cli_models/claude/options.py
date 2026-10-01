"""ClaudeAgentOptions with isolation always on.

Measured on Claude Code 2.1.286: without these settings a session sees the
user's claude.ai connectors, settings, memory and project context.
"""

from __future__ import annotations

import json
from typing import Any, Sequence

import claude_agent_sdk as sdk

from agno_cli_models._common import config_hash
from agno_cli_models._env import blanking_overrides

SERVER = "agno"
ISOLATION_SETTINGS = {"disableClaudeAiConnectors": True, "autoMemoryEnabled": False}
REFUSED_PERMISSION_MODES = ("bypassPermissions",)
ENV_POLICY = "blank-api-keys-and-claude-vars/v2"


def fingerprint(builtin_tools: Sequence[str], permission_mode: str | None) -> str:
    return config_hash({
        "cli": "claude",
        "settings": ISOLATION_SETTINGS,
        "setting_sources": [],
        "strict_mcp_config": True,
        "slash_commands": False,
        "builtin_tools": sorted(builtin_tools),
        "permission_mode": permission_mode,
        "env": ENV_POLICY,
    })


def build_options(*, model_id: str, effort: str, cli_path: str, cwd: str, system_prompt: str,
                  builtin_tools: Sequence[str], permission_mode: str | None, agno_tool_names: Sequence[str],
                  mcp_server: Any | None, output_schema: dict | None, resume: str | None, stream: bool,
                  hooks: dict | None, max_turns: int | None) -> sdk.ClaudeAgentOptions:
    if permission_mode in REFUSED_PERMISSION_MODES:
        raise ValueError(f"permission_mode {permission_mode!r} is not allowed")
    allowed = list(builtin_tools) + [f"mcp__{SERVER}__{n}" for n in agno_tool_names]
    kwargs: dict[str, Any] = dict(
        model=model_id,
        effort=effort,
        cli_path=cli_path,
        cwd=cwd,
        system_prompt=system_prompt or "",
        tools=list(builtin_tools),
        allowed_tools=allowed,
        setting_sources=[],
        strict_mcp_config=True,
        settings=json.dumps(ISOLATION_SETTINGS, sort_keys=True),
        extra_args={"disable-slash-commands": None},
        env=blanking_overrides(),
        include_partial_messages=stream,
        max_turns=max_turns,
    )
    if permission_mode:
        kwargs["permission_mode"] = permission_mode
    if hooks:
        kwargs["hooks"] = hooks
    if mcp_server is not None:
        kwargs["mcp_servers"] = {SERVER: mcp_server}
    if output_schema is not None:
        kwargs["output_format"] = {"type": "json_schema", "schema": output_schema}
    if resume:
        kwargs["resume"] = resume
    return sdk.ClaudeAgentOptions(**kwargs)
