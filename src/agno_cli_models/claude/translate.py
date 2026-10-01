"""Claude Agent SDK messages to Agno metrics and typed errors."""

from __future__ import annotations

import re

import claude_agent_sdk as sdk
from agno.metrics import MessageMetrics

from agno_cli_models.errors import ContextWindowExceededError, ModelProviderError, ModelRateLimitError

_RATE = re.compile(r"rate.?limit|usage limit|too many requests|\b429\b", re.IGNORECASE)
_CONTEXT = re.compile(r"context (window|length)|prompt is too long", re.IGNORECASE)


def usage_metrics(result: sdk.ResultMessage) -> MessageMetrics:
    usage = result.model_usage or {}
    m = MessageMetrics()
    m.cache_read_tokens = sum(u.get("cacheReadInputTokens", 0) for u in usage.values())
    m.cache_write_tokens = sum(u.get("cacheCreationInputTokens", 0) for u in usage.values())
    m.input_tokens = sum(u.get("inputTokens", 0) for u in usage.values()) + m.cache_read_tokens + m.cache_write_tokens
    m.output_tokens = sum(u.get("outputTokens", 0) for u in usage.values())
    m.reasoning_tokens = sum(u.get("thinkingTokens", 0) for u in usage.values())
    m.total_tokens = m.input_tokens + m.output_tokens
    m.cost = result.total_cost_usd
    return m


def observed_model(result: sdk.ResultMessage) -> str | None:
    usage = result.model_usage or {}
    if not usage:
        return None
    return max(usage, key=lambda k: usage[k].get("outputTokens", 0))


def check_result(result: sdk.ResultMessage, model_name: str, model_id: str) -> None:
    if not result.is_error:
        return
    text = f"{result.subtype} {result.api_error_status} {result.result} {result.errors}"
    if result.api_error_status == 429 or _RATE.search(text):
        raise ModelRateLimitError(text[:500], model_name=model_name, model_id=model_id)
    if _CONTEXT.search(text):
        raise ContextWindowExceededError(text[:500], status_code=400, model_name=model_name, model_id=model_id)
    raise ModelProviderError(text[:500], model_name=model_name, model_id=model_id)


def rate_limit_error(event: sdk.RateLimitEvent, model_name: str, model_id: str) -> ModelRateLimitError | None:
    info = event.rate_limit_info
    if info.status != "rejected":
        return None
    return ModelRateLimitError(f"rate limit {info.rate_limit_type} rejected (resets {info.resets_at})",
                               model_name=model_name, model_id=model_id)
