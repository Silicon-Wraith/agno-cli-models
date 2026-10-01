# agno-cli-models

Agno models backed by the official Claude Code and Codex clients. Each model drives the real CLI (`claude` through the Claude Agent SDK, `codex app-server` over JSON-RPC) and uses that CLI's own login, so calls run on your existing subscription. No API keys, no token extraction.

Agno's tools stay in your process: the CLI sees them as tools, calls them, and Agno runs them.

## Install

```bash
pip install agno-cli-models
```

Requires Python 3.11+ and Claude Code and/or codex-cli installed and logged in:

```bash
claude        # then run /login
codex login
```

Agno's SQLite database needs `sqlalchemy[asyncio]` (`pip install "sqlalchemy[asyncio]"`) if you use `SqliteDb` for sessions.

## Quick start

```python
from agno.agent import Agent
from agno_cli_models import ClaudeCodeModel, CodexModel

agent = Agent(model=ClaudeCodeModel(id="claude-haiku-4-5-20251001", effort="low"))
print(agent.run("Say OK.").content)

agent = Agent(model=CodexModel(id="gpt-5.6-sol", effort="low"))
print(agent.run("Say OK.").content)
```

Custom tools and structured output work as in any Agno agent:

```python
from agno.tools import tool
from pydantic import BaseModel

def get_secret(key: str) -> str:
    """Look up a secret by key.

    Args:
        key: secret name
    """
    return {"alpha": "4242"}.get(key, "not found")

class City(BaseModel):
    city: str
    country: str

Agent(model=ClaudeCodeModel(), tools=[get_secret]).run("What is the secret for key 'alpha'?")
Agent(model=CodexModel(), output_schema=City).run("Capital of Norway?")
```

## Parameters

Both models subclass `CliModel`, exported from the package root for type checks and `isinstance`. Shared by both models:

| Field | Default | Meaning |
| --- | --- | --- |
| `id` | see below | Model id, pinned on every call |
| `effort` | `"high"` | Reasoning effort, pinned on every call |
| `cwd` | `None` | Working directory for the CLI. Default is an empty directory, `~/.cache/agno-cli-models/empty` |
| `timeout_s` | `600.0` | Hard wall-clock limit per call. Exceeding it raises `CliTimeoutError` |

`ClaudeCodeModel` (default `id="claude-opus-5-5"`):

| Field | Default | Meaning |
| --- | --- | --- |
| `builtin_tools` | `()` | Claude Code built-in tools to allow, for example `("Read", "Grep")`. Listed tools are pre-approved and run without a permission check, so `("Bash",)` means unconditional shell access. Empty means only your Agno tools |
| `permission_mode` | `None` | Passed to Claude Code. `"bypassPermissions"` is refused with `ValueError` |
| `max_turns` | `50` | Turn limit inside one call |
| `cli_path` | `None` | Path to the `claude` binary. Default is the one on `PATH` |

`CodexModel` (default `id="gpt-5.6-sol"`):

| Field | Default | Meaning |
| --- | --- | --- |
| `sandbox` | `"read-only"` | `"read-only"` or `"workspace-write"`. Anything else, including `danger-full-access`, raises `ValueError` |
| `builtin_tools` | `False` | Allow Codex's own shell and file tools. Off means only your Agno tools |
| `codex_bin` | `"codex"` | Codex binary to run |

## Isolation

The goal is that a call sees your Agno tools and nothing from your personal CLI setup.

Claude Code, always on:
- `setting_sources=[]`, `strict_mcp_config`, slash commands disabled.
- claude.ai connectors disabled and auto memory off.
- Only the tools you list in `builtin_tools` plus your Agno tools (served as an in-process MCP server named `agno`).

Codex, always on:
- `approval_policy` is `never`, web search disabled, apps disabled, model fallback disabled.
- `codex app-server` has no `--ignore-user-config`, so the package disables each MCP server defined in `$CODEX_HOME/config.toml` (or `~/.codex/config.toml`) with `-c mcp_servers.<name>.enabled=false`. If that file exists but cannot be read or parsed, the call fails with `CliProtocolError` naming the path instead of running without isolation. A missing file is fine.
- Skills are off. The skills prompt and Codex's bundled skills are disabled, and every `SKILL.md` under `$CODEX_HOME/skills` is disabled by path.
- Plugins, hooks, memories, image generation, browser and computer use, goals, tool suggestions, the sleep tool, shell snapshots and `request_user_input` are disabled. Personality is `none`.
- The `/root` multi-agent prompt is blanked. The model can still start sub-agents; see Known limits.
- What the model still sees besides your prompt:
  - the sandbox and approval policy
  - the date and timezone
  - Codex's `<multi_agent_mode>` line, which tells it not to start sub-agents unless asked
- See Known limits for what this does not cover. Every `-c` key is checked against the installed Codex with `--strict-config` in the unit tests.

Both:
- Child processes do not see `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `CODEX_API_KEY`, `CLAUDECODE`, or any `CLAUDE_*` variable (including `CLAUDE_CODE_OAUTH_TOKEN`) except `CLAUDE_CONFIG_DIR` and `CLAUDE_CODE_ENTRYPOINT`. For Claude these are blanked (set to empty), because the SDK merges the parent environment and cannot delete keys; for Codex they are removed. A CLI can never fall back to API billing or another endpoint by accident.
- The `--version` probe runs with stdin closed. Each call has a hard wall-clock limit (`timeout_s`).
- The package never uses `--bare`, `bypassPermissions` or `danger-full-access`, and never reads or passes OAuth tokens.

## Sessions

The CLI session id (Claude session or Codex thread) is stored in `provider_data` on assistant messages, so Agno's own history persists it. A new agent instance with the same `session_id` resumes the CLI session. Resume needs `add_history_to_context=True`.

If the stored CLI session no longer exists (its files were cleaned up, or the Agno session moved to another machine), the model logs a warning, starts a new CLI session with the Agno history replayed as a transcript, and stores the new session id. Codex retries only when `thread/resume` gets an error reply. Claude's SDK reports a missing session only as a failed process, so Claude retries when resuming fails before the session starts, unless the failure is a rate limit or context overflow:

```python
from agno.db.sqlite import SqliteDb

db = SqliteDb(db_file="s.db")
first = Agent(model=CodexModel(), db=db, session_id="s1", add_history_to_context=True)
first.run("Remember the code word: PELICAN. Reply OK.")

second = Agent(model=CodexModel(), db=db, session_id="s1", add_history_to_context=True)
print(second.run("What was the code word? One word.").content)
```

## Approvals

Tools marked `@tool(requires_confirmation=True)` pause the Agno run.

```python
@tool(requires_confirmation=True)
def wipe(path: str) -> str:
    """Delete a file.

    Args:
        path: file to delete
    """
    return f"deleted {path}"

agent = Agent(model=ClaudeCodeModel(), tools=[wipe])
run = agent.run("Delete /tmp/demo.txt using the wipe tool.")
assert run.is_paused
for req in run.active_requirements:
    req.confirm()          # or req.reject()
done = agent.continue_run(run_response=run, requirements=run.requirements)
```

- Claude uses the SDK's deferral: a `PreToolUse` hook defers the call, the run ends paused, and the session resumes after you decide.
- Codex declines the pending call, interrupts the turn, and resumes the thread on `continue_run`.
- Server requests Codex sends are answered by method: command and file-change approvals are declined, MCP elicitation is declined, and anything else gets a JSON-RPC error.
- A rejected tool never runs. The model may retry and pause again, so check `is_paused` after `continue_run`. Confirmed tools run exactly once.

The `continue_run` call above is the Agno 3.0.11 form.

## Streaming

`await agent.arun(..., stream=True)` streams. Sync `agent.run(..., stream=True)` does not: the model's sync `response_stream` runs the whole call and then yields every event at once.

- Claude streams every text delta, including commentary written before a tool call. Codex streams only the deltas of its final answer.
- In stream mode the stored assistant message is the concatenation of the streamed deltas, so for Claude it can differ from the non-stream answer, which is the CLI's final result text.
- Structured output is parsed only on the non-stream path.

## Run info

After a run, `model.last_run_info` holds details of the last call, and the same dict is at `run_output.model_provider_data["agno_cli_models"]`:

| Key | Meaning |
| --- | --- |
| `observed_model` | The model the CLI actually used, not the one requested |
| `cli_version` | Installed CLI version |
| `cli_session_id` | Claude session id or Codex thread id |
| `config_fingerprint` | SHA-256 of the isolation and permission settings in force |
| `cli` | `"claude"` or `"codex"` |
| `parse_error` | Optional. First line of the validation error when structured output did not match `output_schema` (non-stream path) |

```python
agent = Agent(model=ClaudeCodeModel(), tools=[get_secret])
out = agent.run("What is the secret for key 'alpha'?")
print(agent.model.last_run_info["observed_model"])
print(out.model_provider_data["agno_cli_models"]["cli_session_id"])
```

## Token semantics

Identical for both models:

| Metric | Meaning |
| --- | --- |
| `input_tokens` | Total input, including cached tokens |
| `cache_read_tokens` | The cached part of the input |
| `cache_write_tokens` | Cache creation (Claude only) |
| `reasoning_tokens` | Reasoning or thinking output |
| `total_tokens` | `input_tokens + output_tokens` |

Codex usage is summed over every model call in a turn. CLI overhead is large (tens of thousands of input tokens per call, mostly cached), so compare cost on total tokens.

## Errors

```python
from agno_cli_models import (
    ModelRateLimitError, ModelProviderError, CliTimeoutError, CliProtocolError,
)
from agno_cli_models.errors import ContextWindowExceededError
```

- `ModelRateLimitError`: either CLI reported a rate or usage limit (Agno's own class).
- `ContextWindowExceededError`: either CLI reported a context overflow (Agno's own class).
- `ModelProviderError`: other CLI errors, and the base class of the two below.
- `CliTimeoutError`: no complete answer within `timeout_s` (status 504).
- `CliProtocolError`: the CLI exited or spoke outside the protocol, for example Codex exiting mid-turn (status 502).

Because rate limits use Agno's classes, Agno's `FallbackConfig(on_rate_limit=[...])` works with these models without extra code.

## Supported CLI versions

Tested against Claude Code `2.1.286` and `2.1.287`, and codex-cli `0.155.1`:

```python
>>> import agno_cli_models as m
>>> m.SUPPORTED
{'claude': ('2.1.286', '2.1.287'), 'codex': ('0.155.1',)}
```

Any other version, or a version that cannot be read, emits `UnsupportedCliVersionWarning` (a `UserWarning`) once per model instance. `--version` runs once per instance. It never blocks the call.

## Known limits

- `codex app-server` is experimental and its protocol may change between Codex releases.
- Codex user config is not ignored, because `codex app-server` has no `--ignore-user-config`. The package switches off what it can, listed under Isolation. The rest of that config still applies, for example `notify`, `model_provider` and `model_providers`, `shell_environment_policy`, and `~/.codex/AGENTS.md`. Project-level `.codex` config and project skills are not covered either.
- Codex still offers the model some tools that no config setting removes in codex-cli 0.155.1:
  - **Collaboration tools** (`spawn_agent`, `wait_agent`, `followup_task`, `send_message`, `interrupt_agent`, `list_agents`). The model catalog enables multi-agent v2 for `gpt-5.6-sol`, and no config key stops a spawn. A spawned sub-agent runs in the same sandbox and gets Codex's own sub-agent prompt. Only the kept `<multi_agent_mode>` instruction stands between the model and a spawn, so a call can include work by more than one agent. Whether a sub-agent can call your Agno tools has not been tested.
  - **`skills__list` and `skills__read`.** Every skill they could reach is disabled.
  - **`functions.exec` and `functions.wait`.** These are not a leak: in this model's code mode, every tool call goes through `exec`, including your Agno tools.
- The tool lists above are what the model reports; Codex does not record the tools it offers. The details are in `reports/2026-10-01-codex-isolation-findings.md`.
- Every Codex call leaves a session file under `$CODEX_HOME/sessions` containing the prompt.
- Claude's SDK bundles its own CLI, but this package uses the `claude` on `PATH` unless `cli_path` is set, so the version you tested is the version you run.
- Codex needs the `/usr/bin/bwrap` AppArmor profile on Ubuntu 24.04 for its sandbox.

## Tests

Unit tests mock the runners:

```bash
.venv/bin/pytest -q
```

Integration tests call the real CLIs and spend subscription quota, so they are deselected by default:

```bash
.venv/bin/pytest -m integration -v
```

## License

MIT
