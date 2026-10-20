"""Range validation happens before the transcript compactor runs."""

import json
from types import SimpleNamespace

import pytest

from lcc.mcp_server import handle_message


def _call(value):
    return handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "compact_transcript",
                "arguments": {
                    "messages": [{"role": "user", "text": "synthetic input"}],
                    "question": "check the synthetic input",
                    "provider": "rules",
                    "min_reduction": value,
                },
            },
        },
        echo_errors=False,
    )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), -0.1, 1.5])
def test_min_reduction_out_of_range_is_rejected_before_compaction(monkeypatch, value):
    calls = []

    def compact(request):
        calls.append(request)
        return SimpleNamespace(messages=[], decisions=[], report={})

    monkeypatch.setattr("lcc.relevance.transcript.compact_transcript", compact)
    response = _call(value)
    assert calls == []
    assert response["result"]["isError"] is True
    assert "min_reduction must be between 0 and 1" in response["result"]["content"][0]["text"]
    json.dumps(response, allow_nan=False)


@pytest.mark.parametrize("value", [0.0, 0.25, 1.0])
def test_min_reduction_valid_boundaries_reach_compaction(monkeypatch, value):
    calls = []

    def compact(request):
        calls.append(request.min_reduction)
        return SimpleNamespace(messages=[], decisions=[], report={})

    monkeypatch.setattr("lcc.relevance.transcript.compact_transcript", compact)
    response = _call(value)
    assert calls == [value]
    assert response["result"].get("isError") is not True
    json.dumps(response, allow_nan=False)
