"""Deterministic text normalization (line endings, whitespace, blank lines)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from lcc.schemas import CleaningStep

_LEADING_WS = re.compile(r"^[ \t]+")
_INNER_RUNS = re.compile(r"(?<=\S)[ \t]{2,}(?=\S)")
_BLANK_RUNS = re.compile(r"\n{3,}")
_FENCE_START = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})(.*)$")
_TABLE_DELIM = re.compile(
    r"^[ \t]*\|?([ \t]*:?-{1,}:?[ \t]*\|)+[ \t]*:?-{1,}:?[ \t]*\|?[ \t]*$"
)


#: Prefix of the in-band placeholders that mask protected regions while the rest of a document
#: is cleaned. Input that already contains it gets a longer, unused prefix instead (see
#: :func:`placeholder_prefix`), so text that happens to look like a placeholder is never
#: substituted.
_PLACEHOLDER_PREFIX = "\x00__LCC_PROTECTED_BLOCK_"
_BOM = "\ufeff"


def placeholder_prefix(text: str) -> str:
    """A placeholder prefix that does not occur in ``text`` (deterministic in ``text``)."""
    prefix = _PLACEHOLDER_PREFIX
    salt = 0
    while prefix in text:
        salt += 1
        prefix = f"\x00__LCC_PROTECTED_BLOCK_{salt}_"
    return prefix


def first_lone_surrogate(text: str) -> int | None:
    """Offset of the first unpaired UTF-16 surrogate in ``text``, or ``None``.

    Such code points cannot be encoded as UTF-8; they reach a ``str`` through
    ``errors="surrogateescape"`` decoding or a JSON ``"\\ud800"`` escape. Callers reject them
    with a clear error instead of crashing later in hashing, tokenizing or writing.
    """
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as exc:
        return exc.start
    return None


def require_valid_unicode(text: str, what: str = "input text") -> None:
    """Raise ``ValueError`` when ``text`` holds an unpaired surrogate (invalid UTF-8)."""
    offset = first_lone_surrogate(text)
    if offset is not None:
        raise ValueError(
            f"{what} contains an unpaired surrogate (U+{ord(text[offset]):04X}) at offset "
            f"{offset}: it is not valid Unicode and cannot be encoded as UTF-8. Decode the "
            "source as UTF-8 (strict) or repair it before running lcc."
        )


@dataclass
class NormalizeResult:
    """Normalized text plus the list of cleaning steps that were applied."""

    text: str
    steps: list[CleaningStep]


def _extract_protected_regions(lines: list[str]) -> tuple[list[tuple[int, int]], list[str]]:
    """Identify line ranges [start_idx, end_idx] (inclusive) for fenced code blocks and tables."""
    protected_ranges: list[tuple[int, int]] = []
    n = len(lines)
    i = 0

    while i < n:
        line = lines[i]
        fence_match = _FENCE_START.match(line)
        if fence_match:
            fence_char = fence_match.group(1)[0]
            fence_len = len(fence_match.group(1))
            start_i = i
            i += 1
            while i < n:
                close_line = lines[i].strip()
                if (
                    close_line.startswith(fence_char * fence_len)
                    and all(c == fence_char for c in close_line)
                ):
                    i += 1
                    break
                i += 1
            protected_ranges.append((start_i, i - 1))
            continue
        i += 1

    def in_code_block(line_idx: int) -> bool:
        return any(start <= line_idx <= end for start, end in protected_ranges)

    table_ranges: list[tuple[int, int]] = []
    i = 0
    while i < n:
        if in_code_block(i):
            i += 1
            continue
        line = lines[i]
        if _TABLE_DELIM.match(line) and "|" in line:
            start_t = i
            while start_t > 0 and not in_code_block(start_t - 1):
                prev_line = lines[start_t - 1].strip()
                if prev_line and "|" in prev_line:
                    start_t -= 1
                else:
                    break
            end_t = i
            while end_t + 1 < n and not in_code_block(end_t + 1):
                next_line = lines[end_t + 1].strip()
                if next_line and "|" in next_line:
                    end_t += 1
                else:
                    break
            table_ranges.append((start_t, end_t))
            i = end_t + 1
            continue
        i += 1

    all_ranges = sorted(protected_ranges + table_ranges, key=lambda r: r[0])
    blocks = ["\n".join(lines[start : end + 1]) for start, end in all_ranges]
    return all_ranges, blocks


def normalize_text(text: str) -> NormalizeResult:
    """Normalize whitespace and line structure without altering meaning.

    Steps applied (each recorded in the result):

    0. Remove a leading byte-order mark (U+FEFF).
    1. Convert CRLF / CR line endings to LF.
    2. Strip trailing whitespace from every line (outside fenced code blocks / tables).
    3. Collapse runs of 2+ interior spaces/tabs to a single space in non-code, non-table
       regions, preserving leading indentation.
    4. Collapse 3+ consecutive newlines (multiple blank lines) to a single blank line.
    5. Trim leading/trailing blank lines from the whole document.

    Paragraph structure (a single blank line between blocks) and code/table formatting
    are preserved exactly.
    """
    steps: list[CleaningStep] = []

    if text.startswith(_BOM):
        # A byte-order mark is an encoding artifact, not content. Left in place it also hides
        # a code fence on the first line from the protection below.
        text = text[len(_BOM) :]
        steps.append(CleaningStep("strip_bom", "Removed a leading byte-order mark.", {"bom": 1}))

    crlf = text.count("\r\n")
    cr_only = text.count("\r") - crlf
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if crlf or cr_only:
        steps.append(
            CleaningStep(
                "normalize_line_endings",
                "Converted CR/CRLF line endings to LF.",
                {"crlf": crlf, "cr": cr_only},
            )
        )

    raw_lines = normalized.split("\n")
    all_ranges, blocks = _extract_protected_regions(raw_lines)
    prefix = placeholder_prefix(normalized)

    # Build line list with placeholders for protected blocks
    range_map: dict[int, tuple[int, int]] = {}  # start_idx -> (end_idx, block_idx)
    for block_idx, (start, end) in enumerate(all_ranges):
        range_map[start] = (end, block_idx)

    trimmed = 0
    collapsed_lines = 0
    out_lines: list[str] = []

    line_idx = 0
    n = len(raw_lines)
    while line_idx < n:
        if line_idx in range_map:
            end, block_idx = range_map[line_idx]
            out_lines.append(f"{prefix}{block_idx}__\x00")
            line_idx = end + 1
            continue

        line = raw_lines[line_idx]
        stripped = line.rstrip()
        if stripped != line:
            trimmed += 1
        lead_match = _LEADING_WS.match(stripped)
        lead = lead_match.group(0) if lead_match else ""
        body = stripped[len(lead) :]
        collapsed = _INNER_RUNS.sub(" ", body)
        if collapsed != body:
            collapsed_lines += 1
        out_lines.append(lead + collapsed)
        line_idx += 1

    if trimmed:
        steps.append(
            CleaningStep(
                "strip_trailing_whitespace",
                "Removed trailing whitespace from lines.",
                {"lines": trimmed},
            )
        )
    if collapsed_lines:
        steps.append(
            CleaningStep(
                "collapse_inner_spaces",
                "Collapsed repeated interior spaces to a single space.",
                {"lines": collapsed_lines},
            )
        )

    joined = "\n".join(out_lines)
    joined, blank_runs = _BLANK_RUNS.subn("\n\n", joined)
    if blank_runs:
        steps.append(
            CleaningStep(
                "collapse_blank_lines",
                "Collapsed runs of blank lines to a single blank line.",
                {"runs": blank_runs},
            )
        )

    result_text = joined.strip("\n")
    for block_idx, block in enumerate(blocks):
        result_text = result_text.replace(f"{prefix}{block_idx}__\x00", block)

    return NormalizeResult(text=result_text, steps=steps)
