# Codex isolation: what codex app-server 0.155.1 lets us turn off

Investigation for the Sendesis request `request-agno-cli-models-codex-isolation-leak` (dec-477114059d7c). Status: done. Decisions and the implementation are at the end.

## Method

- **Effective config, no quota:** `config/read` and `skills/list` over app-server JSON-RPC, using the package's own `app_server_argv()` plus candidate `-c` overrides.
- **Key names, no quota:** `codex app-server --strict-config -c key=value </dev/null`. An unknown key, including unknown names under `features.*`, `skills.*` and `memories.*`, fails with `unknown configuration field`. A known key starts and exits on EOF.
  - Order matters: `--strict-config` must come before the `-c` overrides. Placed after them, it accepted `bogus_key_xyz`.
- **What the model sees, small quota:** one low-effort turn on `gpt-5.6-sol` per probe.
  - Developer and context messages come from the rollout file `~/.codex/sessions/.../rollout-*-<thread>.jsonl`.
  - The feature list comes from the `feedback_tags` log line in `~/.codex/logs_2.sqlite`.
  - The tool list is the model's own answer to "list every tool you can call". The rollout does not record the tool list, so this is self-report.

## Baseline: Sendesis M3.1 exit run (current FIXED_CONFIG)

Rollout `2026-10-01T14-15-28-01a0f952-7f48-7222-bdca-87be377d97cc`. Context items sent:

- `<skills_instructions>`: roots `~/.codex/skills` (6 user skills) and `~/.codex/skills/.system` (5 bundled skills).
- `<permissions instructions>`: sandbox and approval policy. This one is wanted.
- `<plugins_instructions>`: present because the user config enables `github@openai-curated`.
- `You are /root, the primary agent in a team of agents ...`: 2264 characters, multi-agent v2.
- `<multi_agent_mode>`: explicit-request-only delegation policy.
- `<environment_context>`: cwd, shell, date, timezone and filesystem roots.
- turn_context: `personality: pragmatic`, `multi_agent_version: v2`.

Features logged for that run:

```
ShellTool, ViewImage, SleepTool, CodexHooks, ContentItemKinds, CodeModeHost, UnifiedExec, UnifiedExecTty,
UnifiedExecZshFork, TerminalResizeReflow, ShellSnapshot, EnableRequestCompression, UnboundedConnectionRetries,
Collab, ToolSearchAlwaysDeferMcpTools, ToolSuggest, Plugins, InAppBrowser, InAppChat, InAppDictation,
InAppLocalAutomation, InAppUpdates, BrowserUse, BrowserUseFullCdpAccess, BrowserUseExternal, ComputerUse,
RemotePlugin, PluginSharing, ImageGeneration, ResizeAllImages, ItemIds, SkillMcpDependencyInstall, SkillSearch,
MentionsV2, GuardianApproval, Goals, ToolCallMcpElicitation, AuthElicitation, Personality, FastMode,
CompactionImageBudget, WorkspaceDependencies, Sqlite, Steer, CollaborationModes, TuiAppServer
```

`Memories` is absent, so memories were already off; the `memories` feature defaults to false. `Apps` is absent too, which confirms that the existing `features.apps=false` takes effect.

## Model catalog

`~/.codex/models_cache.json` holds per-model properties that config does not override:

- `gpt-5.6-sol`: `multi_agent_version: v2`, `tool_mode: code_mode_only`.
- `code_mode_only` means the model calls every tool, Agno dynamic tools included, by writing code against `functions.exec`.

## Probe results

Six low-effort turns on `gpt-5.6-sol`, on 2026-10-01 between 16:12 and 16:25 local time. All runs used the cwd `~/.cache/agno-cli-models/empty`, `builtin_tools=False` and `environments: []`.

| Turn | Overrides on top of today's argv | Developer/context items | Model-reported tools |
| --- | --- | --- | --- |
| 1 `01a0f9bd` | skills, plugins, personality and feature flags (set 1 below) | permissions, `/root` multi-agent prompt, `<multi_agent_mode>`, environment_context | (not asked) |
| 2 `01a0f9be` | set 1, with `features.multi_agent_v2.enabled=false`, `usage_hint_enabled=false` and `include_collaboration_mode_instructions=false` | same as turn 1 | `functions.exec`, `functions.wait`, `functions.request_user_input`, `collaboration.{spawn_agent, followup_task, send_message, wait_agent, interrupt_agent, list_agents}`, `skills__list`, `skills__read` |
| 3 `01a0f9c2` | set 2 (below), plus a dynamic tool `get_secret` | **permissions, environment_context only** | `functions.exec`, `functions.wait`, `collaboration.{spawn_agent, followup_task, send_message, interrupt_agent, list_agents}`, `tools.get_secret`, `tools.skills__list`, `tools.skills__read`. `get_secret` was called (callId `exec-…`) and returned 4242 |
| 4 `01a0f9c3-2c16` | set 2 and `features.code_mode_host=false` | same as turn 3 | **Tool call failed: "code-mode host is disabled"** |
| 5 `01a0f9c3-c240` | set 2 and `model_catalog_json` = a copy of `models_cache.json` with `gpt-5.6-sol.multi_agent_version = "disabled"` | permissions, environment_context only | `functions.exec`, `functions.wait`, `tools.get_secret`, `tools.skills__list`, `tools.skills__read`. **No collaboration tools**; `get_secret` worked |

Turn 6 (`01a0f9c9-4ec8`) used the implemented `app_server_argv()`: set 2, with no catalog override, and `agents.max_concurrent_threads_per_session=1`. The model was asked to call `collaboration.spawn_agent`. **The spawn succeeded** (`{"task_name":"/root/reply_ok"}`). Child thread `01a0f9c9-5b56` ran a turn with its own prompt, "You are an agent in a team of agents ...", which `subagent_usage_hint_text` controls. The cap therefore does not count the root agent, and 0 is rejected, so it does not prevent sub-agents. The parent rollout records a `SubAgentActivity` item.

In turns 3–5 the logged feature list no longer has SleepTool, CodexHooks, ShellSnapshot, Collab, ToolSuggest, Plugins, BrowserUse, ComputerUse, RemotePlugin, ImageGeneration, SkillMcpDependencyInstall, SkillSearch, Goals or Personality.

### Override set 1 (all validated with `--strict-config`)

```
skills.include_instructions=false          # drops <skills_instructions>
skills.bundled.enabled=false               # drops the 5 bundled .system skills
features.plugins=false                     # drops <plugins_instructions> and plugin skills (review-agent), even though config/read still shows the user's plugin enabled
features.remote_plugin=false
features.personality=false                 # and personality: "none" on thread/start
features.multi_agent=false                 # drops Collab from features, but NOT the v2 tools or prompt
features.sleep_tool=false
features.hooks=false                       # CodexHooks
features.shell_snapshot=false
features.skill_search=false
features.skill_mcp_dependency_install=false
features.image_generation=false
features.browser_use=false
features.computer_use=false
features.goals=false
features.tool_suggest=false
include_apps_instructions=false
memories.use_memories=false
memories.generate_memories=false
```

### Override set 2 = set 1, plus

```
features.multi_agent_v2.enabled=false
features.multi_agent_v2.usage_hint_enabled=false
features.multi_agent_v2.root_agent_usage_hint_text=""     # drops the "/root" prompt
features.multi_agent_v2.multi_agent_mode_hint_text=""     # drops <multi_agent_mode>
features.multi_agent_v2.wait_agent_enabled=false          # drops collaboration.wait_agent
agents.max_concurrent_threads_per_session=1               # 0 is rejected; caps spawning if the tools stay
include_collaboration_mode_instructions=false
tools.experimental_request_user_input.enabled=false       # drops functions.request_user_input
features.default_mode_request_user_input=false
skills.config=[{path="<CODEX_HOME>/skills/<name>/SKILL.md", enabled=false}, ...]   # one entry per user skill
```

`skills.config` per path is the only way found to disable user skills. `features.skip_host_skill_discovery=true` had no effect on `skills/list`. Like the MCP servers, the skills have to be listed from `$CODEX_HOME/skills/*/SKILL.md` and disabled by path. With all of them disabled, `skills/list` reports each as `enabled: false`.

## What can and cannot be disabled (codex-cli 0.155.1)

| Item from the request | Can disable? | How |
| --- | --- | --- |
| `<skills_instructions>` | yes | `skills.include_instructions=false` |
| Bundled `.system` skills | yes | `skills.bundled.enabled=false` |
| User skills (`~/.codex/skills`) | yes, by enumeration | `skills.config=[{path, enabled=false}]` per skill |
| `<plugins_instructions>` and plugin skills | yes | `features.plugins=false`, `features.remote_plugin=false` |
| `/root` multi-agent prompt, `<multi_agent_mode>` | yes | blank `features.multi_agent_v2.root_agent_usage_hint_text` and `multi_agent_mode_hint_text` |
| Collaboration tools (`spawn_agent` and others) | **not through config** | `multi_agent_version: v2` is a property of the model in the catalog. Only a `model_catalog_json` override removed them (turn 5). `max_concurrent_threads_per_session=1` does not stop a spawn (turn 6) |
| SleepTool, CodexHooks, ShellSnapshot | yes | `features.sleep_tool`, `features.hooks`, `features.shell_snapshot` set to false |
| Memories | already off; now pinned | `memories.use_memories=false`, `memories.generate_memories=false` |
| Personality | yes | `features.personality=false`, plus `personality: "none"` on `thread/start` |
| `request_user_input` | yes | `tools.experimental_request_user_input.enabled=false` |
| `skills__list`, `skills__read` tools | **no lever found** | Still offered with every skill disabled. Presumably they list and read an empty catalog, but no turn checked this |
| `functions.exec`, `functions.wait` | **must stay** | `tool_mode: code_mode_only`: Agno's dynamic tools are called through exec (turn 4 broke tool calls) |
| `<permissions instructions>`, `<environment_context>` | keep | Describe this call's sandbox, date and timezone, not user setup |

## Decisions (maintainer, 2026-10-01)

1. **Collaboration tools: document only.**
   - Rejected: a patched `model_catalog_json`, always on or opt-in. It depends on the cache's format and pins model metadata, in a package whose premise is "the real CLI as installed".
   - Rejected: a client-side guard that fails the call when a spawn is seen.
   - Not shipped: the ineffective `wait_agent_enabled=false` and `max_concurrent_threads_per_session=1`, so that nothing suggests a guard that does not exist.
   - `multi_agent_mode_hint_text` is **not** blanked. With the tools still offered, Codex's `<multi_agent_mode>` "do not spawn unless asked" line is the only thing discouraging a spawn. Only the `/root` delegation prompt is blanked.
2. **`skills__list` / `skills__read`: documented as remaining.**
3. **No extra feature flags for ShellTool, UnifiedExec or ViewImage when `builtin_tools=False`.** `environments: []` already hides them.

## Implemented

- `FIXED_CONFIG` is set 2 without `wait_agent_enabled`, `max_concurrent_threads_per_session` or `multi_agent_mode_hint_text`. The per-path `skills.config` is built at spawn time from `$CODEX_HOME/skills/**/SKILL.md`, skipping dot-directories.
- `personality: "none"` goes on `thread/start` and `thread/resume`.
- `fingerprint()` covers FIXED_CONFIG, plus `user_skills: disabled` and `personality: none`.
- A unit test runs the real argv under `codex app-server --strict-config </dev/null`. It is skipped without the binary or on an untested version, and catches a renamed key without a model call; a bogus key exits 1.
- An integration test reads the call's rollout and checks for no skills, plugins, apps or `/root` blocks, `<multi_agent_mode>` present, and personality `none`.
- `~/.codex/AGENTS.md` does not exist on this machine, and `project_doc_max_bytes` is left alone. It stays under Known limits.
