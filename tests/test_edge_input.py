"""Input edge cases for the deterministic core (cleaning, segmentation, pipeline).

Each test names the behaviour it pins; ``docs/EDGE_CASES.md`` maps every edge case to the test
that covers it. Expected behaviour is either "handled" (stated here) or "rejected with a clear
error" -- never a crash or a silent rewrite.
"""

from __future__ import annotations

import json
import subprocess
import sys
import unicodedata
from pathlib import Path

import pytest

from edge_corpus import SPECIAL_INPUTS, edge_documents
from lcc.cleaning import (
    deduplicate_paragraphs,
    normalize_text,
    remove_common_boilerplate,
    safe_clean_text,
)
from lcc.cleaning.speech import clean_speech_transcript
from lcc.intake import parse_intake
from lcc.pipeline import OptimizationRequest, optimize
from lcc.relevance import RelevanceCompactionRequest, compact_context
from lcc.relevance.blocks import (
    BLOCK_ID_RE,
    gaps_between,
    reconstruct,
    split_blocks,
)

ROOT = Path(__file__).resolve().parents[1]
FENCED_BOILERPLATE = (
    "Intro paragraph.\n\n"
    "```text\n"
    "----------\n"
    "Page 3 of 10\n"
    "On Mon, Bob wrote:\n"
    "Sent from my iPhone\n"
    "```\n"
)


# --- empty and whitespace-only -----------------------------------------------------------


@pytest.mark.parametrize("raw", ["", " ", "\n\n", "\r\n", " \t\u3000 \n"])
def test_pipeline_empty_and_whitespace_only_input_is_handled(raw: str) -> None:
    result = optimize(OptimizationRequest(raw_text=raw, question="anything"))
    assert result.cleaned_context == ""
    assert any("empty or whitespace-only" in w for w in result.report.warnings)


@pytest.mark.parametrize("raw", ["", "   ", "\n\n\n"])
def test_compaction_of_empty_input_is_identity(raw: str) -> None:
    result = compact_context(
        RelevanceCompactionRequest(text=raw, question="q", provider="mechanical")
    )
    assert result.compacted_text == raw
    assert result.report.blocks_total == 0
    assert result.report.blocks_dropped == 0


def test_compaction_requires_an_objective() -> None:
    with pytest.raises(ValueError, match="non-empty objective"):
        compact_context(RelevanceCompactionRequest(text="abc", question="  \n"))


# --- BOM -----------------------------------------------------------------------------------


def test_normalize_strips_leading_bom_and_records_it() -> None:
    result = normalize_text("\ufeffHello\n")
    assert result.text == "Hello"
    assert "strip_bom" in {step.name for step in result.steps}


def test_normalize_protects_a_code_fence_right_after_a_bom() -> None:
    result = normalize_text("\ufeff```py\na   =  1\n```\n")
    assert result.text == "```py\na   =  1\n```"


def test_blocks_detect_a_code_fence_right_after_a_bom() -> None:
    text = "\ufeff```\nx\n```\n" + "middle paragraph " * 8 + "\n\n```\ny\n```\n"
    blocks = split_blocks(text)
    assert [b.text for b in blocks] == [
        "\ufeff```\nx\n```\n",
        "middle paragraph " * 8 + "\n",
        "```\ny\n```\n",
    ]


def test_bom_is_kept_verbatim_by_compaction() -> None:
    text = "\ufeffkeep this paragraph about the router cache budget\n"
    result = compact_context(
        RelevanceCompactionRequest(text=text, question="router cache", provider="mechanical")
    )
    assert result.compacted_text == text


# --- newlines ------------------------------------------------------------------------------


def test_cr_only_line_numbers_count_cr_as_a_line_break() -> None:
    text = "a" * 90 + "\r\r" + "b" * 90 + "\r"
    blocks = split_blocks(text)
    assert [(b.line_start, b.line_end) for b in blocks] == [(1, 1), (3, 3)]


def test_mixed_newline_line_numbers() -> None:
    text = "one\r\ntwo\rthree\n\nfour " * 3
    for block in split_blocks(text, min_block_chars=1):
        expected_start = (
            len(text[: block.character_start].replace("\r\n", "\n").replace("\r", "\n").split("\n"))
        )
        assert block.line_start == expected_start


def test_unicode_line_separators_do_not_split_lines() -> None:
    # U+2028, form feed and friends are characters inside a line for editors and for
    # ``open()``'s universal newlines; only \n, \r\n and \r end a line.
    text = "alpha\u2028beta\x0cgamma\x85delta " * 5 + "\n"
    blocks = split_blocks(text, min_block_chars=1)
    assert len(blocks) == 1
    assert (blocks[0].line_start, blocks[0].line_end) == (1, 1)


def test_oversized_crlf_block_is_never_cut_between_cr_and_lf() -> None:
    text = "".join(f"row {i:05d} " + "x" * 40 + "\r\n" for i in range(400))
    blocks = split_blocks(text, max_block_chars=1000)
    assert len(blocks) > 1
    for block in blocks:
        assert not block.text.startswith("\n")
        assert len(block.text) <= 1000 + 80  # trailing tiny piece may merge back


# --- NUL and control characters ------------------------------------------------------------


def test_normalize_placeholder_text_in_input_is_not_substituted() -> None:
    raw = "x \x00__LCC_PROTECTED_BLOCK_0__\x00 y\n\n```\na  b\n```\n"
    result = normalize_text(raw)
    assert result.text == raw.rstrip("\n")


def test_speech_cleaning_placeholder_text_in_input_is_not_substituted() -> None:
    raw = "Speaker 1: hello \x00__LCC_PROTECTED_BLOCK_0__\x00\nSpeaker 2: hi\n```\ncode  here\n```"
    cleaned = clean_speech_transcript(raw)
    assert "\x00__LCC_PROTECTED_BLOCK_0__\x00" in cleaned
    assert cleaned.count("```\ncode  here\n```") == 1


def test_control_characters_survive_cleaning() -> None:
    raw = "bell\x07 escape\x1b[31m red\x1b[0m nul\x00 end"
    assert safe_clean_text(raw) == raw


# --- invalid UTF-8 / lone surrogates -------------------------------------------------------


def test_cli_rejects_invalid_utf8_with_a_clear_message(tmp_path: Path) -> None:
    source = tmp_path / "bad.txt"
    source.write_bytes(b"valid start \xff\xfe invalid bytes\n")
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from lcc.cli import cli_main; sys.argv[0] = 'lcc'; cli_main()",
            "optimize",
            str(source),
            "-q",
            "what?",
        ],
        capture_output=True,
        text=True,
        env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin"},
        check=False,
    )
    assert proc.returncode != 0
    assert "UTF-8" in proc.stderr
    assert "Traceback" not in proc.stderr


LONE_SURROGATE = json.loads('"before \\ud800 after"')


def test_compaction_rejects_lone_surrogates_with_a_clear_error() -> None:
    with pytest.raises(ValueError, match="unpaired surrogate"):
        compact_context(
            RelevanceCompactionRequest(text=LONE_SURROGATE, question="q", provider="mechanical")
        )


def test_pipeline_rejects_lone_surrogates_with_a_clear_error() -> None:
    with pytest.raises(ValueError, match="unpaired surrogate"):
        optimize(OptimizationRequest(raw_text=LONE_SURROGATE, question="q"))


# --- very long single line -----------------------------------------------------------------


def test_very_long_single_line_splits_into_bounded_verbatim_blocks() -> None:
    text = "token" * 40000  # 200k chars, no newline
    blocks = split_blocks(text)
    assert all(len(b.text) <= 6000 for b in blocks[:-1])
    assert reconstruct(text, blocks, gaps_between(text, blocks)) == text


# --- Unicode normalization -----------------------------------------------------------------


def test_nfc_and_nfd_paragraphs_are_exact_duplicates_and_first_bytes_win() -> None:
    nfc = "O relatório de exportação está pronto para revisão."
    nfd = unicodedata.normalize("NFD", nfc)
    assert nfc != nfd
    result = deduplicate_paragraphs(f"{nfd}\n\n{nfc}", remove_near_duplicates=False)
    assert result.text == nfd  # the first occurrence, its own bytes
    assert result.metrics.duplicates_removed == 1


def test_rtl_emoji_and_zero_width_characters_are_preserved() -> None:
    raw = "שלום \u202eRTL\u202c 👩\u200d💻 zero\u200bwidth 🇧🇷"
    assert safe_clean_text(raw) == raw
    result = compact_context(
        RelevanceCompactionRequest(text=raw + "\n", question="zero width", provider="mechanical")
    )
    assert result.compacted_text == raw + "\n"


# --- code fences must not be cleaned -------------------------------------------------------


def test_boilerplate_lines_inside_code_fences_are_kept() -> None:
    result = remove_common_boilerplate(FENCED_BOILERPLATE)
    assert result.text == FENCED_BOILERPLATE
    assert result.actions == []


def test_boilerplate_outside_fences_is_still_removed() -> None:
    text = "Page 2 of 9\n" + FENCED_BOILERPLATE + "Sent from my iPhone\n"
    result = remove_common_boilerplate(text)
    assert result.text == FENCED_BOILERPLATE
    assert {a.name for a in result.actions} == {
        "remove_boilerplate:page_marker",
        "remove_boilerplate:email_signature",
    }


def test_dedup_never_splits_or_dedups_inside_a_code_fence() -> None:
    code = (
        "```python\ndef f():\n    return 1\n\n    # same\n\n"
        "def g():\n    return 1\n\n    # same\n```"
    )
    result = deduplicate_paragraphs(code)
    assert result.text == code
    assert result.metrics.duplicates_removed == 0


def test_dedup_keeps_leading_indentation_of_a_paragraph() -> None:
    text = "items:\n  - a\n\n    nested: true\n    deeper:\n      - b"
    assert deduplicate_paragraphs(text).text == text


def test_pipeline_keeps_fenced_boilerplate_end_to_end() -> None:
    result = optimize(OptimizationRequest(raw_text=FENCED_BOILERPLATE, question="what?"))
    assert "Page 3 of 10" in result.cleaned_context
    assert "----------" in result.cleaned_context


# --- tables and deep nesting ---------------------------------------------------------------


def test_tables_keep_their_spacing_through_cleaning() -> None:
    table = "| a    | b  |\n|------|----|\n| 1    | 22 |"
    assert safe_clean_text(table) == table


def test_deep_nesting_is_preserved() -> None:
    nested = "\n".join("  " * depth + f"- level {depth}" for depth in range(200))
    assert safe_clean_text(nested) == nested
    blocks = split_blocks(nested)
    assert reconstruct(nested, blocks, gaps_between(nested, blocks)) == nested


# --- block ids at scale --------------------------------------------------------------------


def test_block_ids_match_the_published_pattern_past_9999_blocks() -> None:
    text = "\n\n".join(f"p{i}" for i in range(10_001))
    blocks = split_blocks(text)
    assert len(blocks) == 10_001
    assert all(BLOCK_ID_RE.match(b.id) for b in blocks)
    assert blocks[-1].id.startswith("blk_10001_")


# --- generated corpus: no crash, verbatim segmentation, determinism -------------------------


CORPUS = [(f"special{i}", t) for i, t in enumerate(SPECIAL_INPUTS)] + [
    (f"seed{s}", t) for s, t in edge_documents(60)
]


@pytest.mark.parametrize(("name", "text"), CORPUS, ids=[n for n, _ in CORPUS])
def test_cleaning_and_intake_never_crash_and_are_deterministic(name: str, text: str) -> None:
    first = optimize(OptimizationRequest(raw_text=text, question="router cache budget"))
    second = optimize(OptimizationRequest(raw_text=text, question="router cache budget"))
    assert first.cleaned_context == second.cleaned_context
    assert first.prompt == second.prompt
    assert safe_clean_text(text) == safe_clean_text(text)
    assert parse_intake(text) == parse_intake(text)


@pytest.mark.parametrize(("name", "text"), CORPUS, ids=[n for n, _ in CORPUS])
def test_segmentation_is_verbatim_and_ordered(name: str, text: str) -> None:
    blocks = split_blocks(text)
    gaps = gaps_between(text, blocks)
    assert reconstruct(text, blocks, gaps) == text
    cursor = 0
    for block in blocks:
        assert block.character_start >= cursor
        assert text[block.character_start : block.character_end] == block.text
        assert block.text.strip()  # gaps hold whitespace, blocks hold content
        assert 1 <= block.line_start <= block.line_end
        cursor = block.character_end
    assert not "".join(gaps).strip()


@pytest.mark.parametrize(("name", "text"), CORPUS, ids=[n for n, _ in CORPUS])
def test_cleaning_never_touches_fenced_code(name: str, text: str) -> None:
    """Every closed fence of the LF-normalized input survives cleaning byte-exact."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    fences = []
    start = normalized.find("```python\n")
    while start != -1:
        end = normalized.find("\n```", start + 3)
        if end == -1:
            break
        if start == 0 or normalized[start - 1] == "\n":  # an opener only at a line start
            fences.append(normalized[start : end + 4])
        start = normalized.find("```python\n", end + 4)
    cleaned = optimize(OptimizationRequest(raw_text=text, question="q")).cleaned_context
    for fence in fences:
        if fence.count("```") == 2:  # a fence that does not swallow a neighbour's opener
            assert fence in cleaned
