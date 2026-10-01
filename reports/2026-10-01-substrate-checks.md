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

(filled in after the runs)
