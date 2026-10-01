"""What a child CLI process may see of the parent environment.

API keys, bearer tokens and base URLs are removed so a CLI can never silently
switch from the subscription login to API billing or another endpoint. Claude Code session variables are removed because a
parent Claude Code session sets several (session id, effort) that would
otherwise leak into the child.
"""

from __future__ import annotations

import os
from typing import Mapping

REMOVED_KEYS = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "CODEX_API_KEY", "CLAUDECODE",
)
REMOVED_PREFIXES = ("CLAUDE_",)
KEPT_KEYS = ("CLAUDE_CONFIG_DIR", "CLAUDE_CODE_ENTRYPOINT")


def is_removed(key: str) -> bool:
    if key in KEPT_KEYS:
        return False
    return key in REMOVED_KEYS or key.startswith(REMOVED_PREFIXES)


def clean_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """A full environment for a child we spawn ourselves (Codex)."""
    source = os.environ if environ is None else environ
    return {k: v for k, v in source.items() if not is_removed(k)}


def blanking_overrides(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Overrides for a child spawned by the Claude Agent SDK, which merges the
    parent environment under `options.env` and cannot delete keys. Blank
    values hide them without touching the parent's `os.environ`."""
    source = os.environ if environ is None else environ
    return {k: "" for k in source if is_removed(k)}
