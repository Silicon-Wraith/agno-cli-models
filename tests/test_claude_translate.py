import claude_agent_sdk as sdk
import pytest

from agno_cli_models.claude.translate import check_result, observed_model, rate_limit_error, usage_metrics
from agno_cli_models.errors import ContextWindowExceededError, ModelProviderError, ModelRateLimitError

USAGE = {
    "claude-opus-5-5": {"inputTokens": 2, "outputTokens": 748, "cacheReadInputTokens": 100, "cacheCreationInputTokens": 5873, "thinkingTokens": 40},
    "claude-haiku-4-5": {"inputTokens": 10, "outputTokens": 5, "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0},
}


def result(**over):
    base = dict(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1, session_id="s1",
                total_cost_usd=0.06, model_usage=USAGE, result="ok")
    base.update(over)
    return sdk.ResultMessage(**base)


def test_usage_metrics_normalized():
    m = usage_metrics(result())
    assert m.input_tokens == 2 + 100 + 5873 + 10
    assert m.cache_read_tokens == 100
    assert m.cache_write_tokens == 5873
    assert m.output_tokens == 753
    assert m.reasoning_tokens == 40
    assert m.total_tokens == m.input_tokens + m.output_tokens
    assert m.cost == pytest.approx(0.06)


def test_observed_model_is_the_one_that_did_the_work():
    assert observed_model(result()) == "claude-opus-5-5"
    assert observed_model(result(model_usage=None)) is None


def test_check_result_passes_success():
    check_result(result(), "ClaudeCode", "claude-opus-5-5")


@pytest.mark.parametrize("over, error", [
    (dict(is_error=True, api_error_status=429, result="limit"), ModelRateLimitError),
    (dict(is_error=True, result="Claude AI usage limit reached"), ModelRateLimitError),
    (dict(is_error=True, result="prompt is too long: context window exceeded"), ContextWindowExceededError),
    (dict(is_error=True, subtype="error_during_execution", result="boom"), ModelProviderError),
])
def test_check_result_raises_typed_errors(over, error):
    with pytest.raises(error):
        check_result(result(**over), "ClaudeCode", "claude-opus-5-5")


def test_rate_limit_event_only_when_rejected():
    def ev(status):
        return sdk.RateLimitEvent(rate_limit_info=sdk.RateLimitInfo(status=status, rate_limit_type="five_hour", utilization=1.0), uuid="u", session_id="s")

    assert isinstance(rate_limit_error(ev("rejected"), "ClaudeCode", "x"), ModelRateLimitError)
    assert rate_limit_error(ev("allowed_warning"), "ClaudeCode", "x") is None
    assert rate_limit_error(ev("allowed"), "ClaudeCode", "x") is None
