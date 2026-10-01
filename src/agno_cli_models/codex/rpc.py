"""Minimal JSON-RPC 2.0 client over a `codex app-server` stdio subprocess."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from agno_cli_models.errors import CliProtocolError

DONE = object()


class Rpc:
    @classmethod
    async def spawn(cls, argv: list[str], env: dict[str, str], limit: int = 16 * 1024 * 1024) -> "Rpc":
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=env, limit=limit,
        )
        return cls(proc)

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self.proc = proc
        self.next_id = 0
        self.pending: dict[int, asyncio.Future] = {}
        self.inbox: asyncio.Queue = asyncio.Queue()
        self._reader_failure: CliProtocolError | None = None
        self.reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        assert self.proc.stdout is not None
        try:
            while True:
                try:
                    line = await self.proc.stdout.readline()
                except ValueError as e:
                    # LimitOverrunError: line exceeds the limit
                    self._reader_failure = CliProtocolError(f"codex app-server sent oversized line: {e}")
                    raise self._reader_failure
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # Skip non-dict messages (do not forward to inbox)
                if not isinstance(msg, dict):
                    continue
                # Check if this is a response to a pending request
                if "id" in msg and "method" not in msg and isinstance(msg["id"], int) and msg["id"] in self.pending:
                    self.pending.pop(msg["id"]).set_result(msg)
                else:
                    await self.inbox.put(msg)
        except CliProtocolError:
            # Already stored the failure reason, re-raise it
            raise
        finally:
            # Use the specific failure reason if set, otherwise generic "exited" for clean EOF
            failure = self._reader_failure or CliProtocolError("codex app-server exited")
            for fut in self.pending.values():
                if not fut.done():
                    fut.set_exception(failure)
            await self.inbox.put(DONE)

    async def send(self, obj: dict) -> None:
        assert self.proc.stdin is not None
        try:
            self.proc.stdin.write((json.dumps(obj) + "\n").encode())
            await self.proc.stdin.drain()
        except OSError as e:
            raise CliProtocolError(f"codex app-server send failed: {e}")

    async def request(self, method: str, params: dict) -> dict:
        # Fail fast if reader is already done
        if self.reader.done():
            try:
                exc = self.reader.exception()
                if exc:
                    raise exc
            except asyncio.CancelledError:
                raise CliProtocolError("rpc closed")
            raise CliProtocolError("codex app-server exited")

        self.next_id += 1
        fut = asyncio.get_running_loop().create_future()
        self.pending[self.next_id] = fut
        try:
            await self.send({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params})
        except CliProtocolError:
            # Remove the future if send failed
            self.pending.pop(self.next_id, None)
            raise
        msg = await fut
        if "error" in msg:
            raise CliProtocolError(f"codex {method} failed: {msg['error']}")
        return msg.get("result", {})

    async def reply(self, request_id: Any, result: dict) -> None:
        await self.send({"jsonrpc": "2.0", "id": request_id, "result": result})

    async def close(self, grace_s: float = 3.0) -> None:
        self.reader.cancel()
        try:
            await self.reader
        except (asyncio.CancelledError, CliProtocolError):
            pass
        if self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), grace_s)
            except asyncio.TimeoutError:
                self.proc.kill()
                await self.proc.wait()
