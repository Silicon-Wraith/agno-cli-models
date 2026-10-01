import asyncio

from agno.models.message import Message

from agno_cli_models._common import (
    canon_args,
    config_hash,
    find_cli_session,
    last_user_text,
    run_sync,
    session_marker,
    split_system,
    strict_schema,
    strip_nulls,
    text_of,
    transcript_prompt,
)


def test_text_of():
    assert text_of(None) == ""
    assert text_of("hi") == "hi"
    assert text_of({"a": 1}) == '{"a": 1}'
    assert text_of(["a", "b"]) == "a\nb"


def test_canon_args_is_order_independent():
    assert canon_args({"b": 1, "a": 2}) == canon_args({"a": 2, "b": 1})
    assert canon_args(None) == "{}"


def test_split_system_and_last_user():
    msgs = [Message(role="system", content="S1"), Message(role="user", content="u1"),
            Message(role="system", content="S2"), Message(role="assistant", content="a1"),
            Message(role="user", content="u2")]
    system, rest = split_system(msgs)
    assert system == "S1\n\nS2"
    assert [m.role for m in rest] == ["user", "assistant", "user"]
    assert last_user_text(rest) == "u2"


def test_transcript_prompt_replays_history():
    rest = [Message(role="user", content="hello"), Message(role="assistant", content="hi there"),
            Message(role="user", content="and now?")]
    assert transcript_prompt(rest) == "Conversation so far:\nUSER: hello\nASSISTANT: hi there\n\nUSER: and now?"


def test_transcript_prompt_without_history_is_just_the_message():
    assert transcript_prompt([Message(role="user", content="only")]) == "only"


def test_find_cli_session_takes_latest_matching_marker():
    msgs = [Message(role="assistant", content="a", provider_data=session_marker("claude", "s1")),
            Message(role="assistant", content="b", provider_data=session_marker("codex", "t1")),
            Message(role="assistant", content="c", provider_data=session_marker("claude", "s2"))]
    assert find_cli_session(msgs, "claude") == "s2"
    assert find_cli_session(msgs, "codex") == "t1"


def test_find_cli_session_ignores_other_cli_and_absent_data():
    msgs = [Message(role="user", content="x"), Message(role="assistant", content="y"),
            Message(role="assistant", content="z", provider_data={"something": "else"})]
    assert find_cli_session(msgs, "claude") is None


def test_strict_schema_requires_every_property_and_nullable_optionals():
    schema = {"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "integer"}}, "required": ["a"]}
    strict = strict_schema(schema)
    assert strict["required"] == ["a", "b"]
    assert strict["additionalProperties"] is False
    assert strict["properties"]["b"]["type"] == ["integer", "null"]
    assert schema["required"] == ["a"]  # input not mutated


def test_strip_nulls():
    assert strip_nulls({"a": None, "b": [{"c": None, "d": 1}]}) == {"b": [{"d": 1}]}


def test_config_hash_is_stable_and_sensitive():
    assert config_hash({"a": 1, "b": [1, 2]}) == config_hash({"b": [1, 2], "a": 1})
    assert config_hash({"a": 1}) != config_hash({"a": 2})


async def _double(x):
    await asyncio.sleep(0)
    return x * 2


def test_run_sync_without_loop():
    assert run_sync(_double(2)) == 4


def test_run_sync_inside_running_loop():
    async def outer():
        return run_sync(_double(3))

    assert asyncio.run(outer()) == 6
