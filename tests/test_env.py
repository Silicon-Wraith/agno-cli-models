import os

from agno_cli_models._env import blanking_overrides, clean_env, is_removed

PARENT = {
    "HOME": "/home/u",
    "PATH": "/usr/bin",
    "ANTHROPIC_API_KEY": "sk-a",
    "OPENAI_API_KEY": "sk-o",
    "CODEX_API_KEY": "sk-c",
    "CLAUDECODE": "1",
    "CLAUDE_CODE_SESSION_ID": "parent",
    "CLAUDE_EFFORT": "max",
    "CLAUDE_CONFIG_DIR": "/home/u/.claude-alt",
    "CLAUDE_CODE_ENTRYPOINT": "cli",
}


def test_is_removed():
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CODEX_API_KEY", "CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_EFFORT",
                "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "OPENAI_BASE_URL", "CLAUDE_CODE_OAUTH_TOKEN"):
        assert is_removed(key), key
    for key in ("HOME", "PATH", "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_ENTRYPOINT"):
        assert not is_removed(key), key


def test_clean_env_drops_keys_and_parent_session_vars():
    env = clean_env(PARENT)
    assert env == {"HOME": "/home/u", "PATH": "/usr/bin", "CLAUDE_CONFIG_DIR": "/home/u/.claude-alt", "CLAUDE_CODE_ENTRYPOINT": "cli"}


def test_blanking_overrides_blank_only_present_removed_keys():
    over = blanking_overrides(PARENT)
    assert over == {k: "" for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CODEX_API_KEY", "CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_EFFORT")}


def test_blanking_overrides_never_mutate_environ(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-live")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent")
    blanking_overrides()
    clean_env()
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-live"
    assert os.environ["CLAUDE_CODE_SESSION_ID"] == "parent"
