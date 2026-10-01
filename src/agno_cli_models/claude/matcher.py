"""Pair the SDK hook's tool_use_id with the tool handler call.

The in-process MCP handler does not receive the tool_use_id, but the
PreToolUse hook does, with the same arguments. Pairing by (name, arguments)
keeps parallel calls to one tool apart; order is the fallback only for
identical arguments.
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Mapping
from uuid import uuid4

from agno_cli_models._common import canon_args


class CallIdMatcher:
    def __init__(self) -> None:
        self._ids: dict[tuple[str, str], deque[str]] = defaultdict(deque)

    def record(self, name: str, args: Mapping | None, tool_use_id: str | None) -> str:
        call_id = tool_use_id or str(uuid4())
        self._ids[(name, canon_args(args))].append(call_id)
        return call_id

    def take(self, name: str, args: Mapping | None) -> str:
        queue = self._ids.get((name, canon_args(args)))
        return queue.popleft() if queue else str(uuid4())
