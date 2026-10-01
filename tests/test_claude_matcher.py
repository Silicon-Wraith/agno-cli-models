from agno_cli_models.claude.matcher import CallIdMatcher


def test_matcher_pairs_by_arguments_not_order():
    m = CallIdMatcher()
    m.record("add", {"a": 1}, "id-A")
    m.record("add", {"a": 2}, "id-B")
    assert m.take("add", {"a": 2}) == "id-B"
    assert m.take("add", {"a": 1}) == "id-A"


def test_same_arguments_fall_back_to_order():
    m = CallIdMatcher()
    m.record("add", {"a": 1}, "id-1")
    m.record("add", {"a": 1}, "id-2")
    assert [m.take("add", {"a": 1}), m.take("add", {"a": 1})] == ["id-1", "id-2"]


def test_unrecorded_call_gets_a_fresh_id():
    m = CallIdMatcher()
    assert m.take("add", {}) != m.take("add", {})


def test_missing_tool_use_id_is_generated():
    m = CallIdMatcher()
    rid = m.record("add", {}, None)
    assert rid and m.take("add", {}) == rid
