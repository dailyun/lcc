"""The prompt-cache contract (docs/CACHE_ALIGNMENT.md, "Invariants and tests").

I1 prefix stability within an epoch, I2 deterministic placeholders, I3 no non-deterministic
fields in emitted text, I4 append never rewrites earlier output unless the report opens an
epoch. Plus the deterministic cost model behind ``cache_effect``.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import math
import os
import subprocess
import sys
from pathlib import Path

import pytest

from lcc.mcp_server import handle_message
from lcc.relevance import RelevanceCompactionRequest, compact_context
from lcc.relevance.cache_contract import (
    CacheFactors,
    cache_state_from_idle,
    common_prefix_chars,
    estimate_cache_effect,
    prefix_stability,
)
from lcc.relevance.compactor import report_to_dict
from lcc.relevance.transcript import (
    TranscriptCompactionRequest,
    TranscriptError,
    compact_transcript,
    messages_to_payload,
)

ROOT = Path(__file__).resolve().parents[1]
OBJECTIVE = "Why did checkout conversion fall?"
RELEVANT = "Turn {n}: checkout conversion fell on mobile after the release, funnel cohort data."
NOISE = "Turn {n}: " + "office plants coffee weather parking lunch printer music " * 5


def _bench():
    path = ROOT / "benchmarks" / "cache" / "run_prefix_stability.py"
    spec = importlib.util.spec_from_file_location("_prefix_stability", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _offline(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("LCC_DISABLE_NETWORK", "1")
    monkeypatch.setenv("LCC_STASH_DIR", str(tmp_path / "stash"))


def _pass(text: str, cache: Path | None, **over):
    return compact_context(
        RelevanceCompactionRequest(
            text=text, question=OBJECTIVE, provider="mechanical", decisions_cache_path=cache, **over
        )
    )


# --- cache state from an idle gap: the edges ---------------------------------------------------


@pytest.mark.parametrize(
    ("idle", "ttl", "expected"),
    [
        (3600, 3600, ("cold", "idle_past_ttl")),  # exactly at the TTL: expired
        (3599.999, 3600, ("warm", "idle_within_ttl")),
        (3600.001, 3600, ("cold", "idle_past_ttl")),
        (0, 3600, ("warm", "idle_within_ttl")),
        (-5, 3600, ("warm", "clock_skew")),  # never cold from a clock that went backwards
        (-1e12, 3600, ("warm", "clock_skew")),
        (math.inf, 3600, ("cold", "idle_past_ttl")),  # very long idle
        (1e15, 7200, ("cold", "idle_past_ttl")),
        (None, 3600, ("unknown", "missing_timestamp")),
        (math.nan, 3600, ("unknown", "missing_timestamp")),
        ("soon", 3600, ("unknown", "missing_timestamp")),
        (True, 3600, ("unknown", "missing_timestamp")),
        (10, 0, ("unknown", "invalid_ttl")),
        (10, -1, ("unknown", "invalid_ttl")),
        (10, None, ("unknown", "invalid_ttl")),
        (10, math.nan, ("unknown", "invalid_ttl")),
    ],
)
def test_cache_state_from_idle_edges(idle, ttl, expected):
    assert cache_state_from_idle(idle, ttl) == expected


# --- cost model ----------------------------------------------------------------------------------


def _effect(state: str, *, removed: int, stable_after: int, total: int = 40_000, **kw):
    """A 40k-token context; the pass keeps ``stable_after`` tokens of prefix and removes ``removed``."""
    return estimate_cache_effect(
        baseline_tokens=total,
        no_pass_tokens=total,
        no_pass_stable_tokens=total,
        pass_tokens=total - removed,
        pass_stable_tokens=stable_after,
        cache_state=state,
        **kw,
    )


def test_warm_cache_small_removal_is_not_recommended():
    effect = _effect("warm", removed=400, stable_after=8_000)
    assert not effect.recommended
    assert effect.reason == "warm_cache_invalidation_exceeds_saving"
    assert effect.estimated_relative_cost_delta > 5  # the docs' "~90x" shape: far worse
    assert effect.invalidated_prefix_tokens == 32_000
    assert effect.epoch_opened


def test_unknown_cache_is_priced_as_warm():
    unknown = _effect("unknown", removed=400, stable_after=8_000)
    warm = _effect("warm", removed=400, stable_after=8_000)
    assert unknown.assumed_state == "warm"
    assert unknown.to_dict()["estimated_cost_pass"] == warm.to_dict()["estimated_cost_pass"]
    assert not unknown.recommended


def test_cold_cache_removal_at_threshold_is_recommended():
    effect = _effect("cold", removed=10_000, stable_after=0, min_removed_tokens=10_000)
    assert effect.recommended
    assert effect.reason == "saves_cost_cold_cache"
    assert effect.estimated_relative_cost_delta == pytest.approx(-0.25)
    below = _effect("cold", removed=9_999, stable_after=0, min_removed_tokens=10_000)
    assert not below.recommended
    assert below.reason == "below_min_removed_tokens"


def test_warm_cache_pays_off_with_enough_reuses_or_tail_only_mutation():
    # A tail-only pass (prefix intact) is a free win even on a warm cache.
    assert _effect("warm", removed=400, stable_after=39_600).recommended
    # A mid-prefix pass needs reuses; the break-even sits between 1 and 200 reuses here.
    assert not _effect("warm", removed=4_000, stable_after=30_000, expected_reuses=1).recommended
    assert _effect("warm", removed=4_000, stable_after=30_000, expected_reuses=200).recommended


def test_nothing_removed_is_never_recommended():
    for state in ("warm", "cold", "unknown"):
        effect = _effect(state, removed=0, stable_after=40_000)
        assert not effect.recommended and effect.reason == "nothing_removed"
        assert not effect.epoch_opened
    grown = _effect("cold", removed=-50, stable_after=0)  # markers made it longer
    assert not grown.recommended


def test_cost_model_is_deterministic_and_json_stable():
    a = _effect("warm", removed=1234, stable_after=777, expected_reuses=3).to_dict()
    b = _effect("warm", removed=1234, stable_after=777, expected_reuses=3).to_dict()
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    zero = _effect("warm", removed=0, stable_after=40_000).to_dict()
    assert "-0.0" not in json.dumps(zero)
    assert estimate_cache_effect(
        baseline_tokens=0, no_pass_tokens=0, no_pass_stable_tokens=0, pass_tokens=0,
        pass_stable_tokens=0,
    ).estimated_relative_cost_delta == 0.0


@pytest.mark.parametrize(
    "factors",
    [{"read": -0.1}, {"write": math.nan}, {"input": 0}, {"read": math.inf}, {"write": "1.25"}],
)
def test_cache_factors_are_validated(factors):
    with pytest.raises(ValueError):
        CacheFactors(**factors)


def test_invalid_cache_state_is_refused():
    with pytest.raises(ValueError):
        _effect("lukewarm", removed=1, stable_after=0)
    with pytest.raises(ValueError):
        _pass("x" * 400, None, cache_state="lukewarm")
    with pytest.raises(TranscriptError):
        compact_transcript(
            TranscriptCompactionRequest(
                payload={"messages": [{"role": "user", "text": "hi"}]},
                question="hi", provider="rules", cache_state="hot",
            )
        )


def test_common_prefix_chunking():
    a = "x" * 10_000 + "A" + "y" * 5
    b = "x" * 10_000 + "B"
    assert common_prefix_chars(a, b) == 10_000
    assert common_prefix_chars(a, a) == len(a)
    assert common_prefix_chars("", a) == 0
    assert prefix_stability("abc", "abcdef") == {
        "previous_chars": 3, "current_chars": 6, "stable_prefix_chars": 3,
        "append_only": True, "share": 1.0,
    }


# --- I4: a dropped run that grows at the tail does not rewrite its marker -------------------------


def test_sticky_tail_run_growing_keeps_earlier_marker(tmp_path: Path):
    """Regression: the run marker counted the whole run, so a tail run that grew on the next
    pass rewrote '[dropped 1 block]' as '[dropped 2 blocks]' inside the cached prefix."""
    cache = tmp_path / "d.jsonl"
    text = "System contract.\n\n" + RELEVANT.format(n=1) + "\n\n" + NOISE.format(n=2) + "\n"
    first = _pass(text, cache)
    assert "[lcc-compact: dropped 1 block" in first.compacted_text
    grown = text + "\n" + NOISE.format(n=3) + "\n\n" + RELEVANT.format(n=4) + "\n"
    second = _pass(grown, cache, previous_compacted_text=first.compacted_text)
    assert second.compacted_text.startswith(first.compacted_text)
    effect = second.report.cache_effect
    assert effect is not None
    assert effect["baseline"] == "previous_output"
    assert effect["epoch_opened"] is False
    assert effect["invalidated_prefix_tokens"] == 0


def test_without_decisions_cache_runs_still_merge(tmp_path: Path):
    # Back-compat: one-shot passes keep one marker per run.
    text = RELEVANT.format(n=1) + "\n\n" + NOISE.format(n=2) + "\n\n" + NOISE.format(n=3) + "\n"
    out = _pass(text, None).compacted_text
    assert out.count("[lcc-compact: dropped") == 1
    assert "dropped 2 blocks" in out
    per_block = _pass(text, None, merge_dropped_runs=False).compacted_text
    assert per_block.count("[lcc-compact: dropped 1 block") == 2


# --- I1: a sufficiency restoration that flips a sticky decision is reported and ratcheted ----------


def _flip_session():
    """Seed 1 of the benchmark: turn 2 links a new block to an earlier sticky drop."""
    bench = _bench()
    import random

    rng = random.Random(1)
    text = "System contract: answer questions about checkout conversion.\n"
    texts = []
    index = 0
    for _ in range(4):
        for _ in range(rng.randint(1, 3)):
            index += 1
            text += "\n" + bench._dossier_block(rng, index) + "\n"
        texts.append(text)
    return bench, texts


def test_restoration_flip_opens_a_reported_epoch_once(tmp_path: Path):
    bench = _bench()
    flips = [r for seed in range(12) for r in bench.simulate_dossier(seed, 12) if r["sticky_flips"]]
    assert flips, "the fixture must exercise a sticky flip"
    # Every flip is reported, and every prefix break is a reported flip (I1/I4).
    records = [r for seed in range(6) for r in bench.simulate_dossier(seed, 12)]
    assert all(r["append_only"] or (r["epoch_opened"] and r["sticky_flips"]) for r in records)


def test_ratchet_persists_restoration_and_counts_it(tmp_path: Path):
    bench = _bench()
    seed = next(
        seed for seed in range(40) if any(r["sticky_flips"] for r in bench.simulate_dossier(seed, 12))
    )
    import random

    rng = random.Random(seed)
    cache = tmp_path / "d.jsonl"
    text = "System contract: answer questions about checkout conversion.\n"
    previous = None
    index = 0
    flipped_at = None
    for turn in range(12):
        for _ in range(rng.randint(1, 3)):
            index += 1
            text += "\n" + bench._dossier_block(rng, index) + "\n"
        result = _pass(text, cache, previous_compacted_text=previous)
        report = report_to_dict(result.report)
        if report["cache_effect"]["sticky_flips"] and flipped_at is None:
            flipped_at = turn
            assert any(w.startswith("cache_epoch_opened:") for w in report["warnings"])
            assert report["cache_effect"]["epoch_opened"] is True
            # Same input again: the persisted keep reproduces the bytes, no new flip,
            # and the restored block still counts as restored.
            again = _pass(text, cache, previous_compacted_text=result.compacted_text)
            assert again.compacted_text == result.compacted_text
            assert again.report.cache_effect["sticky_flips"] == []
            assert again.report.cache_effect["epoch_opened"] is False
            assert again.report.blocks_restored >= len(report["cache_effect"]["sticky_flips"])
        previous = result.compacted_text
    assert flipped_at is not None
    records = [json.loads(line) for line in cache.read_text().splitlines()]
    assert any(r["provider"] == "ratchet" and r["decision"] == "keep" for r in records)


# --- I2 / I3: deterministic placeholders and emitted bytes ------------------------------------


def test_rerun_reproduces_identical_bytes_without_cache():
    text = "".join(
        (RELEVANT if n % 3 == 0 else NOISE).format(n=n) + "\n\n" for n in range(1, 20)
    )
    outs = {_pass(text, None).compacted_text for _ in range(3)}
    assert len(outs) == 1


_DETERMINISM_SCRIPT = """
import hashlib, json, os, sys
os.environ["LCC_DISABLE_NETWORK"] = "1"
from lcc.relevance import RelevanceCompactionRequest, compact_context
from lcc.relevance.transcript import TranscriptCompactionRequest, compact_transcript, messages_to_payload
text = sys.stdin.read()
payload = json.loads(text.split("\\x00")[1])
r = compact_context(RelevanceCompactionRequest(text=text.split("\\x00")[0], question="Why did checkout conversion fall?", provider="mechanical", trim_head_chars=120))
t = compact_transcript(TranscriptCompactionRequest(payload=payload, question="fix the parser", provider="rules", rules_mode="stashed", preserve_recent=1))
emitted = r.compacted_text + json.dumps(messages_to_payload(t.messages), ensure_ascii=False)
print(hashlib.sha256(emitted.encode()).hexdigest(), json.dumps(r.report.cache_effect, sort_keys=True))
"""


def test_emitted_bytes_ignore_hash_seed_locale_and_timezone(tmp_path: Path):
    """I3: no dict-order, locale, timezone or clock leaks into the compacted context."""
    text = "".join((RELEVANT if n % 4 == 0 else NOISE).format(n=n) + "\n\n" for n in range(1, 30))
    body = "\n".join(f"line {n} of the old parser module" for n in range(1, 200))
    payload = {
        "messages": [
            {"role": "user", "text": "fix the parser"},
            {"role": "assistant", "text": "s", "toolUses": [{"id": "c1", "tool": "Read", "input": {"file_path": "/r/old.py", "b": 1, "a": 2}}]},
            {"role": "user", "text": "", "toolResults": [{"id": "c1", "text": body}]},
            {"role": "assistant", "text": "s", "toolUses": [{"id": "c2", "tool": "Read", "input": {"file_path": "/r/old.py"}}]},
            {"role": "user", "text": "", "toolResults": [{"id": "c2", "text": body + "\nchanged"}]},
            {"role": "assistant", "text": "done"},
        ]
    }
    stdin = text + "\x00" + json.dumps(payload)
    outputs = set()
    for env in (
        {"PYTHONHASHSEED": "0", "LC_ALL": "C", "TZ": "UTC"},
        {"PYTHONHASHSEED": "12345", "LC_ALL": "C.UTF-8", "TZ": "Asia/Kathmandu"},
        {"PYTHONHASHSEED": "random", "LANG": "de_DE.UTF-8", "TZ": "America/St_Johns"},
    ):
        run = subprocess.run(
            [sys.executable, "-c", _DETERMINISM_SCRIPT],
            input=stdin, capture_output=True, text=True, check=True,
            env={
                **os.environ,
                "PYTHONPATH": str(ROOT / "src"),
                "LCC_STASH_DIR": str(tmp_path / "stash"),
                **env,
            },
        )
        outputs.add(run.stdout.strip())
    assert len(outputs) == 1, outputs


# --- stash placeholders are fixed points (regression) -------------------------------------------


def _stale_session(lines: int, tail_lines: int = 0) -> list[dict]:
    def body(name: str) -> str:
        return "\n".join(f"line {n} of the {name} parser module" for n in range(1, lines))

    messages: list[dict] = [{"role": "user", "text": "fix the parser"}]
    calls = [
        ("Read", {"file_path": "/repo/src/old.py"}, body("old")),
        ("Read", {"file_path": "/repo/src/other.py"}, body("other")),
        ("Read", {"file_path": "/repo/src/missing.py"},
         "<tool_use_error>File does not exist: /repo/src/missing.py and more words</tool_use_error>"),
    ]
    for n, (tool, tool_input, output) in enumerate(calls, start=1):
        messages.append({"role": "assistant", "text": f"step {n}", "toolUses": [{"id": f"c{n}", "tool": tool, "input": tool_input}]})
        messages.append({"role": "user", "text": "", "toolResults": [{"id": f"c{n}", "text": output}]})
    # Text is never touched: a long final answer is a large kept tail after the stashed reads.
    tail = "\n".join(f"note {n}: the parser fix is verified" for n in range(tail_lines))
    messages.append({"role": "assistant", "text": "done" + ("\n" + tail if tail else "")})
    return messages


def _stash(messages, **over):
    result = compact_transcript(
        TranscriptCompactionRequest(
            payload={"messages": copy.deepcopy(messages)}, question="fix the parser",
            provider="rules", preserve_recent=1, rules_mode="stashed", **over,
        )
    )
    return messages_to_payload(result.messages), result


@pytest.mark.parametrize("lines", [80, 400, 3000])
def test_second_stash_pass_keeps_existing_placeholders(lines: int):
    """Regression: a stash pointer was itself stashed again when its own token count had fewer
    digits than the original ('[removed: 693 tokens · ...]' became '[removed: 11 tokens · ...]'),
    rewriting early history for nothing and making recall return the pointer, not the output."""
    first, r1 = _stash(_stale_session(lines))
    assert r1.report["tool_calls_stashed"] >= 1
    second, r2 = _stash(first)
    assert second == first
    assert r2.report["tool_calls_stashed"] == 0
    assert "already_stashed" in {d["reason"] for d in r2.decisions}
    assert r2.report["cache_effect"]["epoch_opened"] is False
    assert r2.report["cache_effect"]["recommended"] is False


def test_stash_report_cache_effect_cold_vs_warm():
    _, cold = _stash(_stale_session(400), cache_state="cold")
    effect = cold.report["cache_effect"]
    assert effect["cache_state"] == "cold" and effect["recommended"] is True
    assert effect["removed_tokens"] == cold.report["tokens_before"] - cold.report["tokens_after"]
    # The stash rewrites history from message 2: on a warm cache everything after it is
    # written again. Removing ~99% of the tokens still pays; a small early removal ahead of
    # a large kept tail does not, while the same pass on a cold cache does.
    _, big = _stash(_stale_session(400), cache_state="warm")
    assert big.report["cache_effect"]["recommended"] is True
    _, warm = _stash(_stale_session(80, tail_lines=3000), cache_state="warm")
    effect = warm.report["cache_effect"]
    assert warm.report["tool_calls_stashed"] >= 1
    assert effect["invalidated_prefix_tokens"] > effect["removed_tokens"] > 0
    assert effect["recommended"] is False
    assert effect["reason"] == "warm_cache_invalidation_exceeds_saving"
    _, cold_tail = _stash(_stale_session(80, tail_lines=3000), cache_state="cold")
    assert cold_tail.report["cache_effect"]["recommended"] is True


def test_mcp_compact_transcript_passes_cache_state():
    def call(state):
        return handle_message(
            {
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {
                    "name": "compact_transcript",
                    "arguments": {
                        "messages": _stale_session(400), "question": "fix the parser",
                        "provider": "rules", "rules_mode": "stashed", "preserve_recent": 1,
                        "cache_state": state, "ref_unchanged": True,
                    },
                },
            }
        )

    ok = json.loads(call("cold")["result"]["content"][0]["text"])
    assert ok["report"]["cache_effect"]["cache_state"] == "cold"
    bad = call("tepid")["result"]
    assert bad["isError"] is True


# --- property: seeded sessions growing turn by turn ---------------------------------------------


def test_prefix_stability_property_over_seeded_sessions():
    bench = _bench()
    records = [r for seed in range(20) for r in bench.simulate_dossier(seed, 10)]
    assert len(records) == 180
    for record in records:
        # I1/I4: append-only, or an epoch the report opened and named.
        assert record["append_only"] or (record["epoch_opened"] and record["sticky_flips"]), record
    share = sum(r["share"] for r in records) / len(records)
    assert share > 0.9


def test_stash_idempotence_property_over_seeded_sessions():
    bench = _bench()
    records = [r for seed in range(6) for r in bench.simulate_transcript(seed, 4)]
    assert sum(r["stashed"] for r in records) > 0
    assert all(r["idempotent"] for r in records)
    assert all(r.get("placeholder_rewrites", 0) == 0 for r in records)


def test_benchmark_check_mode_passes_and_is_deterministic(tmp_path: Path):
    bench = _bench()
    a = bench.run(seeds=3, turns=6, rounds=3)
    b = bench.run(seeds=3, turns=6, rounds=3)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    assert a["dossier"]["unexplained_prefix_breaks"] == 0
    assert a["transcript_stashed"]["idempotence_violations"] == 0
    assert hashlib.sha256(json.dumps(a, sort_keys=True).encode()).hexdigest()
    assert bench.main(["--check", "--seeds", "6", "--turns", "8", "--rounds", "2", "--out", str(tmp_path)]) == 0
    assert (tmp_path / "prefix_stability.json").is_file()


def test_cli_cache_flags(tmp_path: Path):
    from typer.testing import CliRunner

    from lcc.cli import app

    runner = CliRunner()
    text = "".join((RELEVANT if n % 3 == 0 else NOISE).format(n=n) + "\n\n" for n in range(1, 12))
    src = tmp_path / "in.md"
    src.write_text(text, encoding="utf-8")
    previous = tmp_path / "prev.md"
    previous.write_text(_pass(text, None).compacted_text, encoding="utf-8")
    report = tmp_path / "r.json"
    base = ["compact", str(src), "--question", OBJECTIVE, "--provider", "mechanical"]
    result = runner.invoke(
        app,
        [*base, "--output", str(tmp_path / "o.md"), "--report", str(report),
         "--cache-state", "cold", "--expected-reuses", "3", "--previous-output", str(previous),
         "--cache-read-factor", "0.1", "--cache-write-factor", "2"],
    )
    assert result.exit_code == 0, result.output
    effect = json.loads(report.read_text(encoding="utf-8"))["cache_effect"]
    assert effect["cache_state"] == "cold"
    assert effect["baseline"] == "previous_output"
    assert effect["expected_reuses"] == 3
    assert effect["factors"]["write"] == 2.0
    assert effect["epoch_opened"] is False  # same input, same output: the cache stays valid
    assert runner.invoke(app, [*base, "--cache-state", "hot"]).exit_code == 2
    assert runner.invoke(app, [*base, "--cache-read-factor", "-1"]).exit_code == 2
    assert runner.invoke(app, [*base, "--previous-output", str(tmp_path / "nope.md")]).exit_code == 2
    tool_calls = runner.invoke(app, [*base, "--mode", "tool-calls", "--cache-state", "cold"])
    assert tool_calls.exit_code != 0
