"""Agno models backed by the official Claude Code and Codex clients."""

from agno_cli_models._base import CliModel
from agno_cli_models.claude.model import ClaudeCodeModel
from agno_cli_models.codex.model import CodexModel
from agno_cli_models.errors import CliProtocolError, CliTimeoutError, ModelProviderError, ModelRateLimitError
from agno_cli_models.versions import SUPPORTED, UnsupportedCliVersionWarning

__version__ = "0.1.0"
__all__ = ["CliModel", "ClaudeCodeModel", "CodexModel", "CliProtocolError", "CliTimeoutError", "ModelProviderError",
           "ModelRateLimitError", "SUPPORTED", "UnsupportedCliVersionWarning", "__version__"]
