"""Keep ordinary token counts without exempting credentials from redaction."""

from time import perf_counter

import pytest

from lcc import request_log
from lcc.request_log import redact


@pytest.mark.parametrize(
    "goal",
    [
        "max_tokens=4000",
        "tokens: 500",
        "set MAX_TOKENS = 4000 for this run",
        "compare tokens: 500 with max_tokens=4000",
        "tokens=0",
    ],
)
def test_plain_integer_token_counts_are_preserved(goal):
    assert redact(goal) == goal


@pytest.mark.parametrize(
    "goal",
    [
        "api_key=123456",
        "access_token: 482913",
        "GITHUB_TOKEN=123456",
        "otp_secret=482913",
        "password=123456",
        "token=4000",
        "my_max_tokens=4000",
        "max_tokens=4000abc",
        "max_tokens=4000.5",
        "tokens: 500-secret",
        'tokens: "500"',
    ],
)
def test_token_count_exemption_does_not_hide_secret_matches(goal):
    assert redact(goal) == "<TOKEN>"


def test_preserved_count_does_not_bypass_other_redaction_rules():
    assert redact("max_tokens=4000 api_key=123456") == "max_tokens=4000 <TOKEN>"
    assert redact("max_tokens=123456789") == "max_tokens=<NUMBER>"


@pytest.mark.parametrize("target", ["goal", "field"])
@pytest.mark.parametrize(
    ("suffix", "expected_tail"),
    [("abc", None), ("", "max_tokens=4000"), (" more text", "max_tokens=4000")],
)
def test_log_limit_does_not_turn_a_secret_into_a_count(
    tmp_path, monkeypatch, target, suffix, expected_tail
):
    monkeypatch.setenv(request_log.ENV_FLAG, "1")
    monkeypatch.setenv(request_log.ENV_DIR, str(tmp_path))
    assignment = "max_tokens=4000"
    prefix = "x " * ((request_log.MAX_GOAL_CHARS - len(assignment)) // 2)
    prefix = prefix.ljust(request_log.MAX_GOAL_CHARS - len(assignment))
    value = prefix + assignment + suffix

    path = request_log.log_request(
        value if target == "goal" else "keep count",
        field=value if target == "field" else None,
    )

    assert path is not None
    (entry,) = request_log.read_entries(tmp_path)
    expected = prefix.rstrip() if expected_tail is None else prefix + expected_tail
    assert entry[target] == expected
    assert len(entry[target]) <= request_log.MAX_GOAL_CHARS


@pytest.mark.parametrize("target", ["goal", "field"])
@pytest.mark.parametrize("separator", [" ", "\n", "\t", "\u2003"])
def test_log_limit_drops_partial_words_after_whitespace(tmp_path, monkeypatch, target, separator):
    monkeypatch.setenv(request_log.ENV_FLAG, "1")
    monkeypatch.setenv(request_log.ENV_DIR, str(tmp_path))
    prefix = "keep" + separator
    value = prefix + "a" * request_log.MAX_GOAL_CHARS

    request_log.log_request(
        value if target == "goal" else "keep count",
        field=value if target == "field" else None,
    )

    (entry,) = request_log.read_entries(tmp_path)
    assert entry[target] == "keep"


def test_log_limit_preserves_a_complete_word_before_whitespace(tmp_path, monkeypatch):
    monkeypatch.setenv(request_log.ENV_FLAG, "1")
    monkeypatch.setenv(request_log.ENV_DIR, str(tmp_path))
    prefix = "x " * (request_log.MAX_GOAL_CHARS // 2)

    request_log.log_request(prefix + "tail")

    (entry,) = request_log.read_entries(tmp_path)
    assert entry["goal"] == prefix


def test_log_limit_drops_an_incomplete_single_word(tmp_path, monkeypatch):
    monkeypatch.setenv(request_log.ENV_FLAG, "1")
    monkeypatch.setenv(request_log.ENV_DIR, str(tmp_path))

    request_log.log_request("a-" * 20_000)

    (entry,) = request_log.read_entries(tmp_path)
    assert entry["goal"] == ""


def test_long_goal_redaction_remains_bounded(tmp_path, monkeypatch):
    monkeypatch.setenv(request_log.ENV_FLAG, "1")
    monkeypatch.setenv(request_log.ENV_DIR, str(tmp_path))
    started = perf_counter()

    path = request_log.log_request("keep " + "a-" * 20_000)

    assert perf_counter() - started < 1.0
    assert path is not None
    (entry,) = request_log.read_entries(tmp_path)
    assert entry["goal"] == "keep"
