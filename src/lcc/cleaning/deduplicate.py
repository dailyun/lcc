"""Deterministic paragraph deduplication (exact and conservative near-duplicate)."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

from lcc.cleaning.normalize import _extract_protected_regions
from lcc.schemas import DedupMetrics

_WS = re.compile(r"\s+")


@dataclass
class DedupResult:
    """Deduplicated text plus paragraph-level metrics."""

    text: str
    metrics: DedupMetrics


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
    kept_keys: list[str] = []
    exact_removed = 0
    near_removed = 0

    for paragraph in paragraphs:
        key = _normalize_key(paragraph)
        if key in seen_exact:
            exact_removed += 1
            continue

        is_near_duplicate = False
        if remove_near_duplicates and len(key) >= min_near_dup_chars:
            lowered = key.lower()
            for existing in kept_keys:
                if len(existing) < min_near_dup_chars:
                    continue
                if SequenceMatcher(None, lowered, existing).ratio() >= similarity_threshold:
                    is_near_duplicate = True
                    break

        if is_near_duplicate:
            near_removed += 1
            continue

        seen_exact.add(key)
        kept.append(paragraph)
        kept_keys.append(key.lower())

    metrics = DedupMetrics(
        paragraphs_before=before,
        paragraphs_after=len(kept),
        duplicates_removed=exact_removed,
        near_duplicates_removed=near_removed,
    )
    return DedupResult(text="\n\n".join(kept), metrics=metrics)
