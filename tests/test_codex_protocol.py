import pytest
from agno.models.message import Message

from agno_cli_models.codex.protocol import (
    TurnTracker,
    app_server_argv,
    continuation_text,
    fingerprint,
    thread_resume_params,
    thread_start_params,
    turn_start_params,
)
from agno_cli_models.errors import ContextWindowExceededError, ModelProviderError, ModelRateLimitError


def test_app_server_argv_isolates():
    argv = app_server_argv("codex")
    assert argv[:2] == ["codex", "app-server"]
    assert "features.apps=false" in argv and 'web_search="disabled"' in argv


def test_thread_start_params_pin_everything():
    p = thread_start_params(model_id="gpt-5.6-sol", system="S", cwd="/w", sandbox="read-only", builtin_tools=False, dynamic_tools=[{"name": "t"}])
    assert p["model"] == "gpt-5.6-sol" and p["baseInstructions"] == "S" and p["cwd"] == "/w"
    assert p["sandbox"] == "read-only" and p["approvalPolicy"] == "never"
    assert p["allowProviderModelFallback"] is False
    assert p["environments"] == [] and p["dynamicTools"] == [{"name": "t"}]


def test_builtin_tools_keep_the_environment():
    p = thread_start_params(model_id="m", system="", cwd="/w", sandbox="workspace-write", builtin_tools=True, dynamic_tools=[])
    assert "environments" not in p and p["sandbox"] == "workspace-write"


def test_danger_sandbox_refused():
    with pytest.raises(ValueError):
        thread_start_params(model_id="m", system="", cwd="/w", sandbox="danger-full-access", builtin_tools=True, dynamic_tools=[])


def test_resume_and_turn_params():
    r = thread_resume_params(thread_id="t1", model_id="m", system="S", cwd="/w", sandbox="read-only")
    assert r["threadId"] == "t1" and r["model"] == "m"
    t = turn_start_params(thread_id="t1", text="hi", effort="high", builtin_tools=False, output_schema={"type": "object", "properties": {"a": {"type": "string"}}})
    assert t["input"] == [{"type": "text", "text": "hi"}] and t["effort"] == "high" and t["environments"] == []
    assert t["outputSchema"]["required"] == ["a"]


def test_fingerprint():
    assert fingerprint("read-only", False) != fingerprint("workspace-write", False)
    assert fingerprint("read-only", False) != fingerprint("read-only", True)


def test_continuation_text_reports_decisions():
    text = continuation_text([Message(role="tool", tool_call_id="c1", tool_name="wipe", content="wiped"),
                              Message(role="tool", tool_call_id="c2", tool_name="drop", content="denied by user", tool_call_error=True)])
    assert "c1" in text and "wiped" in text and "c2" in text and "denied" in text


def tracker():
    return TurnTracker(model_name="Codex", model_id="gpt-5.6-sol")


def test_tracker_collects_final_answer_usage_and_model():
    t = tracker()
    t.on_notification("thread/started", {"thread": {"id": "t1"}, "model": "gpt-5.6-sol"})
    assert t.on_notification("item/started", {"item": {"id": "i1", "type": "agentMessage", "phase": "final_answer"}}) == []
    deltas = t.on_notification("item/agentMessage/delta", {"itemId": "i1", "delta": "hel"})
    assert deltas[0].content == "hel"
    t.on_notification("item/completed", {"item": {"id": "i1", "type": "agentMessage", "phase": "final_answer", "text": "hello"}})
    t.on_notification("thread/tokenUsage/updated", {"tokenUsage": {"last": {"inputTokens": 100, "cachedInputTokens": 40, "outputTokens": 7, "reasoningOutputTokens": 3}}})
    t.on_notification("turn/completed", {"turn": {"status": "completed"}})
    assert t.done and t.final_text == "hello"
    m = t.metrics()
    assert (m.input_tokens, m.cache_read_tokens, m.output_tokens, m.reasoning_tokens, m.total_tokens) == (100, 40, 7, 3, 107)


def test_reroute_changes_observed_model():
    t = tracker()
    t.model = "gpt-5.6-sol"
    t.on_notification("model/rerouted", {"fromModel": "gpt-5.6-sol", "toModel": "gpt-5.6-mini", "reason": "x", "threadId": "t", "turnId": "u"})
    assert t.model == "gpt-5.6-mini"


@pytest.mark.parametrize("info, error", [
    ("usageLimitExceeded", ModelRateLimitError),
    ("rateLimitExceeded", ModelRateLimitError),
    ("contextWindowExceeded", ContextWindowExceededError),
    ("internalServerError", ModelProviderError),
])
def test_failed_turn_raises_typed_errors(info, error):
    with pytest.raises(error):
        tracker().on_notification("turn/completed", {"turn": {"status": "failed", "error": {"message": "x", "codexErrorInfo": info}}})


def test_interrupted_turn_is_not_an_error():
    t = tracker()
    t.on_notification("turn/completed", {"turn": {"status": "interrupted"}})
    assert t.done and t.interrupted


def test_error_notification_only_raises_when_final():
    t = tracker()
    assert t.on_notification("error", {"error": {"message": "retrying"}, "willRetry": True, "threadId": "t", "turnId": "u"}) == []
    with pytest.raises(ModelProviderError):
        t.on_notification("error", {"error": {"message": "dead"}, "willRetry": False, "threadId": "t", "turnId": "u"})


def test_rate_limit_snapshot_reached_raises():
    with pytest.raises(ModelRateLimitError):
        tracker().on_notification("account/rateLimits/updated", {"rateLimits": {"rateLimitReachedType": "rate_limit_reached"}})
    assert tracker().on_notification("account/rateLimits/updated", {"rateLimits": {"rateLimitReachedType": None}}) == []


def test_user_mcp_servers_are_disabled(tmp_path, monkeypatch):
    (tmp_path / "config.toml").write_text('[mcp_servers.docs-rag]\ncommand = "x"\n[mcp_servers."odd name"]\nurl = "http://y"\n')
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    argv = app_server_argv("codex")
    assert "mcp_servers.docs-rag.enabled=false" in argv
    assert 'mcp_servers."odd name".enabled=false' in argv


def test_no_user_config_means_no_mcp_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    assert not [a for a in app_server_argv("codex") if a.startswith("mcp_servers")]
