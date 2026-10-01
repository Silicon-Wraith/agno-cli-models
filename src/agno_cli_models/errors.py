"""Typed failures. Rate limits use Agno's own error so Agno's FallbackConfig and
callers such as Sendesis can react to them without knowing this package."""

from agno.exceptions import ContextWindowExceededError, ModelProviderError, ModelRateLimitError

__all__ = ["CliProtocolError", "CliTimeoutError", "ContextWindowExceededError", "ModelProviderError", "ModelRateLimitError"]


class CliTimeoutError(ModelProviderError):
    """The CLI produced no complete answer within the wall-clock limit."""

    def __init__(self, message: str, model_name: str | None = None, model_id: str | None = None):
        super().__init__(message, status_code=504, model_name=model_name, model_id=model_id)


class CliProtocolError(ModelProviderError):
    """The CLI exited or spoke in a way the protocol does not allow."""

    def __init__(self, message: str, model_name: str | None = None, model_id: str | None = None):
        super().__init__(message, status_code=502, model_name=model_name, model_id=model_id)
