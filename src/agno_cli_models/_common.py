"""Small helpers shared by both CLI models."""

from __future__ import annotations

import asyncio
import concurrent.futures
import copy
import hashlib
import json
from typing import Any, Coroutine, Mapping, TypeVar

from agno.models.message import Message

T = TypeVar("T")
SESSION_KEY = "agno_cli_models"


def text_of(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(c) for c in content)
    return json.dumps(content)


def canon_args(args: Mapping[str, Any] | None) -> str:
    return json.dumps(dict(args or {}), sort_keys=True, separators=(",", ":"))


def split_system(messages: list[Message]) -> tuple[str, list[Message]]:
    system = "\n\n".join(text_of(m.content) for m in messages if m.role == "system")
    return system, [m for m in messages if m.role != "system"]


def last_user_text(rest: list[Message]) -> str:
    last = next((m for m in reversed(rest) if m.role == "user"), None)
    return text_of(last.content) if last else ""


def transcript_prompt(rest: list[Message]) -> str:
    """Prompt for a fresh CLI session when Agno already has history: replay it."""
    last = next((m for m in reversed(rest) if m.role == "user"), None)
    history = [m for m in rest if m is not last and m.role in ("user", "assistant") and m.content]
    body = text_of(last.content) if last else ""
    if not history:
        return body
    lines = [f"{m.role.upper()}: {text_of(m.content)}" for m in history]
    return "Conversation so far:\n" + "\n".join(lines) + "\n\nUSER: " + body


def session_marker(cli: str, session_id: str) -> dict[str, str]:
    """provider_data stored on assistant messages so Agno persists the CLI session id."""
    return {f"{SESSION_KEY}_cli": cli, f"{SESSION_KEY}_session": session_id}


def find_cli_session(messages: list[Message], cli: str) -> str | None:
    for m in reversed(messages):
        data = m.provider_data or {}
        if data.get(f"{SESSION_KEY}_cli") == cli and data.get(f"{SESSION_KEY}_session"):
            return data[f"{SESSION_KEY}_session"]
    return None


def strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """OpenAI strict variant: every property required, no extra properties,
    formerly optional properties made nullable."""

    def walk(node: Any) -> Any:
        if isinstance(node, list):
            return [walk(x) for x in node]
        if not isinstance(node, dict):
            return node
        node = {k: walk(v) for k, v in node.items() if k not in ("$schema", "$id", "default")}
        if node.get("type") == "object" and "properties" in node:
            required = set(node.get("required", []))
            for name, prop in node["properties"].items():
                if name in required:
                    continue
                if "enum" in prop:
                    prop["enum"] = prop["enum"] + [None]
                elif "type" in prop:
                    types = prop["type"] if isinstance(prop["type"], list) else [prop["type"]]
                    prop["type"] = types + ["null"]
                else:
                    node["properties"][name] = {"anyOf": [prop, {"type": "null"}]}
            node["required"] = list(node["properties"])
            node["additionalProperties"] = False
        return node

    return walk(copy.deepcopy(schema))


def strip_nulls(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: strip_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [strip_nulls(v) for v in value]
    return value


def config_hash(config: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def run_sync(coro: Coroutine[Any, Any, T]) -> T:
    """Run a coroutine from sync code, even when an event loop is already running."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()
