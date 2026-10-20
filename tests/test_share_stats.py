"""Tests for `lcc share-stats`: aggregation, rounding, privacy, bad input, JSON schema."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from lcc import __version__, share_stats
from lcc.cli import app

runner = CliRunner()

SECRET_OBJECTIVE = "migrate patient Maria Souza records using password hunter2"
SECRET_PATH = "/home/alice/secret-project/payroll_2026.csv"


def _relevance(**overrides: Any) -> dict[str, Any]:
    report: dict[str, Any] = {
        "schema_version": "relevance-compaction-1.2",
        "provider_requested": "mechanical",
        "provider_used": "mechanical",
        "degraded": False,
        "objective": SECRET_OBJECTIVE,
        "tokens_before": 12345,
        "tokens_after": 6789,
        "reduction_ratio": 0.4501,
        "worth_it": True,
        "min_reduction": 0.25,
        "prefix_sha256": "9f2c1e7ab3d54c10e8f6a2b4c6d8e0f1a3b5c7d9e1f3a5b7c9d1e3f5a7b9c1d3",
        "output_sha256": "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b",
        "degradation_reason": None,
        "warnings": [f"could not read {SECRET_PATH}"],
        "decisions": [
            {
                "id": "blk-7f3a9c",
                "text": "ssh deploy@build-01.internal.example.com 'cat ~/.aws/credentials'",
                "reason": "matched AKIAIOSFODNN7EXAMPLE",
            }
        ],
    }
    report.update(overrides)
    return report


def _transcript(**overrides: Any) -> dict[str, Any]:
    report: dict[str, Any] = {
        "schema_version": "transcript-compaction-1.0",
        "mode": "tool-calls",
        "objective": "summarise the bob@corp.example incident",
        "provider_requested": "auto",
        "provider_used": "specialist-v3",
        "rules_mode": "stashed",
        "stash_dir": "/Users/bob/.lcc/stash",
        "degraded": False,
        "degradation_reason": None,
        "tokens_before": 40210,
        "tokens_after": 9870,
        "reduction_ratio": 0.7546,
        "worth_it": True,
        "tool_calls_stashed": 3,
        "decisions": [{"tool_call_id": "toolu_01XYZsecretid", "command": "rm -rf /srv/data"}],
    }
    report.update(overrides)
    return report


LOG_ENTRIES: list[dict[str, Any]] = [
    {
        "id": "a1b2c3d4e5f6",
        "date": "2026-10-01",
        "goal": "find invoice for <EMAIL> at acme-holdings",
        "language": "en",
        "session": "0f1e2d3c4b5a6978",
        "tool": "mcp:compact_transcript",
        "field": "billing-ops",
        "lcc_version": "1.2.0",
        "redaction_ruleset_sha256": "deadbeefcafebabe0123456789abcdef",
        "provider": "specialist-v3",
        "degradation_reason": "none",
        "trigger": "idle",
        "gap_bucket": "2h-8h",
        "removed_tokens": 10892,
    },
    {
        "id": "b2c3d4e5f6a1",
        "date": "2026-10-03",
        "goal": "qual o peso do paciente Joaquim",
        "language": "pt",
        "session": "8a7b6c5d4e3f2a1b",
        "tool": "mcp:compact_transcript",
        "field": "none",
        "lcc_version": "1.2.0",
        "redaction_ruleset_sha256": "deadbeefcafebabe0123456789abcdef",
        "provider": "degraded",
        "degradation_reason": "jev timeout talking to api.internal.corp",
    },
    {
        "id": "c3d4e5f6a1b2",
        "date": "2026-09-20",
        "goal": "old entry from september",
        "language": "en",
        "session": "8a7b6c5d4e3f2a1b",
        "tool": "cli:compact",
        "field": "none",
        "lcc_version": "1.1.0",
        "redaction_ruleset_sha256": "deadbeefcafebabe0123456789abcdef",
    },
]

ENV = {"lcc_version": "9.9.9", "os_family": "Linux", "python": "3.12"}


def _write(path: Path, payload: Any) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _write_log(directory: Path, entries: list[dict[str, Any]], extra: str = "") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "requests.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in entries) + extra, encoding="utf-8")
    return directory


def _run(*args: str) -> Any:
    return runner.invoke(app, ["share-stats", *args])


# --- rounding and helpers ---------------------------------------------------------------


def test_rounding_is_half_up_and_deterministic() -> None:
    assert share_stats.round_tokens(12345) == 12300
    assert share_stats.round_tokens(12350) == 12400
    assert share_stats.round_tokens(49) == 0
    assert share_stats.round_tokens(50) == 100
    assert share_stats.round_ratio(0.4501) == 0.45
    assert share_stats.round_ratio(0.125) == 0.13
    assert share_stats.round_ratio(0.7546) == 0.75


def test_percentile_interpolates() -> None:
    values = [0.1, 0.2, 0.3, 0.4, 0.5]
    assert share_stats.percentile(values, 0.5) == 0.3
    assert abs(share_stats.percentile(values, 0.1) - 0.14) < 1e-9  # type: ignore[operator]
    assert share_stats.percentile([], 0.5) is None
    assert share_stats.percentile([0.7], 0.9) == 0.7


def test_os_family_is_coarse() -> None:
    assert share_stats.os_family("Darwin") == "macOS"
    assert share_stats.os_family("Linux") == "Linux"
    assert share_stats.os_family("Windows") == "Windows"
    assert share_stats.os_family("FreeBSD") == "other"


def test_parse_since_rejects_bad_dates() -> None:
    assert share_stats.parse_since("2026-10-02").isoformat() == "2026-10-02"  # type: ignore[union-attr]
    for bad in ("2026-1-1", "yesterday", "2026-10-02T10:00"):
        try:
            share_stats.parse_since(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


# --- aggregation --------------------------------------------------------------------------


def test_aggregates_reports_and_log(tmp_path: Path) -> None:
    reports = [
        _write(tmp_path / "a.json", _relevance()),
        _write(tmp_path / "b.json", _transcript()),
        _write(
            tmp_path / "c.json",
            _relevance(
                provider_used="laya+mechanical_fallback",
                provider_requested="laya",
                degraded=True,
                tokens_before=1000,
                tokens_after=900,
                reduction_ratio=0.1,
            ),
        ),
    ]
    log_dir = _write_log(tmp_path / "log", LOG_ENTRIES)
    inputs = share_stats.load_inputs(reports, log_dir / "requests.jsonl")
    summary = share_stats.build_summary(inputs, env=ENV)

    rep = summary["reports"]
    assert rep["passes"] == 3
    assert rep["by_mode"] == {"blocks": 2, "tool-calls": 1}
    assert rep["by_provider"] == {
        "laya+mechanical_fallback": 1,
        "mechanical": 1,
        "specialist-v3": 1,
    }
    assert rep["degraded_passes"] == 1
    assert rep["fallback_passes"] == 1
    assert rep["under_25pct_gate"] == 1
    assert rep["tokens_before_total"] == 53600  # 12345 + 40210 + 1000 = 53555
    assert rep["tokens_after_total"] == 17600  # 6789 + 9870 + 900 = 17559
    assert rep["reduction_ratio"] == {"p10": 0.17, "median": 0.45, "p90": 0.69}
    assert rep["per_pass_reduction_ratios"] is None  # fewer than 5 passes
    assert rep["stashed_outputs_total"] == 3

    log = summary["request_log"]
    assert log["found"] is True
    assert log["entries"] == 3
    assert log["active_days"] == 3
    assert log["by_tool"] == {"cli:compact": 1, "mcp:compact_transcript": 2}
    assert log["by_provider"] == {"degraded": 1, "specialist-v3": 1}
    assert log["degraded_passes"] == 1
    assert log["recall_calls"] is None
    assert log["idle_stash_passes"] == 1
    assert log["idle_removed_tokens_total"] == 10900
    assert log["idle_by_gap_bucket"] == {"2h-8h": 1}
    assert log["by_lcc_version"] == {"1.1.0": 1, "1.2.0": 2}


def test_per_pass_ratios_appear_sorted_from_five_passes(tmp_path: Path) -> None:
    ratios = [0.9, 0.1, 0.55, 0.3333, 0.25]
    reports = [
        _write(tmp_path / f"r{i}.json", _relevance(reduction_ratio=r)) for i, r in enumerate(ratios)
    ]
    summary = share_stats.build_summary(share_stats.load_inputs(reports, None), env=ENV)
    assert summary["reports"]["per_pass_reduction_ratios"] == [0.1, 0.25, 0.33, 0.55, 0.9]
    assert summary["reports"]["under_25pct_gate"] == 1


def test_since_filters_log_by_day(tmp_path: Path) -> None:
    log_dir = _write_log(tmp_path / "log", LOG_ENTRIES)
    since = share_stats.parse_since("2026-10-01")
    inputs = share_stats.load_inputs([], log_dir / "requests.jsonl", since=since)
    summary = share_stats.build_summary(inputs, since=since, env=ENV)
    assert summary["request_log"]["entries"] == 2
    assert summary["inputs"]["since"] == "2026-10-01"


def test_unknown_labels_become_other(tmp_path: Path) -> None:
    report = _write(tmp_path / "x.json", _relevance(provider_used="alice-laptop-provider"))
    entry = dict(LOG_ENTRIES[0], tool="mcp:secret_tool", gap_bucket="3h17m", lcc_version="dev")
    log_dir = _write_log(tmp_path / "log", [entry])
    summary = share_stats.build_summary(
        share_stats.load_inputs([report], log_dir / "requests.jsonl"), env=ENV
    )
    assert summary["reports"]["by_provider"] == {"other": 1}
    assert summary["request_log"]["by_tool"] == {"other": 1}
    assert summary["request_log"]["idle_by_gap_bucket"] == {"other": 1}
    assert summary["request_log"]["by_lcc_version"] == {"other": 1}


def test_recall_calls_are_counted_when_logged(tmp_path: Path) -> None:
    entries = [dict(LOG_ENTRIES[2], tool="mcp:recall"), dict(LOG_ENTRIES[2], tool="cli:recall")]
    log_dir = _write_log(tmp_path / "log", entries)
    summary = share_stats.build_summary(
        share_stats.load_inputs([], log_dir / "requests.jsonl"), env=ENV
    )
    assert summary["request_log"]["recall_calls"] == 2


# --- privacy ----------------------------------------------------------------------------


def _strings(value: Any, keys: set[str], out: set[str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            keys.add(key)
            _strings(item, keys, out)
    elif isinstance(value, list):
        for item in value:
            _strings(item, keys, out)
    elif isinstance(value, str):
        out.add(value)


# Fixed labels the summary is allowed to repeat (allowlisted enums and versions).
_ALLOWED_VALUES = {
    "mechanical",
    "laya",
    "auto",
    "stashed",
    "specialist-v3",
    "laya+mechanical_fallback",
    "degraded",
    "tool-calls",
    "mcp:compact_transcript",
    "cli:compact",
    "2h-8h",
    "idle",
    "none",
    "1.2.0",
    "1.1.0",
    "relevance-compaction-1.2",
    "transcript-compaction-1.0",
}


def test_output_never_contains_input_strings(tmp_path: Path) -> None:
    secret_dir = tmp_path / "home-alice-SecretClientAcme"
    secret_dir.mkdir()
    reports = [
        _write(secret_dir / "acme_payroll_report.json", _relevance()),
        _write(secret_dir / "incident_bob_transcript.json", _transcript()),
        _write(
            secret_dir / "fallback_laya_report.json",
            _relevance(provider_used="laya+mechanical_fallback", degraded=True),
        ),
    ]
    log_dir = _write_log(secret_dir / "requestlogdir", LOG_ENTRIES)

    keys: set[str] = set()
    values: set[str] = set()
    for payload in [_relevance(), _transcript(), *LOG_ENTRIES]:
        _strings(payload, keys, values)
    values |= {str(p) for p in reports} | {p.name for p in reports} | {str(log_dir)}
    values |= {secret_dir.name, "requestlogdir"}
    needles = {v for v in values if len(v) > 3 and v not in _ALLOWED_VALUES and v not in keys}
    assert SECRET_OBJECTIVE in needles and SECRET_PATH not in needles  # path is inside a list
    needles |= {SECRET_PATH, "hunter2", "Maria Souza", "AKIAIOSFODNN7EXAMPLE", "Joaquim"}
    # Long hex/ids must not leak even as substrings (e.g. truncated hashes).
    needles |= {v[:8] for v in values if len(v) >= 12 and v.isalnum()}

    args = [*map(str, reports), "--request-log", str(log_dir)]
    markdown = _run(*args)
    as_json = _run(*args, "--json")
    assert markdown.exit_code == 0, markdown.output
    assert as_json.exit_code == 0, as_json.output
    for rendered in (markdown.stdout, as_json.stdout):
        leaked = sorted(n for n in needles if n in rendered)
        assert not leaked, leaked
        assert "2026-10-01" not in rendered and "2026-10-03" not in rendered


# --- bad and empty input ----------------------------------------------------------------


def test_corrupt_and_old_inputs_are_skipped_and_counted(tmp_path: Path) -> None:
    good = _write(tmp_path / "good.json", _relevance())
    not_json = tmp_path / "broken.json"
    not_json.write_text('{"objective": "TOPSECRETCONTENT", ', encoding="utf-8")
    binary = tmp_path / "binary.json"
    binary.write_bytes(b"\xff\xfe\x00TOPSECRETCONTENT")
    old = _write(tmp_path / "old.json", {"schema_version": "relevance-compaction-0.9"})
    optimize = _write(tmp_path / "opt.json", {"schema_version": "1.0", "x": "TOPSECRETCONTENT"})
    listing = _write(tmp_path / "list.json", ["TOPSECRETCONTENT"])
    bad_fields = _write(tmp_path / "bad.json", _relevance(tokens_before="TOPSECRETCONTENT"))
    missing = tmp_path / "does-not-exist.json"
    log_dir = _write_log(
        tmp_path / "log", LOG_ENTRIES[:1], extra='{"goal": "TOPSECRETCONTENT"\n[1, 2]\n'
    )

    paths = [good, not_json, binary, old, optimize, listing, bad_fields, missing, tmp_path]
    result = _run(*map(str, paths), "--request-log", str(log_dir), "--json")
    assert result.exit_code == 0, result.output
    summary = json.loads(result.stdout)
    assert summary["inputs"]["report_files_read"] == 1
    assert summary["inputs"]["skipped_by_reason"] == {
        "invalid_fields": 1,
        "not_a_report": 1,
        "not_json": 1,
        "request_log_line": 2,
        "unreadable": 3,
        "unsupported_format": 2,
    }
    assert summary["inputs"]["inputs_skipped"] == 10
    assert summary["request_log"]["entries"] == 1
    assert "TOPSECRETCONTENT" not in result.output
    assert str(tmp_path) not in result.output


def test_empty_input_prints_a_valid_summary(tmp_path: Path) -> None:
    result = _run("--request-log", str(tmp_path / "nothing-here"))
    assert result.exit_code == 0, result.output
    assert "passes | 0" in result.stdout
    assert "Not found" in result.stdout
    assert "n/a / n/a / n/a" in result.stdout
    assert '"Share your numbers"' in result.stdout.splitlines()[-1]
    assert "template=results.yml" in result.stdout.splitlines()[-1]

    skipped_log = _run("--no-request-log", "--json")
    summary = json.loads(skipped_log.stdout)
    assert summary["request_log"]["read"] is False
    assert summary["reports"]["passes"] == 0
    assert summary["reports"]["reduction_ratio"] == {"p10": None, "median": None, "p90": None}


def test_bad_since_exits_2() -> None:
    result = _run("--no-request-log", "--since", "10/01/2026")
    assert result.exit_code == 2


# --- schema and determinism -------------------------------------------------------------


def test_json_schema_shape(tmp_path: Path) -> None:
    report = _write(tmp_path / "a.json", _relevance())
    result = _run(str(report), "--no-request-log", "--json")
    summary = json.loads(result.stdout)
    assert summary["schema_version"] == "lcc-share-stats-1.0"
    assert summary["tool_version"] == __version__
    assert set(summary) == {
        "schema_version",
        "tool_version",
        "environment",
        "inputs",
        "reports",
        "request_log",
    }
    assert set(summary["environment"]) == {"lcc_version", "os_family", "python"}
    assert summary["environment"]["os_family"] in {"macOS", "Linux", "Windows", "other"}
    assert set(summary["reports"]) == {
        "passes",
        "by_mode",
        "by_provider",
        "degraded_passes",
        "fallback_passes",
        "under_25pct_gate",
        "tokens_before_total",
        "tokens_after_total",
        "reduction_ratio",
        "per_pass_reduction_ratios",
        "stashed_outputs_total",
    }
    assert set(summary["request_log"]) == {
        "read",
        "found",
        "entries",
        "active_days",
        "by_tool",
        "by_provider",
        "degraded_passes",
        "recall_calls",
        "idle_stash_passes",
        "idle_removed_tokens_total",
        "idle_by_gap_bucket",
        "by_lcc_version",
    }


def test_output_is_deterministic_and_order_independent(tmp_path: Path) -> None:
    a = _write(tmp_path / "a.json", _relevance())
    b = _write(tmp_path / "b.json", _transcript())
    log_dir = _write_log(tmp_path / "log", LOG_ENTRIES)
    first = _run(str(a), str(b), "--request-log", str(log_dir))
    second = _run(str(b), str(a), str(a), "--request-log", str(log_dir))
    assert first.stdout == second.stdout
    json_first = _run(str(a), str(b), "--request-log", str(log_dir), "--json")
    json_second = _run(str(b), str(a), "--request-log", str(log_dir), "--json")
    assert json_first.stdout == json_second.stdout


def test_output_file(tmp_path: Path) -> None:
    out = tmp_path / "stats.md"
    result = _run("--no-request-log", "-o", str(out))
    assert result.exit_code == 0
    assert result.stdout == ""
    assert out.read_text(encoding="utf-8").startswith("## lcc share-stats")
