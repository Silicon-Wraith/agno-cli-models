import asyncio
import sys
import textwrap

import pytest

from agno_cli_models.codex.rpc import DONE, Rpc
from agno_cli_models.errors import CliProtocolError

SERVER = textwrap.dedent("""
    import json, sys
    for line in sys.stdin:
        msg = json.loads(line)
        if msg.get("method") == "ping":
            print(json.dumps({"jsonrpc": "2.0", "method": "note", "params": {"n": 1}}), flush=True)
            print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"pong": True}}), flush=True)
        elif msg.get("method") == "fail":
            print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -1, "message": "nope"}}), flush=True)
        elif msg.get("method") == "ask":
            print(json.dumps({"jsonrpc": "2.0", "id": 99, "method": "item/tool/call", "params": {}}), flush=True)
        elif "result" in msg and msg.get("id") == 99:
            print(json.dumps({"jsonrpc": "2.0", "method": "got-reply", "params": msg["result"]}), flush=True)
        elif msg.get("method") == "quit":
            sys.exit(0)
""")


async def server():
    return await Rpc.spawn([sys.executable, "-c", SERVER], env={"PATH": "/usr/bin"})


def test_request_response_and_notifications():
    async def go():
        rpc = await server()
        try:
            assert await rpc.request("ping", {}) == {"pong": True}
            assert (await rpc.inbox.get())["method"] == "note"
        finally:
            await rpc.close()

    asyncio.run(go())


def test_error_response_raises():
    async def go():
        rpc = await server()
        try:
            with pytest.raises(CliProtocolError):
                await rpc.request("fail", {})
        finally:
            await rpc.close()

    asyncio.run(go())


def test_server_request_and_reply():
    async def go():
        rpc = await server()
        try:
            await rpc.send({"jsonrpc": "2.0", "method": "ask"})
            req = await rpc.inbox.get()
            assert req["method"] == "item/tool/call" and req["id"] == 99
            await rpc.reply(99, {"ok": 1})
            assert (await rpc.inbox.get()) == {"jsonrpc": "2.0", "method": "got-reply", "params": {"ok": 1}}
        finally:
            await rpc.close()

    asyncio.run(go())


def test_exit_puts_done_in_inbox():
    async def go():
        rpc = await server()
        await rpc.send({"jsonrpc": "2.0", "method": "quit"})
        assert await asyncio.wait_for(rpc.inbox.get(), 5) is DONE
        await rpc.close()

    asyncio.run(go())


def test_rpc_close_kills_hung_process():
    async def go():
        rpc = await Rpc.spawn([sys.executable, "-c", "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"], env={"PATH": "/usr/bin"})
        await rpc.close(grace_s=0.5)
        assert rpc.proc.returncode is not None

    asyncio.run(go())
