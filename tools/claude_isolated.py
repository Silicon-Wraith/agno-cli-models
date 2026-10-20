#!/usr/bin/env python3
"""Call Claude Code through the Claude Agent SDK with agno-cli-models' isolation, without Agno.

Only dependency: claude-agent-sdk (pip install "claude-agent-sdk>=0.2.163,<0.3").
Uses the `claude` binary on PATH and its own login (run `claude` then /login once).

Ported from agno-cli-models v0.1.2 (ClaudeCodeModel): same isolation settings, API-key
blanking, wall clock, idle limit (60 s, partial messages always on), typed errors,
rate-limit detection and CLI version check. Not ported: Agno tools and approval pauses,
and the stale-session fallback (a failed resume raises instead of replaying history).

The file is self-contained: copy it anywhere. In this repo, tests/test_claude_isolated.py
checks its isolation settings, env policy, tested versions, fingerprint and options against
the package, so a change to ClaudeCodeModel's isolation that is not mirrored here fails.

Library use (with this file on sys.path):

    import asyncio
    from claude_isolated import ask
    r = asyncio.run(ask("Review app.py for security issues.", cwd="/repo", tools=("Read", "Grep", "Glob")))
    print(r.text, r.session_id, r.usage)

Command line:

    python tools/claude_isolated.py "Say OK." --model claude-haiku-4-5-20251001 --effort low
    python tools/claude_isolated.py "Review app.py" --cwd . --tools Read,Grep,Glob --schema review.schema.json
    python tools/claude_isolated.py "And the other file?" --resume <session id from the previous run> --stream
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import claude_agent_sdk as sdk

# ---- isolation (agno_cli_models/claude/options.py and _env.py) ----

ISOLATION_SETTINGS = {"disableClaudeAiConnectors": True, "autoMemoryEnabled": False}
REFUSED_PERMISSION_MODES = ("bypassPermissions",)
REMOVED_KEYS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
                "OPENAI_API_KEY", "OPENAI_BASE_URL", "CODEX_API_KEY", "CLAUDECODE")
REMOVED_PREFIXES = ("CLAUDE_",)
KEPT_KEYS = ("CLAUDE_CONFIG_DIR", "CLAUDE_CODE_ENTRYPOINT")
TESTED_VERSIONS = ("2.1.286", "2.1.287")
DEFAULT_CWD = Path.home() / ".cache" / "claude-isolated" / "empty"


def _removed(key: str) -> bool:
    return key not in KEPT_KEYS and (key in REMOVED_KEYS or key.startswith(REMOVED_PREFIXES))


def blanking_overrides(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The SDK merges the parent environment under options.env and cannot delete keys,
    so blank values hide API keys, base URLs and Claude session variables from the CLI."""
    source = os.environ if environ is None else environ
    return {k: "" for k in source if _removed(k)}


def fingerprint(tools: Sequence[str], permission_mode: str | None, idle_timeout_s: float | None) -> str:
    config = {"cli": "claude", "settings": ISOLATION_SETTINGS, "setting_sources": [], "strict_mcp_config": True,
              "slash_commands": False, "builtin_tools": sorted(tools), "permission_mode": permission_mode,
              "env": "blank-api-keys-and-claude-vars/v2", "idle_timeout_s": idle_timeout_s}
    return hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def build_options(*, model: str, effort: str, cli_path: str, cwd: str, system_prompt: str, tools: Sequence[str],
                  permission_mode: str | None, schema: dict | None, resume: str | None, partial: bool,
                  max_turns: int | None) -> sdk.ClaudeAgentOptions:
    if permission_mode in REFUSED_PERMISSION_MODES:
        raise ValueError(f"permission_mode {permission_mode!r} is not allowed")
    kwargs: dict[str, Any] = dict(
        model=model, effort=effort, cli_path=cli_path, cwd=cwd, system_prompt=system_prompt or "",
        tools=list(tools), allowed_tools=list(tools),  # listed tools are pre-approved
        setting_sources=[], strict_mcp_config=True,
        settings=json.dumps(ISOLATION_SETTINGS, sort_keys=True),
        extra_args={"disable-slash-commands": None},
        env=blanking_overrides(), include_partial_messages=partial, max_turns=max_turns,
    )
    if permission_mode:
        kwargs["permission_mode"] = permission_mode
    if schema is not None:
        kwargs["output_format"] = {"type": "json_schema", "schema": schema}
    if resume:
        kwargs["resume"] = resume
    return sdk.ClaudeAgentOptions(**kwargs)


# ---- typed errors (agno_cli_models/errors.py and claude/translate.py) ----

class ClaudeError(Exception):
    status_code = 502


class ClaudeRateLimitError(ClaudeError):
    status_code = 429


class ClaudeContextWindowError(ClaudeError):
    status_code = 400


class ClaudeProtocolError(ClaudeError):
    """The CLI exited or spoke in a way the protocol does not allow."""


class ClaudeTimeoutError(ClaudeError):
    """No complete answer within the wall-clock limit."""
    status_code = 504


class ClaudeStallError(ClaudeTimeoutError):
    """The CLI sent nothing for idle_s seconds."""

    def __init__(self, message: str, *, idle_s: float, last_kind: str | None, answer_open: bool):
        super().__init__(message)
        self.idle_s, self.last_kind, self.answer_open = idle_s, last_kind, answer_open


_RATE = re.compile(r"rate.?limit|usage limit|too many requests|\b429\b", re.IGNORECASE)
_CONTEXT = re.compile(r"context (window|length)|prompt is too long", re.IGNORECASE)


def _check_result(result: sdk.ResultMessage) -> None:
    if not result.is_error:
        return
    text = f"{result.subtype} {result.api_error_status} {result.result} {result.errors}"[:500]
    if result.api_error_status == 429 or _RATE.search(text):
        raise ClaudeRateLimitError(text)
    if _CONTEXT.search(text):
        raise ClaudeContextWindowError(text)
    raise ClaudeError(text)


# ---- version check (agno_cli_models/versions.py) ----

class UnsupportedCliVersionWarning(UserWarning):
    pass


def installed_version(binary: str) -> str | None:
    try:
        done = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=30,
                              stdin=subprocess.DEVNULL, env={k: v for k, v in os.environ.items() if not _removed(k)})
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"\b(\d+\.\d+\.\d+)\b", done.stdout or "")
    return m.group(1) if done.returncode == 0 and m else None


# ---- the call ----

@dataclass
class Result:
    text: str
    structured: Any | None
    session_id: str | None
    observed_model: str | None
    cli_version: str | None
    usage: dict = field(default_factory=dict)
    cost_usd: float | None = None
    fingerprint: str = ""


def _usage(result: sdk.ResultMessage) -> dict:
    u = result.model_usage or {}
    cache_read = sum(x.get("cacheReadInputTokens", 0) for x in u.values())
    cache_write = sum(x.get("cacheCreationInputTokens", 0) for x in u.values())
    inp = sum(x.get("inputTokens", 0) for x in u.values()) + cache_read + cache_write
    out = sum(x.get("outputTokens", 0) for x in u.values())
    return {"input_tokens": inp, "output_tokens": out, "cache_read_tokens": cache_read, "cache_write_tokens": cache_write,
            "reasoning_tokens": sum(x.get("thinkingTokens", 0) for x in u.values()), "total_tokens": inp + out}


def _kind(item: Any) -> str:
    if isinstance(item, sdk.SystemMessage):
        return f"system:{item.subtype}"
    if isinstance(item, sdk.StreamEvent):
        return f"stream_event:{item.event.get('type')}"
    return type(item).__name__


_DONE = object()


async def ask(prompt: str, *, model: str = "claude-opus-5-5", effort: str = "high", cwd: str | None = None,
              tools: Sequence[str] = (), system_prompt: str = "", schema: dict | None = None,
              resume: str | None = None, permission_mode: str | None = None, max_turns: int | None = 50,
              timeout_s: float = 600.0, idle_timeout_s: float | None = 60.0,
              on_text: Callable[[str], None] | None = None, cli_path: str | None = None) -> Result:
    """One isolated Claude Code call. `on_text` receives answer text deltas as they stream."""
    cli = cli_path or shutil.which("claude")
    if not cli:
        raise FileNotFoundError("claude CLI not found on PATH; install Claude Code or pass cli_path")
    version = installed_version(cli)
    if version not in TESTED_VERSIONS:
        warnings.warn(f"claude CLI version {version!r} is not in the tested set {TESTED_VERSIONS}",
                      UnsupportedCliVersionWarning, stacklevel=2)
    if cwd is None:
        DEFAULT_CWD.mkdir(parents=True, exist_ok=True)
        cwd = str(DEFAULT_CWD)
    # An idle limit needs partial messages: without them a long answer arrives as one
    # message after a silence as long as its generation.
    options = build_options(model=model, effort=effort, cli_path=cli, cwd=cwd, system_prompt=system_prompt,
                            tools=tools, permission_mode=permission_mode, schema=schema, resume=resume,
                            partial=on_text is not None or idle_timeout_s is not None, max_turns=max_turns)
    queue: asyncio.Queue = asyncio.Queue()

    async def pump() -> None:
        gen = None
        try:
            gen = sdk.query(prompt=prompt, options=options)
            async for msg in gen:
                await queue.put(msg)
        except Exception as exc:
            await queue.put(exc)
        finally:
            try:
                if gen is not None:
                    await gen.aclose()
            except Exception:
                pass
            finally:
                await queue.put(_DONE)

    async def run() -> Result:
        session, last, open_text = resume, None, set()
        while True:
            try:
                async with asyncio.timeout(idle_timeout_s):  # covers only this wait
                    item = await queue.get()
            except TimeoutError:
                raise ClaudeStallError(f"claude sent nothing for {idle_timeout_s}s (last: {last})",
                                       idle_s=idle_timeout_s, last_kind=last, answer_open=bool(open_text)) from None
            if item is _DONE:
                raise ClaudeProtocolError("claude ended without a result")
            if isinstance(item, Exception):
                if isinstance(item, ClaudeError):
                    raise item
                raise ClaudeProtocolError(str(item) or type(item).__name__) from item
            last = _kind(item)
            if isinstance(item, sdk.StreamEvent):
                ev = item.event
                if ev.get("type") == "content_block_start" and (ev.get("content_block") or {}).get("type") == "text":
                    open_text.add(ev.get("index"))
                elif ev.get("type") == "content_block_stop":
                    open_text.discard(ev.get("index"))
                elif ev.get("type") == "content_block_delta" and ev.get("delta", {}).get("type") == "text_delta" and on_text:
                    on_text(ev["delta"]["text"])
            elif isinstance(item, sdk.RateLimitEvent) and item.rate_limit_info.status == "rejected":
                info = item.rate_limit_info
                raise ClaudeRateLimitError(f"rate limit {info.rate_limit_type} rejected (resets {info.resets_at})")
            elif isinstance(item, sdk.SystemMessage) and item.subtype == "init":
                session = item.data.get("session_id") or session
            elif isinstance(item, sdk.ResultMessage):
                _check_result(item)
                usage = item.model_usage or {}
                return Result(
                    text=json.dumps(item.structured_output) if item.structured_output is not None else (item.result or ""),
                    structured=item.structured_output, session_id=item.session_id or session,
                    observed_model=max(usage, key=lambda k: usage[k].get("outputTokens", 0)) if usage else None,
                    cli_version=version, usage=_usage(item), cost_usd=item.total_cost_usd,
                    fingerprint=fingerprint(tools, permission_mode, idle_timeout_s))

    task = asyncio.create_task(pump())
    try:
        async with asyncio.timeout(timeout_s):
            return await run()
    except TimeoutError as exc:
        raise ClaudeTimeoutError(f"claude gave no complete answer within {timeout_s}s") from exc
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


def main() -> int:
    ap = argparse.ArgumentParser(description="One isolated Claude Code call, without Agno.")
    ap.add_argument("prompt", help="the prompt, or - to read it from stdin")
    ap.add_argument("--model", default="claude-opus-5-5")
    ap.add_argument("--effort", default="high")
    ap.add_argument("--cwd", help="working directory (default: an empty directory)")
    ap.add_argument("--tools", default="", help="built-in tools to allow, e.g. Read,Grep,Glob (pre-approved)")
    ap.add_argument("--system", default="", help="system prompt")
    ap.add_argument("--schema", help="JSON Schema file for structured output")
    ap.add_argument("--resume", help="session id to continue")
    ap.add_argument("--max-turns", type=int, default=50)
    ap.add_argument("--timeout", type=float, default=600.0, help="wall-clock limit in seconds")
    ap.add_argument("--idle-timeout", type=float, default=60.0, help="idle limit in seconds; 0 turns it off")
    ap.add_argument("--stream", action="store_true", help="print answer text as it arrives")
    ap.add_argument("--json", action="store_true", help="print the whole result as JSON")
    a = ap.parse_args()
    prompt = sys.stdin.read() if a.prompt == "-" else a.prompt
    schema = json.loads(Path(a.schema).read_text()) if a.schema else None
    on_text = (lambda t: print(t, end="", flush=True)) if a.stream else None
    try:
        r = asyncio.run(ask(prompt, model=a.model, effort=a.effort, cwd=a.cwd,
                            tools=tuple(t for t in a.tools.split(",") if t), system_prompt=a.system, schema=schema,
                            resume=a.resume, max_turns=a.max_turns, timeout_s=a.timeout,
                            idle_timeout_s=a.idle_timeout or None, on_text=on_text))
    except ClaudeError as exc:
        extra = f" (answer open: {exc.answer_open})" if isinstance(exc, ClaudeStallError) else ""
        print(f"\n{type(exc).__name__} [{exc.status_code}]: {exc}{extra}", file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps(r.__dict__, indent=2, default=str))
    else:
        if a.stream:
            print(flush=True)
        else:
            print(r.text, flush=True)
        print(f"[session {r.session_id} | {r.observed_model} | {r.usage.get('total_tokens')} tokens]", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
