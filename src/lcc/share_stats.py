"""Anonymous "share your numbers" summary for beta testers (``lcc share-stats``).

Turns local compaction reports and the opt-in request log into a small summary of counts
that a tester can paste into a public issue. Nothing is sent anywhere: this module only reads
local files and returns a dict / Markdown string.

Privacy contract (``lcc-share-stats-1.0``): the summary holds counts, rounded token totals,
rounded ratios and labels from fixed allowlists only. It never copies a string from an input
file into the output: no objective, goal, path, file name, command, id, hash, session,
username, hostname or timestamp finer than a day. A label outside the allowlist becomes
``"other"``. Token totals are rounded to the nearest 100 and ratios to 2 decimals so the
summary cannot be matched back to one session.

Deterministic: the same inputs and environment give byte-identical output (no clock, sorted
keys and sorted per-pass ratios, no input order dependence).
"""

from __future__ import annotations

import json
import math
import platform
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from lcc import __version__

SCHEMA_VERSION = "lcc-share-stats-1.0"
GATE = 0.25  # the default minimum-reduction gate of `lcc compact` (both modes)
TOKEN_ROUNDING = 100
MIN_PASSES_FOR_LIST = 5  # below this, per-pass ratios are left out
ISSUE_TEMPLATE = "Share your results"

_RELEVANCE_PREFIX = "relevance-compaction-1."
_TRANSCRIPT_PREFIX = "transcript-compaction-1."

_BASE_PROVIDERS = (
    "mechanical",
    "laya",
    "jev",
    "nimble",
    "rules",
    "auto",
    "cache",
    "degraded",
    "specialist",
    "specialist-v3",
    "specialist_shadow",
)
_PROVIDER_SUFFIXES = ("mechanical_fallback", "partial_fail_safe")
_PROVIDERS = frozenset(
    _BASE_PROVIDERS
    + tuple(f"{base}+{suffix}" for base in _BASE_PROVIDERS for suffix in _PROVIDER_SUFFIXES)
)
_MODES = frozenset({"blocks", "tool-calls"})
_GAP_BUCKETS = frozenset({"<5m", "5m-1h", "1h-2h", "2h-8h", ">8h", "unknown"})
_TOOLS = frozenset(
    {
        "cli:compact",
        "cli:recall",
        "mcp:compact",
        "mcp:compact_transcript",
        "mcp:inspect",
        "mcp:prepare",
        "mcp:explain",
        "mcp:recall",
        "mcp:intake",
    }
)
_RECALL_TOOLS = frozenset({"cli:recall", "mcp:recall"})
_VERSION = re.compile(r"\d{1,3}\.\d{1,3}\.\d{1,3}")
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


def _label(value: Any, allowed: frozenset[str]) -> str:
    return value if isinstance(value, str) and value in allowed else "other"


def os_family(system: str | None = None) -> str:
    """macOS / Linux / Windows / other. Never the release, kernel or hostname."""
    name = (system if system is not None else platform.system()).lower()
    if name == "darwin":
        return "macOS"
    if name == "linux":
        return "Linux"
    if name == "windows" or name.startswith(("cygwin", "msys")):
        return "Windows"
    return "other"


def environment() -> dict[str, str]:
    return {
        "lcc_version": __version__,
        "os_family": os_family(),
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
    }


def round_tokens(value: float) -> int:
    """Nearest 100, half up (deterministic, unlike round()'s banker's rounding)."""
    return int(math.floor(value / TOKEN_ROUNDING + 0.5)) * TOKEN_ROUNDING


def round_ratio(value: float) -> float:
    return math.floor(value * 100 + 0.5) / 100


def percentile(sorted_values: list[float], q: float) -> float | None:
    """Linear interpolation between closest ranks (numpy's default)."""
    if not sorted_values:
        return None
    position = q * (len(sorted_values) - 1)
    low = math.floor(position)
    high = min(low + 1, len(sorted_values) - 1)
    weight = position - low
    return sorted_values[low] * (1 - weight) + sorted_values[high] * weight


def parse_since(value: str | None) -> date | None:
    if value is None:
        return None
    if not _DAY.fullmatch(value):
        raise ValueError(f"--since must be YYYY-MM-DD, got {value!r}")
    return date.fromisoformat(value)


@dataclass(frozen=True)
class Pass:
    """One compaction pass, reduced to the fields the summary may use."""

    mode: str
    provider: str
    degraded: bool
    fallback: bool
    tokens_before: int
    tokens_after: int
    reduction_ratio: float
    stashed: int = 0


@dataclass
class Inputs:
    passes: list[Pass] = field(default_factory=list)
    log_entries: list[dict[str, Any]] = field(default_factory=list)
    reports_read: int = 0
    skipped: Counter[str] = field(default_factory=Counter)
    log_requested: bool = False
    log_found: bool = False
    log_lines_skipped: int = 0


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return float(value)


def parse_report(payload: Any) -> Pass | str:
    """Return a Pass, or the reason the payload was skipped (a fixed label)."""
    if not isinstance(payload, dict):
        return "not_a_report"
    schema = payload.get("schema_version")
    if not isinstance(schema, str):
        return "unsupported_format"
    if schema.startswith(_RELEVANCE_PREFIX):
        mode = "blocks"
    elif schema.startswith(_TRANSCRIPT_PREFIX):
        mode = _label(payload.get("mode", "tool-calls"), _MODES)
    else:
        return "unsupported_format"
    before = _number(payload.get("tokens_before"))
    after = _number(payload.get("tokens_after"))
    ratio = _number(payload.get("reduction_ratio"))
    if before is None or after is None:
        return "invalid_fields"
    if ratio is None:
        ratio = 0.0 if before == 0 else max(0.0, 1.0 - after / before)
    if ratio > 1:
        return "invalid_fields"
    provider = _label(payload.get("provider_used"), _PROVIDERS)
    raw_provider = payload.get("provider_used")
    fallback = isinstance(raw_provider, str) and (
        raw_provider == "degraded" or raw_provider.endswith(_PROVIDER_SUFFIXES)
    )
    stashed = _number(payload.get("tool_calls_stashed")) or 0
    return Pass(
        mode=mode,
        provider=provider,
        degraded=payload.get("degraded") is True,
        fallback=fallback,
        tokens_before=int(before),
        tokens_after=int(after),
        reduction_ratio=ratio,
        stashed=int(stashed),
    )


def load_inputs(
    report_paths: list[Path],
    log_file: Path | None,
    since: date | None = None,
    warn: Any = None,
) -> Inputs:
    """Read reports and the request log. Never raises on bad input; never echoes contents.

    ``warn`` receives short messages naming the input position and a fixed reason only.
    """
    inputs = Inputs()
    seen: set[Path] = set()
    for position, path in enumerate(report_paths, start=1):
        try:
            key = path.resolve()
        except OSError:
            key = path
        if key in seen:
            continue
        seen.add(key)
        reason: str | None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            reason = "unreadable"
        except (json.JSONDecodeError, RecursionError):
            reason = "not_json"
        else:
            parsed = parse_report(payload)
            if isinstance(parsed, Pass):
                inputs.passes.append(parsed)
                inputs.reports_read += 1
                reason = None
            else:
                reason = parsed
        if reason is not None:
            inputs.skipped[reason] += 1
            if warn is not None:
                warn(f"skipped report input {position} of {len(report_paths)}: {reason}")
    if log_file is not None:
        inputs.log_requested = True
        _load_log(inputs, log_file, since, warn)
    return inputs


def _load_log(inputs: Inputs, log_file: Path, since: date | None, warn: Any) -> None:
    try:
        text = log_file.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return
    except OSError:
        inputs.skipped["request_log_unreadable"] += 1
        if warn is not None:
            warn("skipped the request log: unreadable")
        return
    inputs.log_found = True
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, RecursionError):
            entry = None
        if not isinstance(entry, dict):
            inputs.log_lines_skipped += 1
            continue
        if since is not None:
            day = entry.get("date")
            if not (isinstance(day, str) and _DAY.fullmatch(day)) or day < since.isoformat():
                continue
        inputs.log_entries.append(entry)
    if inputs.log_lines_skipped and warn is not None:
        warn(f"skipped {inputs.log_lines_skipped} unreadable request-log line(s)")


def _counts(values: list[str]) -> dict[str, int]:
    return dict(sorted(Counter(values).items()))


def _report_section(passes: list[Pass]) -> dict[str, Any]:
    ratios = sorted(round_ratio(p.reduction_ratio) for p in passes)
    raw = sorted(p.reduction_ratio for p in passes)

    def pct(q: float) -> float | None:
        value = percentile(raw, q)
        return None if value is None else round_ratio(value)

    return {
        "passes": len(passes),
        "by_mode": _counts([p.mode for p in passes]),
        "by_provider": _counts([p.provider for p in passes]),
        "degraded_passes": sum(p.degraded for p in passes),
        "fallback_passes": sum(p.fallback for p in passes),
        "under_25pct_gate": sum(p.reduction_ratio < GATE for p in passes),
        "tokens_before_total": round_tokens(sum(p.tokens_before for p in passes)),
        "tokens_after_total": round_tokens(sum(p.tokens_after for p in passes)),
        "reduction_ratio": {"p10": pct(0.10), "median": pct(0.50), "p90": pct(0.90)},
        "per_pass_reduction_ratios": ratios if len(passes) >= MIN_PASSES_FOR_LIST else None,
        "stashed_outputs_total": sum(p.stashed for p in passes),
    }


def _log_section(inputs: Inputs) -> dict[str, Any]:
    entries = inputs.log_entries
    tools = [_label(e.get("tool"), _TOOLS) for e in entries]
    with_provider = [e for e in entries if "provider" in e]
    idle = [e for e in entries if e.get("trigger") == "idle"]
    removed = sum(_number(e.get("removed_tokens")) or 0 for e in idle)
    days = {
        e["date"] for e in entries if isinstance(e.get("date"), str) and _DAY.fullmatch(e["date"])
    }
    versions = [
        e["lcc_version"]
        if isinstance(e.get("lcc_version"), str) and _VERSION.fullmatch(e["lcc_version"])
        else "other"
        for e in entries
    ]
    recalls = sum(tool in _RECALL_TOOLS for tool in tools)
    return {
        "read": inputs.log_requested,
        "found": inputs.log_found,
        "entries": len(entries),
        "active_days": len(days),
        "by_tool": _counts(tools),
        "by_provider": _counts([_label(e.get("provider"), _PROVIDERS) for e in with_provider]),
        "degraded_passes": sum(
            e.get("degradation_reason") not in (None, "none") for e in with_provider
        ),
        # lcc 1.2.0 does not log recall calls; 0 would claim more than the log knows.
        "recall_calls": recalls or None,
        "idle_stash_passes": len(idle),
        "idle_removed_tokens_total": round_tokens(removed),
        "idle_by_gap_bucket": _counts([_label(e.get("gap_bucket"), _GAP_BUCKETS) for e in idle]),
        "by_lcc_version": _counts(versions),
    }


def build_summary(
    inputs: Inputs, since: date | None = None, env: dict[str, str] | None = None
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "tool_version": __version__,
        "environment": env if env is not None else environment(),
        "inputs": {
            "report_files_read": inputs.reports_read,
            "inputs_skipped": sum(inputs.skipped.values()) + inputs.log_lines_skipped,
            "skipped_by_reason": dict(
                sorted(
                    (
                        inputs.skipped + Counter({"request_log_line": inputs.log_lines_skipped})
                    ).items()
                )
            ),
            "since": since.isoformat() if since else None,
        },
        "reports": _report_section(inputs.passes),
        "request_log": _log_section(inputs),
    }


def to_json(summary: dict[str, Any]) -> str:
    return json.dumps(summary, indent=2, sort_keys=True) + "\n"


def _fmt(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, dict):
        return ", ".join(f"{k} {v}" for k, v in value.items()) or "none"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


HEADER = (
    "Included: counts, token totals rounded to the nearest 100, reduction ratios rounded to "
    "2 decimals, lcc version, OS family and Python version.\n"
    "Not included: session text, objectives or goals, file paths or names, commands, ids, "
    "hashes, usernames, hostnames, or any date finer than a day. Nothing was sent anywhere; "
    "read it before you paste it."
)


def to_markdown(summary: dict[str, Any]) -> str:
    env = summary["environment"]
    meta = summary["inputs"]
    rep = summary["reports"]
    log = summary["request_log"]
    ratio = rep["reduction_ratio"]
    lines = [
        f"## lcc share-stats ({summary['schema_version']})",
        "",
        *(f"> {line}" for line in HEADER.splitlines()),
        "",
        f"- lcc {env['lcc_version']} · {env['os_family']} · Python {env['python']}",
        f"- Since: {meta['since'] or 'all time'} (applies to the request log; reports carry "
        "no dates)",
        f"- Report files read: {meta['report_files_read']} · inputs skipped: "
        f"{meta['inputs_skipped']}",
        "",
        "### Compaction reports",
        "",
        "| metric | value |",
        "| --- | --- |",
        f"| passes | {rep['passes']} |",
        f"| by mode | {_fmt(rep['by_mode'])} |",
        f"| by provider | {_fmt(rep['by_provider'])} |",
        f"| degraded passes | {rep['degraded_passes']} |",
        f"| fallback passes | {rep['fallback_passes']} |",
        f"| passes under the 25% gate | {rep['under_25pct_gate']} |",
        f"| tokens before (total, ~100) | {rep['tokens_before_total']} |",
        f"| tokens after (total, ~100) | {rep['tokens_after_total']} |",
        f"| reduction ratio p10 / median / p90 | {_fmt(ratio['p10'])} / "
        f"{_fmt(ratio['median'])} / {_fmt(ratio['p90'])} |",
        "| per-pass reduction ratios | "
        + (
            ", ".join(f"{r:.2f}" for r in rep["per_pass_reduction_ratios"])
            if rep["per_pass_reduction_ratios"] is not None
            else f"left out (fewer than {MIN_PASSES_FOR_LIST} passes)"
        )
        + " |",
        f"| stashed tool outputs | {rep['stashed_outputs_total']} |",
        "",
        "### Request log (LCC_REQUEST_LOG=1)",
        "",
    ]
    if not log["read"]:
        lines.append("Not read (--no-request-log).")
    elif not log["found"]:
        lines.append("Not found (the log is off unless LCC_REQUEST_LOG=1).")
    else:
        lines += [
            "| metric | value |",
            "| --- | --- |",
            f"| entries | {log['entries']} |",
            f"| active days | {log['active_days']} |",
            f"| by entry point | {_fmt(log['by_tool'])} |",
            f"| by provider | {_fmt(log['by_provider'])} |",
            f"| degraded passes | {log['degraded_passes']} |",
            f"| recall calls | {_fmt(log['recall_calls'])} "
            + ("(not recorded by the log) |" if log["recall_calls"] is None else "|"),
            f"| idle-stash passes | {log['idle_stash_passes']} |",
            f"| idle-stash removed tokens (total, ~100) | {log['idle_removed_tokens_total']} |",
            f"| idle gap buckets | {_fmt(log['idle_by_gap_bucket'])} |",
            f"| lcc versions | {_fmt(log['by_lcc_version'])} |",
        ]
    lines += [
        "",
        f'Paste this into a new "{ISSUE_TEMPLATE}" issue: '
        "https://github.com/lucasmartins-ai/lcc/issues/new/choose",
    ]
    return "\n".join(lines) + "\n"
