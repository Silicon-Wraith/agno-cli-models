"""Typed failures. Rate limits use Agno's own error so Agno's FallbackConfig and
callers such as Sendesis can react to them without knowing this package."""

from agno.exceptions import ContextWindowExceededError, ModelProviderError, ModelRateLimitError

__all__ = ["CliProtocolError", "CliStallError", "CliTimeoutError", "ContextWindowExceededError", "ModelProviderError",
           "ModelRateLimitError"]


class CliTimeoutError(ModelProviderError):
    """The CLI produced no complete answer within the wall-clock limit."""

    def __init__(self, message: str, model_name: str | None = None, model_id: str | None = None):
        super().__init__(message, status_code=504, model_name=model_name, model_id=model_id)


class CliStallError(CliTimeoutError):
    """The CLI sent nothing for `idle_s` seconds while a call was waiting on it.

    `last_method` names the last message received (a Codex notification method, or the
    Claude SDK message kind) and `answer_open` says whether a final-answer item was open.
    """

    def __init__(self, message: str, model_name: str | None = None, model_id: str | None = None, *,
                 idle_s: float, last_method: str | None, answer_open: bool):
        super().__init__(message, model_name=model_name, model_id=model_id)
        self.idle_s = idle_s
        self.last_method = last_method
        self.answer_open = answer_open


class CliProtocolError(ModelProviderError):
    """The CLI exited or spoke in a way the protocol does not allow."""

    def __init__(self, message: str, model_name: str | None = None, model_id: str | None = None):
        super().__init__(message, status_code=502, model_name=model_name, model_id=model_id)
