import sys
import warnings

import pytest

from agno_cli_models.versions import UnsupportedCliVersionWarning, check_supported, installed_version, parse_version


def test_parse_version():
    assert parse_version("2.1.286 (Claude Code)") == "2.1.286"
    assert parse_version("codex-cli 0.155.1") == "0.155.1"
    assert parse_version("nothing here") is None


def test_supported_versions_pass_silently():
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert check_supported("claude", "2.1.286") is True
        assert check_supported("claude", "2.1.287") is True
        assert check_supported("codex", "0.155.1") is True


@pytest.mark.parametrize("cli, version", [("claude", "2.1.999"), ("codex", None)])
def test_unknown_version_warns_and_returns_false(cli, version):
    with pytest.warns(UnsupportedCliVersionWarning):
        assert check_supported(cli, version) is False


def test_installed_version_reads_the_binary(tmp_path):
    fake = tmp_path / "fakecli"
    fake.write_text(f"#!{sys.executable}\nprint('9.8.7 (Fake)')\n")
    fake.chmod(0o755)
    assert installed_version(str(fake)) == "9.8.7"
    assert installed_version(str(tmp_path / "missing")) is None


def test_cli_model_is_exported_from_the_package_root():
    import agno_cli_models
    from agno_cli_models._base import CliModel

    assert agno_cli_models.CliModel is CliModel
    assert "CliModel" in agno_cli_models.__all__
    assert issubclass(agno_cli_models.ClaudeCodeModel, CliModel)
    assert issubclass(agno_cli_models.CodexModel, CliModel)


def test_stall_error_is_exported_and_is_a_timeout():
    import agno_cli_models

    assert "CliStallError" in agno_cli_models.__all__
    assert issubclass(agno_cli_models.CliStallError, agno_cli_models.CliTimeoutError)
