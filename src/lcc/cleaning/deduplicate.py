"""Deterministic paragraph deduplication (exact and conservative near-duplicate)."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

from lcc.cleaning.normalize import _extract_protected_regions
from lcc.schemas import DedupMetrics

_WS = re.compile(r"\s+")

#: Work budget for near-duplicate detection, in characters compared (a full ``ratio()``
#: counts ``_RATIO_WEIGHT`` times). Pairwise ``difflib`` similarity is quadratic in the
#: number of paragraphs, so ~2,000 same-length paragraphs (200 KB) used to take minutes and a
#: few MB hours. The cheap upper bounds (``real_quick_ratio``, ``quick_ratio``) never change
#: a result; once the budget is spent the remaining paragraphs are only checked for *exact*
#: duplicates and otherwise kept (keeping is the safe direction), and
#: ``near_duplicate_budget_exhausted`` is set so callers can warn. Deterministic: the budget
#: counts characters, not wall time.
NEAR_DUP_CHAR_BUDGET = 12_000_000
_RATIO_WEIGHT = 4
_PAIR_COST = 2  # every pair visited, even one the O(1) length bound rejects


@dataclass
class DedupResult:
    """Deduplicated text plus paragraph-level metrics."""

    text: str
    metrics: DedupMetrics
    near_duplicate_budget_exhausted: bool = False


def _normalize_key(paragraph: str) -> str:
    """Comparison key: Unicode NFC plus collapsed whitespace.

    NFC makes a precomposed ``é`` and ``e`` + combining acute the same paragraph (they render
    identically and mean the same); trivial spacing differences count as exact duplicates.
    The key is only compared, never emitted: a kept paragraph keeps its own bytes.
    """
    return _WS.sub(" ", unicodedata.normalize("NFC", paragraph)).strip()


def _is_blank(line: str) -> bool:
    return line.strip(" \t") == ""


def _paragraphs(text: str) -> list[str]:
    """Blank-line separated paragraphs; a fenced code block is never split.

    Leading indentation is kept (only blank lines around a paragraph and trailing whitespace
    are removed), so nested lists, YAML and indented code keep their structure.
    """
    lines = text.split("\n")
    ranges, _ = _extract_protected_regions(lines)
    fence_end = dict(ranges)
    paragraphs: list[str] = []
    current: list[str] = []
    index = 0
    while index < len(lines):
        if index in fence_end:
            end = fence_end[index]
            current.extend(lines[index : end + 1])
            index = end + 1
            continue
        line = lines[index]
        if _is_blank(line):
            if current:
                paragraphs.append("\n".join(current).rstrip())
                current = []
        else:
            current.append(line)
        index += 1
    if current:
        paragraphs.append("\n".join(current).rstrip())
    return [paragraph for paragraph in paragraphs if paragraph.strip()]


def deduplicate_paragraphs(
    text: str,
    *,
    remove_near_duplicates: bool = True,
    similarity_threshold: float = 0.95,
    min_near_dup_chars: int = 40,
) -> DedupResult:
    """Remove duplicate paragraphs, keeping the first occurrence and preserving order.

    Paragraphs are blocks separated by one or more blank lines; a fenced code block is one
    unit, never split or deduplicated from the inside. Two paragraphs are *exact* duplicates
    when their NFC-normalized, whitespace-collapsed text is identical (case-sensitive). When
    ``remove_near_duplicates`` is True, a paragraph is dropped as a *near* duplicate only
    if its similarity ratio (``difflib``) to an already-kept paragraph is at least
    ``similarity_threshold`` and both paragraphs are at least ``min_near_dup_chars`` long
    -- a deliberately conservative rule that avoids collapsing distinct short lines.

    This is safe cleaning, not summarization: no paragraph is ever shortened or rewritten.
    Line endings are normalized to LF first, so CRLF input is handled correctly.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    paragraphs = _paragraphs(text)
    before = len(paragraphs)

    seen_exact: set[str] = set()
    kept: list[str] = []
    # One matcher per kept paragraph, with the kept text as ``b`` exactly as the pairwise
    # ``SequenceMatcher(None, candidate, existing)`` had it, so ``b``'s index is built once.
    matchers: list[tuple[SequenceMatcher[str], int]] = []
    exact_removed = 0
    near_removed = 0
    budget = NEAR_DUP_CHAR_BUDGET
    exhausted = False

    for paragraph in paragraphs:
        key = _normalize_key(paragraph)
        if key in seen_exact:
            exact_removed += 1
            continue

        is_near_duplicate = False
        lowered = key.lower()
        if remove_near_duplicates and not exhausted and len(key) >= min_near_dup_chars:
            for matcher, kept_len in matchers:
                budget -= _PAIR_COST
                if budget < 0:
                    exhausted = True
                    break
                matcher.set_seq1(lowered)
                # Upper bounds first: each is >= ratio(), so skipping on them is exact.
                if matcher.real_quick_ratio() < similarity_threshold:  # O(1)
                    continue
                budget -= len(lowered) + kept_len
                if budget < 0:
                    exhausted = True
                    break
                if matcher.quick_ratio() < similarity_threshold:
                    continue
                budget -= _RATIO_WEIGHT * (len(lowered) + kept_len)
                if budget < 0:
                    exhausted = True
                    break
                if matcher.ratio() >= similarity_threshold:
                    is_near_duplicate = True
                    break

        if is_near_duplicate:
            near_removed += 1
            continue

        seen_exact.add(key)
        kept.append(paragraph)
        if remove_near_duplicates and len(lowered) >= min_near_dup_chars:
            matchers.append((SequenceMatcher(None, "", lowered), len(lowered)))

    metrics = DedupMetrics(
        paragraphs_before=before,
        paragraphs_after=len(kept),
        duplicates_removed=exact_removed,
        near_duplicates_removed=near_removed,
    )
    return DedupResult(
        text="\n\n".join(kept), metrics=metrics, near_duplicate_budget_exhausted=exhausted
    )
