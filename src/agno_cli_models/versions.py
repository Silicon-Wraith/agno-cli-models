"""CLI versions this release was tested against."""

from __future__ import annotations

import re
import subprocess
import warnings

from agno_cli_models._env import clean_env

SUPPORTED: dict[str, tuple[str, ...]] = {"claude": ("2.1.286",), "codex": ("0.155.1",)}
_VERSION = re.compile(r"\b(\d+\.\d+\.\d+)\b")


class UnsupportedCliVersionWarning(UserWarning):
    pass


def parse_version(text: str) -> str | None:
    match = _VERSION.search(text or "")
    return match.group(1) if match else None


def installed_version(binary: str) -> str | None:
    try:
        done = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=30,
                              stdin=subprocess.DEVNULL, env=clean_env())
    except (OSError, subprocess.TimeoutExpired):
        return None
    return parse_version(done.stdout) if done.returncode == 0 else None


def check_supported(cli: str, version: str | None) -> bool:
    if version in SUPPORTED.get(cli, ()):
        return True
    warnings.warn(
        f"{cli} CLI version {version!r} is not in the tested set {SUPPORTED.get(cli)}; behavior may differ",
        UnsupportedCliVersionWarning,
        stacklevel=2,
    )
    return False
