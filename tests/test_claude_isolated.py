"""tools/claude_isolated.py: the Agno-free copy of ClaudeCodeModel's isolation.

The script carries its own copies of the isolation settings, env policy, tested versions and
fingerprint so it can run without this package. These tests fail when the copies drift.
"""

import asyncio
import importlib.util
import subprocess
import sys
from pathlib import Path

import claude_agent_sdk as sdk
import pytest

from agno_cli_models import _env
from agno_cli_models.claude import options as pkg
from agno_cli_models.versions import SUPPORTED

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "claude_isolated.py"


def load():
    spec = importlib.util.spec_from_file_location("claude_isolated", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses look their module up here
    spec.loader.exec_module(mod)
    return mod


ci = load()


def test_runs_without_agno_or_this_package():
    code = ("import sys, importlib.util; sys.modules['agno'] = None; sys.modules['agno_cli_models'] = None; "
            f"spec = importlib.util.spec_from_file_location('ci', {str(SCRIPT)!r}); "
            "m = importlib.util.module_from_spec(spec); sys.modules['ci'] = m; spec.loader.exec_module(m); print('ok')")
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0 and done.stdout.strip() == "ok", done.stderr


def test_isolation_copies_match_the_package():
    assert ci.ISOLATION_SETTINGS == pkg.ISOLATION_SETTINGS
    assert ci.REFUSED_PERMISSION_MODES == pkg.REFUSED_PERMISSION_MODES
    assert (ci.REMOVED_KEYS, ci.REMOVED_PREFIXES, ci.KEPT_KEYS) == (_env.REMOVED_KEYS, _env.REMOVED_PREFIXES, _env.KEPT_KEYS)
    assert ci.TESTED_VERSIONS == SUPPORTED["claude"]
    env = {"ANTHROPIC_API_KEY": "k", "CLAUDE_CODE_OAUTH_TOKEN": "t", "CLAUDE_CONFIG_DIR": "/c", "PATH": "/bin"}
    assert ci.blanking_overrides(env) == _env.blanking_overrides(env)


@pytest.mark.parametrize("tools, mode, idle", [((), None, 60.0), (("Read", "Grep"), None, None), (("Edit",), "acceptEdits", 30.0)])
def test_fingerprint_matches_the_package(tools, mode, idle):
    assert ci.fingerprint(tools, mode, idle) == pkg.fingerprint(tools, mode, idle)


def test_options_match_the_package():
    common = dict(cli_path="claude", cwd="/w", system_prompt="S", permission_mode=None, resume="s1", max_turns=20)
    mine = ci.build_options(model="m", effort="high", tools=("Read",), schema={"type": "object"}, partial=True, **common)
    theirs = pkg.build_options(model_id="m", effort="high", builtin_tools=("Read",), agno_tool_names=[], mcp_server=None,
                               output_schema={"type": "object"}, stream=False, hooks=None, idle_timeout_s=60.0, **common)
    for name in ("model", "effort", "cli_path", "cwd", "system_prompt", "tools", "allowed_tools", "setting_sources",
                 "strict_mcp_config", "settings", "extra_args", "env", "include_partial_messages", "max_turns",
                 "output_format", "resume"):
        assert getattr(mine, name) == getattr(theirs, name), name


def test_bypass_permissions_is_refused():
    with pytest.raises(ValueError):
        ci.build_options(model="m", effort="high", cli_path="claude", cwd="/w", system_prompt="", tools=(),
                         permission_mode="bypassPermissions", schema=None, resume=None, partial=False, max_turns=1)


# ---- ask() with a fake SDK ----

INIT = sdk.SystemMessage(subtype="init", data={"session_id": "sess-1"})


def result(**over):
    base = dict(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1, session_id="sess-1",
                total_cost_usd=0.01, result="hello", model_usage={"claude-haiku-4-5": {"inputTokens": 5, "outputTokens": 3}})
    base.update(over)
    return sdk.ResultMessage(**base)


@pytest.fixture
def fake_sdk(monkeypatch):
    calls = []

    def install(*items, gap=0.0):
        def query(*, prompt, options):
            calls.append((prompt, options))

            async def gen():
                for item in items:
                    await asyncio.sleep(gap)
                    yield item

            return gen()

        monkeypatch.setattr(ci.sdk, "query", query)
        return calls

    monkeypatch.setattr(ci, "installed_version", lambda binary: SUPPORTED["claude"][-1])
    return install


def test_ask_returns_the_answer_and_run_info(fake_sdk, tmp_path):
    calls = fake_sdk(INIT, result())
    r = asyncio.run(ci.ask("q", cwd=str(tmp_path), tools=("Read",), cli_path="claude"))
    assert r.text == "hello" and r.session_id == "sess-1" and r.observed_model == "claude-haiku-4-5"
    assert r.usage["total_tokens"] == 8 and r.fingerprint == pkg.fingerprint(("Read",), None, 60.0)
    assert calls[0][1].include_partial_messages is True


def test_ask_raises_stall_on_silence(fake_sdk, tmp_path):
    fake_sdk(INIT, result(), gap=1.0)
    with pytest.raises(ci.ClaudeStallError) as info:
        asyncio.run(ci.ask("q", cwd=str(tmp_path), idle_timeout_s=0.1, cli_path="claude"))
    assert info.value.idle_s == 0.1 and info.value.status_code == 504


def test_ask_wall_clock_on_a_chatty_stream(fake_sdk, tmp_path):
    fake_sdk(*([INIT] + [sdk.SystemMessage(subtype="thinking_tokens", data={})] * 100), gap=0.02)
    with pytest.raises(ci.ClaudeTimeoutError) as info:
        asyncio.run(ci.ask("q", cwd=str(tmp_path), idle_timeout_s=5, timeout_s=0.3, cli_path="claude"))
    assert type(info.value) is ci.ClaudeTimeoutError


def test_ask_types_a_rate_limited_result(fake_sdk, tmp_path):
    fake_sdk(INIT, result(is_error=True, api_error_status=429, result="rate limited"))
    with pytest.raises(ci.ClaudeRateLimitError):
        asyncio.run(ci.ask("q", cwd=str(tmp_path), cli_path="claude"))
