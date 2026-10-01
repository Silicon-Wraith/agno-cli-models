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
        import json
        server_code = textwrap.dedent("""
            import signal, time, sys, json
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            print(json.dumps({"ready": True}), flush=True)
            time.sleep(60)
        """)
        rpc = await Rpc.spawn([sys.executable, "-c", server_code], env={"PATH": "/usr/bin"})
        await asyncio.wait_for(rpc.inbox.get(), 5)  # wait for ready
        await rpc.close(grace_s=0.5)
        assert rpc.proc.returncode == -9

    asyncio.run(go())


def test_non_json_line_skipped_later_responses_arrive():
    """A non-JSON line should be skipped; later responses should still arrive."""
    async def go():
        server_code = textwrap.dedent("""
            import json, sys
            for line in sys.stdin:
                msg = json.loads(line)
                if msg.get("method") == "test":
                    print("not valid json", flush=True)
                    print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"ok": True}}), flush=True)
        """)
        rpc = await Rpc.spawn([sys.executable, "-c", server_code], env={"PATH": "/usr/bin"})
        try:
            result = await asyncio.wait_for(rpc.request("test", {}), 5)
            assert result == {"ok": True}
        finally:
            await rpc.close()

    asyncio.run(go())


def test_non_dict_json_line_skipped():
    """A JSON line that is not a dict should be skipped; later responses should still arrive."""
    async def go():
        server_code = textwrap.dedent("""
            import json, sys
            for line in sys.stdin:
                msg = json.loads(line)
                if msg.get("method") == "test":
                    print(json.dumps(123), flush=True)
                    print(json.dumps({"jsonrpc": "2.0", "method": "notification", "params": {}}), flush=True)
                    print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"ok": True}}), flush=True)
        """)
        rpc = await Rpc.spawn([sys.executable, "-c", server_code], env={"PATH": "/usr/bin"})
        try:
            result = await asyncio.wait_for(rpc.request("test", {}), 5)
            assert result == {"ok": True}
            # Verify that the next inbox item is the notification (the non-dict 123 should have been skipped)
            notification = await asyncio.wait_for(rpc.inbox.get(), 5)
            assert isinstance(notification, dict), f"Expected dict notification, got {type(notification).__name__}: {notification}"
            assert notification.get("method") == "notification"
        finally:
            await rpc.close()

    asyncio.run(go())


def test_request_after_server_exit_raises_error():
    """Calling request() after server has exited should raise CliProtocolError, not hang."""
    async def go():
        server_code = textwrap.dedent("""
            import json, sys
            for line in sys.stdin:
                msg = json.loads(line)
                if msg.get("method") == "quit":
                    sys.exit(0)
        """)
        rpc = await Rpc.spawn([sys.executable, "-c", server_code], env={"PATH": "/usr/bin"})
        try:
            # Tell server to exit
            await rpc.send({"jsonrpc": "2.0", "method": "quit"})
            # Wait for server to exit and DONE sentinel
            await asyncio.wait_for(rpc.inbox.get(), 5)

            # Now try to make a request after server is dead
            with pytest.raises(CliProtocolError):
                await asyncio.wait_for(rpc.request("test", {}), 5)
        finally:
            await rpc.close()

    asyncio.run(go())


def test_server_exit_during_outstanding_request_raises_error():
    """Server exiting while request is outstanding should raise CliProtocolError, not hang."""
    async def go():
        server_code = textwrap.dedent("""
            import json, sys, time
            for line in sys.stdin:
                msg = json.loads(line)
                if msg.get("method") == "hang-then-exit":
                    time.sleep(0.5)
                    sys.exit(1)
        """)
        rpc = await Rpc.spawn([sys.executable, "-c", server_code], env={"PATH": "/usr/bin"})
        try:
            # Make request and wait for error as server exits
            with pytest.raises(CliProtocolError):
                await asyncio.wait_for(rpc.request("hang-then-exit", {}), 5)
        finally:
            await rpc.close()

    asyncio.run(go())


def test_oversized_line_fails_pending_requests():
    """A line over the limit should fail pending requests with CliProtocolError mentioning oversized."""
    async def go():
        server_code = textwrap.dedent("""
            import json, sys
            for line in sys.stdin:
                msg = json.loads(line)
                if msg.get("method") == "oversized":
                    # Send a very large JSON object (much larger than typical)
                    large_data = "x" * (20 * 1024 * 1024)  # 20 MiB
                    print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"data": large_data}}), flush=True)
        """)
        rpc = await Rpc.spawn([sys.executable, "-c", server_code], env={"PATH": "/usr/bin"})
        try:
            # This should fail with CliProtocolError due to oversized line
            with pytest.raises(CliProtocolError) as exc_info:
                await asyncio.wait_for(rpc.request("oversized", {}), 5)
            # Verify the error message mentions oversized
            assert "oversized" in str(exc_info.value).lower(), f"Expected 'oversized' in error message, got: {exc_info.value}"
        finally:
            await rpc.close()

    asyncio.run(go())


def test_clean_server_exit_says_exited():
    """A clean server exit should fail pending with 'exited' message, not a specific error."""
    async def go():
        server_code = textwrap.dedent("""
            import json, sys, time
            for line in sys.stdin:
                msg = json.loads(line)
                if msg.get("method") == "wait":
                    time.sleep(0.2)
                    sys.exit(0)  # Clean exit
        """)
        rpc = await Rpc.spawn([sys.executable, "-c", server_code], env={"PATH": "/usr/bin"})
        try:
            # This should fail with generic "exited" message on clean exit
            with pytest.raises(CliProtocolError) as exc_info:
                await asyncio.wait_for(rpc.request("wait", {}), 5)
            # Verify the error message says "exited"
            assert "exited" in str(exc_info.value).lower(), f"Expected 'exited' in error message, got: {exc_info.value}"
        finally:
            await rpc.close()

    asyncio.run(go())
