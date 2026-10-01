"""Minimal JSON-RPC 2.0 client over a `codex app-server` stdio subprocess."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from agno_cli_models.errors import CliProtocolError

DONE = object()


class Rpc:
    @classmethod
    async def spawn(cls, argv: list[str], env: dict[str, str]) -> "Rpc":
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=env, limit=16 * 1024 * 1024,
        )
        return cls(proc)

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self.proc = proc
        self.next_id = 0
        self.pending: dict[int, asyncio.Future] = {}
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        assert self.proc.stdout is not None
        try:
            while True:
                line = await self.proc.stdout.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "id" in msg and "method" not in msg and msg["id"] in self.pending:
                    self.pending.pop(msg["id"]).set_result(msg)
                else:
                    await self.inbox.put(msg)
        finally:
            for fut in self.pending.values():
                if not fut.done():
                    fut.set_exception(CliProtocolError("codex app-server exited"))
            await self.inbox.put(DONE)

    async def send(self, obj: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write((json.dumps(obj) + "\n").encode())
        await self.proc.stdin.drain()

    async def request(self, method: str, params: dict) -> dict:
        self.next_id += 1
        fut = asyncio.get_running_loop().create_future()
        self.pending[self.next_id] = fut
        await self.send({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params})
        msg = await fut
        if "error" in msg:
            raise CliProtocolError(f"codex {method} failed: {msg['error']}")
        return msg.get("result", {})

    async def reply(self, request_id: Any, result: dict) -> None:
        await self.send({"jsonrpc": "2.0", "id": request_id, "result": result})

    async def close(self, grace_s: float = 3.0) -> None:
        self.reader.cancel()
        if self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), grace_s)
            except asyncio.TimeoutError:
                self.proc.kill()
                await self.proc.wait()
