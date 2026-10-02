# Substrate checks: Claude message cadence and Codex stream reconnects

Measurements for the Sendesis request `request-agno-cli-models-substrate-checks` (dec-5799c9b63ac7). They feed the idle limit in `request-agno-cli-models-idle-limit` (dec-acb04313987e).

Versions: Claude Code 2.1.287, claude-agent-sdk as pinned in this repo, codex-cli 0.155.1.

## 1. Codex: overriding the built-in provider's stream settings

**Answer: not possible.** `codex app-server` refuses to start when any key under `model_providers.openai` is set:

```
$ codex app-server -c model_providers.openai.stream_idle_timeout_ms=1000 </dev/null
Error: error loading default config after config error: model_providers contains reserved built-in provider IDs: `openai`. Built-in providers cannot be overridden. Rename your custom provider (for example, `openai-custom`).
in `model_providers`
(exit 1)
```

The same error occurs for `stream_max_retries`, `request_max_retries`, and with or without `--strict-config`. The built-in provider's id is `openai`; rollouts record `model_provider: "openai"`.

**Built-in defaults** (from the codex 0.155.1 source, `codex-rs/model-provider-info/src/lib.rs`):

- `DEFAULT_STREAM_IDLE_TIMEOUT_MS = 300_000`
- `DEFAULT_STREAM_MAX_RETRIES = 5`
- `DEFAULT_REQUEST_MAX_RETRIES = 4`

A stalled stream therefore waits 300 s before Codex itself reconnects. That equals the Sendesis Codex profile's wall clock (`timeout_s: 300`), so on a stall the call times out before or just as Codex would retry.

**A reconnect is visible in app-server notifications.** The probe:

- A custom copy of the provider:
  - `model_providers.openai-custom = { base_url = "https://chatgpt.com/backend-api/codex", wire_api = "responses", requires_openai_auth = true, stream_idle_timeout_ms = 300, stream_max_retries = 1, request_max_retries = 0 }`
  - `model_provider = "openai-custom"`
- It authenticated with the CLI's ChatGPT login.
- Thread `01a0f9db-d07c`, one low-effort turn.

Notifications, with time from `turn/start`:

```
[0.02s] thread/status/changed  {"status": {"type": "active"}}
[1.46s] error  {"error": {"message": "Reconnecting... 1/1", "codexErrorInfo": {"responseStreamDisconnected": {"httpStatusCode": null}},
                 "additionalDetails": "stream disconnected before completion: idle timeout waiting for SSE"}, "willRetry": true}
[2.64s] thread/status/changed  {"status": {"type": "systemError"}}
[2.64s] error  {"error": {"message": "stream disconnected before completion: idle timeout waiting for SSE", "codexErrorInfo": "other"}, "willRetry": false}
[2.64s] turn/completed  {"turn": {"status": "failed", ...}}
```

- **A reconnect** is an `error` notification with `willRetry: true` and `codexErrorInfo.responseStreamDisconnected`.
- **Exhausted retries** produce `willRetry: false`, then thread status `systemError` and a `failed` turn.
- `TurnTracker` already ignores `willRetry: true` errors, so a reconnect does not fail a call. An idle clock that resets on every app-server message would also reset on the reconnect notice.

**The custom-provider route is noted, not recommended.** It makes Codex's own reconnect configurable, for example 60 s. But it copies the built-in provider's definition into the package (base URL, wire API, auth flag), and a Codex release can change those. It is the same premise problem as patching the model catalog (see dec-c998a8dbd441). The package-side idle limit of request 4 does not depend on it.

## 2. Claude: message cadence at effort high

### Margin rule, fixed before the runs

- The idle clock starts at the first SDK message (the `init` system message). Spawn-to-init is reported separately.
- Proposed `idle_timeout_s` default = 2 × the longest inter-message gap seen in any healthy **stream-mode** run, rounded up to a multiple of 30 s, and never below 60 s.
- Stream mode counts because the idle limit can enable `include_partial_messages` whenever an idle limit is set, without yielding deltas to a non-streaming caller.
- Non-stream gaps are reported to show what an idle clock would face without partial messages.
- "Healthy" means the run returned a result without error or rate limit. Other runs are kept in the raw data and excluded from the maximum.

### Setup

- **Harness:** `tools/measure_cadence.py`, kept in the repo so the measurement can be rerun on a new CLI version or model.
  - It wraps `ClaudeCodeModel.query_fn` and stamps every message `claude_agent_sdk.query` yields with `time.monotonic()`. That is the point an idle clock in the package would see.
  - It drives the real model through an Agno `Agent`.
- **Settings, matching Sendesis `profiles/claude-opus.yaml`:** `claude-opus-5-5`, effort `high`, `max_turns=20`. Wall clock 900 s, so slow runs are not cut off.
- **Prompt shapes:**
  - `review`: a Sendesis-style security review of a diff. Tools Read, Grep and Glob; nested `output_schema`; the model must read `app.py`.
  - `puzzle`: Hertzsprung's problem for n=8. Long thinking, short answer.
  - `essay`: about 1200 words of plain prose. Long generation, no tools.
  - `longthink`: a hand-computed recurrence for n=10. Added after the first 12 runs produced little thinking.
- **Modes:** `nonstream`, where `include_partial_messages` is off and the SDK yields whole messages, and `stream`, where it is on.
- **Runs:** one after another. 2 repetitions of each mode for `review`, `puzzle` and `essay` (12 runs), then 1 of each mode for `longthink` (2 runs), 14 in all.
- **Raw data:** `reports/2026-10-01-substrate-checks-claude.jsonl`. Each line has every message's arrival time and kind, the top 5 gaps and the message pair bounding each.

**Already visible in a Haiku smoke run:** the CLI sends `system:thinking_tokens` progress messages *during thinking*, even in non-stream mode. In stream mode, thinking also arrives as `thinking_delta` events. Thinking is therefore not silent at the SDK layer.

### Results (14 runs, all healthy, Claude Code 2.1.287)

The 12 planned runs, plus 2 `longthink` runs, one per mode. Those were added because the first 12 produced little thinking. The `longthink` prompt is a hand-computed recurrence for n=10.

| Shape | Mode | Duration (s) | Longest thinking stretch (s) | Max gap after init (s) | Messages bounding the max gap |
| --- | --- | --- | --- | --- | --- |
| review | stream | 13.6 / 10.7 | 0 / 0 | **5.55** / 1.33 | `system:status` → `stream:message_start` (new turn after a tool result) |
| puzzle | stream | 19.5 / 19.8 | 10.9 / 10.7 | 1.38 / 1.40 | `thinking_delta` → `system:thinking_tokens` |
| essay | stream | 44.1 / 45.5 | 7.8 / 10.0 | 1.48 / 1.45 | `thinking_delta` → `system:thinking_tokens` |
| longthink | stream | 30.7 | 17.0 | 1.50 | `thinking_delta` → `system:thinking_tokens` |
| review | nonstream | 9.9 / 9.2 | 0 / 0 | 6.52 / 5.70 | `UserMessage` → `assistant:ToolUseBlock` (one whole model turn) |
| puzzle | nonstream | 29.3 / 15.2 | 18.3 / 3.0 | 8.13 / 9.51 | `ThinkingBlock` → `TextBlock` (the whole answer text) |
| essay | nonstream | 40.3 / 48.3 | 4.5 / 8.0 | **32.69 / 37.50** | `ThinkingBlock` → `TextBlock` (the whole 1200-word answer) |
| longthink | nonstream | 30.2 | 16.0 | 11.26 | `ThinkingBlock` → `TextBlock` |

- **Startup** (spawn to `init`): 0.56–1.74 s.
- **Gaps inside text deltas** (stream): at most 0.14 s.
- **Gaps inside thinking** (both modes): at most 1.65 s.

### Findings

1. **Thinking is never silent at the SDK layer.** During thinking the CLI sends `system:thinking_tokens` about every 1.2–1.7 s, in non-stream mode too. In stream mode it also sends `thinking_delta` events. Over the longest observed stretch, 18 s, no gap inside thinking exceeded 1.65 s.
2. **In stream mode the longest gap is the start of a new model turn**: 5.55 s from a tool result to the next `message_start`, which is API time to first token. Every other gap was at most 2 s.
3. **In non-stream mode the gap grows with the answer length.** The whole text block arrives as one `AssistantMessage` after generation ends: 37.5 s for about 1200 words. An idle limit at any fixed value would trip on a long enough non-stream answer. **A Claude idle limit therefore needs `include_partial_messages` on whenever `idle_timeout_s` is set**, whatever the caller's stream mode. The deltas are counted by the idle clock but not yielded to a non-streaming caller.
4. **The pre-registered rule gives 60 s:** 2 × 5.55 = 11.1 s, rounded up to 30 s, then the 60 s floor applies. That is about 10× the largest observed stream gap.

### Limits of this measurement

- **Thinking for minutes was not produced** at effort `high` on claude-opus-5-5. The longest stretch was 18 s, so constant cadence during much longer thinking is assumed from the 1.2–1.7 s `thinking_tokens` rhythm, not observed.
- **Not observed in these runs:**
  - Claude Code's own API retries and backoff, on overload or 5xx.
  - Slow tools.
  - Rate-limit waits. The `RateLimitEvent` messages in the raw data are the CLI's routine usage-status updates, not limits being hit; every run completed.
- **What the Agent SDK docs say about those cases** (code.claude.com/docs/en/agent-sdk), not verified on this CLI and SDK version:
  - The message union includes an `SDKAPIRetryMessage` (`api_retry`), so a backoff is announced, not silent. A single backoff longer than the idle limit would still go quiet between the announcement and the retry.
  - While a tool call runs in the main conversation, Claude Code sends a `tool_progress` heartbeat every 30 s. With heartbeats, a slow tool would not trip a 60 s limit. Without them, the idle clock in request 4 should pause while an Agno tool runs in the caller's process.
  - Request 4 should verify both with a fake or a forced case.
- **The result is specific to** Claude Code 2.1.287, claude-opus-5-5 and effort `high`. Re-measure with `tools/measure_cadence.py` when any of them changes.
- **Sample size:** 14 runs, all on 2026-10-01, sequential, on one machine.
