"""The prompt-cache contract of a compaction pass, as code (docs/CACHE_ALIGNMENT.md).

Providers cache prompt prefixes: a request reuses the cached computation for every byte up
to the first changed one, and everything after it is written again. A pass that rewrites
early history can therefore cost more than it saves. This module holds the pure,
deterministic pieces of that contract:

* :func:`common_prefix_chars` / :func:`prefix_stability`: how much of a previous pass's
  output a new output keeps byte-identical (the part a warm cache can still serve);
* :func:`cache_state_from_idle`: warm / cold / unknown from an idle gap and a TTL, with the
  edge cases spelled out (exact TTL is cold, negative gaps are clock skew and count as warm,
  missing timestamps are unknown);
* :func:`estimate_cache_effect`: the relative cost of sending the compacted context against
  sending the uncompacted one, under configurable read/write/input price multipliers, and
  whether the pass is recommended.

No prices are hardcoded: the factors are *relative* multipliers of the base input-token
price (defaults: read 0.10x, write 1.25x, the figures the docs use). Everything returned is
rounded to fixed decimals so the report stays byte-stable across platforms.

Invariants the test suite holds the compactors to (``tests/test_cache_contract.py``):

I1  Prefix stability. Within one cache epoch (same objective, same policy, sticky decisions
    on), a pass over an append-only input emits the previous pass's output as a byte-exact
    prefix. A pass that cannot (a sufficiency restoration flips an earlier sticky drop, a
    new neighbour re-keys a block) says so: ``cache_effect.epoch_opened`` and
    ``cache_effect.sticky_flips`` in the report.
I2  Deterministic placeholders. Same removed content gives the same marker / stash pointer
    text and id, so re-running a pass reproduces the identical prefix; a pass never
    replaces its own placeholder (a second stash pass over its output is a no-op).
I3  No non-deterministic fields in the emitted context: no timestamps, random ids, dict
    order, locale or float formatting drift. Scores only appear inline when requested, and
    then as fixed-decimal buckets.
I4  Appending never rewrites earlier output unless an epoch is opened deliberately and
    reported (I1); dropped runs are not merged across passes in sticky mode, so a run that
    grows at the tail does not rewrite its earlier marker.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

CACHE_CONTRACT_VERSION = "cache-contract-1.0"

#: Relative price multipliers (x base input-token price) from docs/CACHE_ALIGNMENT.md.
DEFAULT_READ_FACTOR = 0.10
DEFAULT_WRITE_FACTOR = 1.25
DEFAULT_INPUT_FACTOR = 1.0

CACHE_STATES = ("warm", "cold", "unknown")

__all__ = [
    "CACHE_CONTRACT_VERSION",
    "CACHE_STATES",
    "DEFAULT_INPUT_FACTOR",
    "DEFAULT_READ_FACTOR",
    "DEFAULT_WRITE_FACTOR",
    "CacheEffect",
    "CacheFactors",
    "cache_state_from_idle",
    "common_prefix_chars",
    "estimate_cache_effect",
    "prefix_stability",
]


@dataclass(frozen=True)
class CacheFactors:
    """Relative price multipliers; validated so a typo can never yield a NaN decision."""

    read: float = DEFAULT_READ_FACTOR
    write: float = DEFAULT_WRITE_FACTOR
    input: float = DEFAULT_INPUT_FACTOR

    def __post_init__(self) -> None:
        for name in ("read", "write", "input"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"cache factor {name!r} must be a number, got {value!r}")
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"cache factor {name!r} must be finite and >= 0, got {value!r}")
        if self.input <= 0:
            raise ValueError("cache factor 'input' must be > 0 (it normalises every cost)")

    def to_dict(self) -> dict[str, float]:
        return {"read": _r(self.read), "write": _r(self.write), "input": _r(self.input)}


def _r(value: float, places: int = 6) -> float:
    """Fixed-decimal rounding; ``-0.0`` is folded to ``0.0`` so JSON never prints ``-0.0``."""
    rounded = round(float(value), places)
    return 0.0 if rounded == 0 else rounded


def common_prefix_chars(a: str, b: str) -> int:
    """Length of the longest common prefix of ``a`` and ``b`` (chunked; linear time)."""
    limit = min(len(a), len(b))
    step = 4096
    start = 0
    while start < limit:
        end = min(limit, start + step)
        if a[start:end] != b[start:end]:
            for index in range(start, end):
                if a[index] != b[index]:
                    return index
        start = end
    return limit


def prefix_stability(previous: str, current: str) -> dict[str, Any]:
    """How much of ``previous`` survives byte-identical at the head of ``current``.

    ``append_only`` is the I1/I4 guarantee: the whole previous output is a prefix of the new
    one. ``share`` is the stable fraction of ``previous`` (1.0 when previous is empty).
    """
    stable = common_prefix_chars(previous, current)
    return {
        "previous_chars": len(previous),
        "current_chars": len(current),
        "stable_prefix_chars": stable,
        "append_only": stable == len(previous),
        "share": _r(stable / len(previous), 6) if previous else 1.0,
    }


def cache_state_from_idle(idle_seconds: float | None, ttl_seconds: float | None) -> tuple[str, str]:
    """``(state, reason)`` for a cache last written ``idle_seconds`` ago with ``ttl_seconds``.

    Edges, chosen to match the Claude Code idle hook (``idleDecision``) exactly:

    * missing / NaN idle gap -> ``unknown`` (``missing_timestamp``);
    * invalid TTL (missing, NaN, <= 0) -> ``unknown`` (``invalid_ttl``);
    * negative gap -> ``warm`` (``clock_skew``): never assume a cold cache from a clock that
      went backwards, since a wrong ``cold`` is the expensive mistake;
    * ``idle >= ttl`` -> ``cold`` (the boundary itself counts as expired);
    * otherwise ``warm``. ``+inf`` (very long idle) is cold.
    """
    if ttl_seconds is None or isinstance(ttl_seconds, bool):
        return "unknown", "invalid_ttl"
    try:
        ttl = float(ttl_seconds)
    except (TypeError, ValueError):
        return "unknown", "invalid_ttl"
    if not math.isfinite(ttl) or ttl <= 0:
        return "unknown", "invalid_ttl"
    if idle_seconds is None or isinstance(idle_seconds, bool):
        return "unknown", "missing_timestamp"
    try:
        idle = float(idle_seconds)
    except (TypeError, ValueError):
        return "unknown", "missing_timestamp"
    if math.isnan(idle):
        return "unknown", "missing_timestamp"
    if idle < 0:
        return "warm", "clock_skew"
    if idle >= ttl:
        return "cold", "idle_past_ttl"
    return "warm", "idle_within_ttl"


def _request_cost(
    *, cached_tokens: int, total_tokens: int, state: str, reuses: int, factors: CacheFactors
) -> float:
    """Relative cost of sending ``total_tokens`` now and ``reuses - 1`` more times.

    Warm: the first ``cached_tokens`` are cache reads, the rest cache writes. Cold: all of it
    is written. Every later reuse reads the whole request from cache.
    """
    cached = max(0, min(cached_tokens, total_tokens)) if state == "warm" else 0
    first = cached * factors.read + (total_tokens - cached) * factors.write
    later = max(0, reuses - 1) * total_tokens * factors.read
    return (first + later) / factors.input


@dataclass(frozen=True)
class CacheEffect:
    """What a pass does to a provider prompt cache, and whether it pays for itself."""

    cache_state: str
    assumed_state: str
    baseline: str
    stable_prefix_tokens: int
    invalidated_prefix_tokens: int
    rewritten_tokens: int
    removed_tokens: int
    expected_reuses: int
    factors: CacheFactors
    cost_no_pass: float
    cost_pass: float
    estimated_relative_cost_delta: float
    recommended: bool
    reason: str
    epoch_opened: bool
    sticky_flips: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": CACHE_CONTRACT_VERSION,
            "cache_state": self.cache_state,
            "assumed_state": self.assumed_state,
            "baseline": self.baseline,
            "stable_prefix_tokens": self.stable_prefix_tokens,
            "invalidated_prefix_tokens": self.invalidated_prefix_tokens,
            "rewritten_tokens": self.rewritten_tokens,
            "removed_tokens": self.removed_tokens,
            "expected_reuses": self.expected_reuses,
            "factors": self.factors.to_dict(),
            "estimated_cost_no_pass": _r(self.cost_no_pass, 4),
            "estimated_cost_pass": _r(self.cost_pass, 4),
            "estimated_relative_cost_delta": _r(self.estimated_relative_cost_delta, 4),
            "recommended": self.recommended,
            "reason": self.reason,
            "epoch_opened": self.epoch_opened,
            "sticky_flips": list(self.sticky_flips),
        }


def estimate_cache_effect(
    *,
    baseline_tokens: int,
    no_pass_tokens: int,
    no_pass_stable_tokens: int,
    pass_tokens: int,
    pass_stable_tokens: int,
    cache_state: str = "unknown",
    expected_reuses: int = 1,
    factors: CacheFactors | None = None,
    min_removed_tokens: int = 0,
    baseline: str = "input",
    epoch_opened: bool | None = None,
    sticky_flips: tuple[str, ...] = (),
) -> CacheEffect:
    """Compare sending the compacted context (pass) with sending the uncompacted one.

    ``baseline_tokens`` is what the provider has cached (the input, or the previous pass's
    output). ``*_stable_tokens`` are the tokens of each candidate request that share a
    byte-identical prefix with that baseline. ``unknown`` cache state is priced as ``warm``,
    the conservative assumption for a pass. The pass is recommended only when it removes at
    least ``max(1, min_removed_tokens)`` tokens *and* lowers the estimated cost over
    ``expected_reuses`` requests.
    """
    if cache_state not in CACHE_STATES:
        raise ValueError(f"cache_state must be one of {CACHE_STATES}, got {cache_state!r}")
    factors = factors or CacheFactors()
    reuses = max(1, int(expected_reuses))
    assumed = "warm" if cache_state == "unknown" else cache_state
    no_pass_stable = max(0, min(no_pass_stable_tokens, no_pass_tokens, baseline_tokens))
    pass_stable = max(0, min(pass_stable_tokens, pass_tokens, baseline_tokens))
    cost_no = _request_cost(
        cached_tokens=no_pass_stable, total_tokens=no_pass_tokens, state=assumed,
        reuses=reuses, factors=factors,
    )
    cost_yes = _request_cost(
        cached_tokens=pass_stable, total_tokens=pass_tokens, state=assumed,
        reuses=reuses, factors=factors,
    )
    delta = 0.0 if cost_no == 0 else (cost_yes - cost_no) / cost_no
    removed = no_pass_tokens - pass_tokens
    floor = max(1, int(min_removed_tokens))
    if removed <= 0:
        recommended, reason = False, "nothing_removed"
    elif removed < floor:
        recommended, reason = False, "below_min_removed_tokens"
    elif cost_yes >= cost_no:
        recommended, reason = False, f"{assumed}_cache_invalidation_exceeds_saving"
    else:
        recommended, reason = True, f"saves_cost_{assumed}_cache"
    invalidated = max(0, baseline_tokens - pass_stable)
    return CacheEffect(
        cache_state=cache_state,
        assumed_state=assumed,
        baseline=baseline,
        stable_prefix_tokens=pass_stable,
        invalidated_prefix_tokens=invalidated,
        rewritten_tokens=max(0, pass_tokens - pass_stable),
        removed_tokens=removed,
        expected_reuses=reuses,
        factors=factors,
        cost_no_pass=cost_no,
        cost_pass=cost_yes,
        estimated_relative_cost_delta=delta,
        recommended=recommended,
        reason=reason,
        epoch_opened=bool(invalidated > 0 if epoch_opened is None else epoch_opened),
        sticky_flips=tuple(sticky_flips),
    )
