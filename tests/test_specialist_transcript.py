"""specialist-v3 on the tool-call path (Claude Code hook, MCP compact_transcript, CLI).

``auto`` is specialist-v3 here, ahead of Jev (opt-in) and Laya (deprecated, opt-in). Offline:
resolving a judge through the ``_resolve_client`` seam fails the test unless a test installs a
stub, and the transcript is synthetic (``tests/specialist_synthetic.py``), stored as JSONL
the way a session log is.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

import lcc.relevance.compactor as compactor_module
import lcc.relevance.transcript as transcript_module
from lcc import request_log
from lcc.cli import app
from lcc.mcp_server import handle_message
from lcc.relevance.specialist import SpecialistModelError
from lcc.relevance.transcript import TranscriptCompactionRequest, compact_transcript
from specialist_synthetic import GOAL, synthetic_payload


@pytest.fixture(autouse=True)
def _no_judge(monkeypatch):
    def refuse(*_):
        raise AssertionError("auto and specialist-v3 must not resolve Jev or Laya")

    monkeypatch.setenv("LCC_DISABLE_NETWORK", "1")
    monkeypatch.setattr(transcript_module, "_resolve_client", refuse)


@pytest.fixture
def messages(tmp_path) -> list[dict]:
    """The synthetic session written to and read back from a JSONL transcript file."""
    path = tmp_path / "session.jsonl"
    path.write_text("".join(json.dumps(m) + "\n" for m in synthetic_payload()), encoding="utf-8")
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def run(messages, **overrides):
    params = {"payload": {"messages": messages}, "question": GOAL, "provider": "auto"}
    params.update(overrides)
    return compact_transcript(TranscriptCompactionRequest(**params))


def test_auto_ranks_with_specialist_v3_and_passes_segments(messages, monkeypatch):
    seen = []
    real = compactor_module.compact_context

    def spy(request):
        seen.append(request)
        return real(request)

    monkeypatch.setattr(compactor_module, "compact_context", spy)
    result = run(messages)
    report = result.report

    [request] = seen
    assert request.provider == "specialist-v3"
    parts = sum(
        bool(m.get("text")) + len(m.get("toolUses", [])) + len(m.get("toolResults", []))
        for m in messages
    )
    assert request.segments is not None and len(request.segments) == parts
    assert {s.kind for s in request.segments} == {"text", "tool_use", "tool_result"}

    assert report["provider_used"] == "specialist-v3"
    assert report["degraded"] is False and report["degradation_reason"] is None
    assert report["semantic_guarantee"] == "none"
    judged = [d for d in report["decisions"] if d["source"] == "specialist-v3"]
    assert judged and report["blocks_scored"] == len(judged)
    assert report["tool_calls_dropped"] + report["tool_calls_trimmed"] > 0
    assert report["chars_after"] < report["chars_before"]
    # User and assistant text is never touched; pinned calls are never ranked.
    assert [m.text for m in result.messages if m.text] == [m["text"] for m in messages if m["text"]]
    assert all(d["decision"] == "keep" for d in report["decisions"] if d["source"] == "pin")


def test_explicit_specialist_v3_matches_auto(messages):
    assert run(messages, provider="specialist-v3").decisions == run(messages).decisions


def test_jev_runs_only_when_named(messages, monkeypatch):
    class Keep:
        model = "fake"
        last_resolved_model = "jev-x"

        def evaluate(self, state, questions):
            return {"answers": {q: {"type": "noul", "noul": 0.9} for q in questions}}

    monkeypatch.setattr(transcript_module, "_resolve_client", lambda *_: Keep())
    assert run(messages).report["provider_used"] == "specialist-v3"  # a key changes nothing
    report = run(messages, provider="jev").report
    assert report["provider_used"] == "jev" and report["degraded"] is False


@pytest.mark.parametrize("provider", ["auto", "specialist-v3"])
def test_a_specialist_failure_keeps_every_call(messages, monkeypatch, provider):
    def broken():
        raise SpecialistModelError("sha256 mismatch")

    monkeypatch.setattr(compactor_module, "load_judge", broken)
    report = run(messages, provider=provider).report
    assert report["degraded"] is True
    assert report["degradation_reason"] == "specialist_v3_failed_fail_safe"
    assert report["provider_used"] == "degraded"
    assert report["tool_calls_dropped"] == report["tool_calls_trimmed"] == 0
    assert report["chars_after"] == report["chars_before"]
    assert any(w.startswith("specialist_v3_failed") for w in report["warnings"])


def test_mcp_compact_transcript_logs_provider_and_reason(messages, tmp_path, monkeypatch):
    monkeypatch.setenv(request_log.ENV_FLAG, "1")
    monkeypatch.setenv(request_log.ENV_DIR, str(tmp_path / "log"))
    resp = handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "compact_transcript",
                "arguments": {"messages": messages, "question": GOAL},  # default: auto
            },
        }
    )
    assert resp is not None and not resp["result"].get("isError")
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload["report"]["provider_used"] == "specialist-v3"
    [entry] = request_log.read_entries(tmp_path / "log")
    assert entry["tool"] == "mcp:compact_transcript"
    assert entry["provider"] == "specialist-v3" and entry["degradation_reason"] == "none"
    assert request_log.summarize(tmp_path / "log")["by_provider"] == {"specialist-v3": 1}


def test_cli_tool_calls_defaults_to_specialist_v3(messages, tmp_path):
    source = tmp_path / "t.json"
    source.write_text(json.dumps({"messages": messages}), encoding="utf-8")
    runner = CliRunner()
    for provider in (["--provider", "specialist-v3"], []):  # [] = the CLI default, auto
        report_path = tmp_path / "r.json"
        result = runner.invoke(
            app,
            ["compact", str(source), "--question", GOAL, "--mode", "tool-calls",
             *provider, "--dry-run", "--report", str(report_path)],
        )
        assert result.exit_code == 0, result.output
        report = json.loads(report_path.read_text(encoding="utf-8"))
        assert report["provider_used"] == "specialist-v3"
        assert report["degraded"] is False


def test_cli_blocks_mode_still_refuses_specialist_v3(tmp_path):
    source = tmp_path / "t.txt"
    source.write_text("plain text\n", encoding="utf-8")
    result = CliRunner().invoke(
        app, ["compact", str(source), "--question", "q", "--provider", "specialist-v3"]
    )
    assert result.exit_code == 2
