"""Compaction invariants as property tests, plus the compaction edge cases.

Properties checked over the seeded hostile corpus (``tests/edge_corpus.py``) and every local
provider path (mechanical, a deterministic stand-in for Jev that exercises the trim band and
the verifier, and specialist-v3):

* determinism   same input + options -> byte-identical output and report (time fields aside)
* verbatim      every kept block is re-emitted byte-exact at its place; a trimmed block keeps
                byte-exact segments of itself (a prefix, or head + tail for logs) plus a note
* accounting    report counters reconcile exactly with the decisions and the emitted text
* idempotence   a second pass with the same objective never drops what the first pass kept
                for an intrinsic reason (see ``test_second_pass_*`` for the exact property)
* never-drop    protected blocks, the latest user message and the specialist floor survive
                any drop budget
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from edge_corpus import SPECIAL_INPUTS, edge_documents
from lcc.relevance import (
    RelevanceCompactionRequest,
    compact_context,
    parse_transcript,
    render_transcript,
    report_to_dict,
)
from lcc.relevance.blocks import split_blocks
from lcc.relevance.compactor import _render_trimmed, _restoration_rank
from lcc.relevance.graph import ContextGraph, EdgeType, GraphEdge, build_graph
from lcc.token_budget import count_tokens
from specialist_synthetic import synthetic_payload

ROOT = Path(__file__).resolve().parents[1]
TIME_FIELDS = ("latency_ms", "compilation_ms")
QUESTION = "why does the router cache budget fail for the tenant import?"
_MARKER_RE = re.compile(r"\[lcc-compact: dropped [^\]\n]*\]")
_TRIM_NOTE_RE = re.compile(r"\n\[lcc-compact: trimmed \d+ of \d+ chars of this block[^\]\n]*\]")


class HashJudge:
    """Deterministic stand-in for a Jev client: a block's score is a hash of its text.

    Scores spread over [0, 1], so a corpus exercises keep, the trim band and drop. The
    verifier questions answer ``verifier`` (a ``(sufficiency, confidence, contradiction)``
    triple) so a test can pin PASS / REVIEW / FAIL.
    """

    model = "hash-judge"
    last_resolved_model = "hash-judge-1"

    def __init__(self, verifier: tuple[float, float, float] = (0.9, 0.9, 0.0)) -> None:
        self.verifier = verifier
        self.calls = 0

    @staticmethod
    def score(text: str) -> float:
        return int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:4], 16) / 0xFFFF

    def evaluate(self, state: dict[str, Any], questions: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        answers: dict[str, Any] = {}
        texts = {block["id"]: block["text"] for block in state.get("blocks", [])}
        for name in questions:
            if name.startswith("keep_"):
                answers[name] = {"noul": self.score(texts[name[5:]]), "confidence": 0.9}
            elif name == "sufficient_to_answer":
                answers[name] = {"noul": self.verifier[0], "confidence": self.verifier[1]}
            elif name == "contradiction_risk":
                answers[name] = {"noul": self.verifier[2], "confidence": 0.9}
        return {"answers": answers}


def _request(text: str, provider: str, **overrides: Any) -> RelevanceCompactionRequest:
    options: dict[str, Any] = {"text": text, "question": QUESTION, "provider": provider}
    if provider == "hash":
        options.update(provider="jev", client=HashJudge(), max_workers=1)
    options.update(overrides)
    return RelevanceCompactionRequest(**options)


def _stable_report(report: Any) -> dict[str, Any]:
    payload = report_to_dict(report)
    for name in TIME_FIELDS:
        payload.pop(name, None)
    return payload


def assert_verbatim_and_accounting(text: str, request: RelevanceCompactionRequest, result: Any):
    """Rebuild the output from the decisions and the source; every byte must be accounted."""
    report = result.report
    out = result.compacted_text
    blocks = split_blocks(
        text, min_block_chars=request.min_block_chars, max_block_chars=request.max_block_chars
    )
    decisions = report.decisions
    assert [d.id for d in decisions] == [b.id for b in blocks]

    cursor = 0
    previous_end = 0
    pending_ending = ""  # line ending owed after a marker / trim note (see _glue_guard)
    in_dropped_run = False
    # Sticky (decisions-cache) mode emits one marker per dropped block so an earlier marker
    # never changes when a dropped run grows (cache alignment); one-shot passes merge runs.
    merge_runs = (
        request.decisions_cache_path is None
        if request.merge_dropped_runs is None
        else request.merge_dropped_runs
    )
    for position, (block, decision) in enumerate(zip(blocks, decisions, strict=True)):
        gap = text[previous_end : block.character_start]
        previous_end = block.character_end
        assert decision.chars == len(block.text)
        assert decision.decision in ("keep", "trim", "drop")
        if decision.decision == "drop":
            if not in_dropped_run or not merge_runs:
                cursor = _consume(out, cursor, pending_ending)
                assert out.startswith(gap, cursor), f"gap before {block.id} altered"
                cursor += len(gap)
                if request.marker:
                    marker = _MARKER_RE.match(out, cursor)
                    assert marker, f"no marker for dropped run at {block.id}"
                    cursor = marker.end()
            in_dropped_run = True
            pending_ending = _glue_guard(text, blocks, position, request.marker)
            continue
        in_dropped_run = False
        cursor = _consume(out, cursor, pending_ending)
        pending_ending = ""
        assert out.startswith(gap, cursor), f"gap before {block.id} altered"
        cursor += len(gap)
        if decision.decision == "keep":
            assert out.startswith(block.text, cursor), f"kept block {block.id} not verbatim"
            cursor += len(block.text)
            continue
        rendered = _render_trimmed(
            block.text,
            decision.score,
            report.trim_threshold,
            request.threshold,
            request.trim_head_chars,
        )
        assert out.startswith(rendered, cursor), f"trimmed block {block.id} not as rendered"
        assert decision.chars_after == len(rendered)
        note = _TRIM_NOTE_RE.search(rendered)
        assert note is not None
        head = rendered[: note.start()]
        _assert_segments_of(head, block.text, decision.content_type)
        cursor += len(rendered)
    cursor = _consume(out, cursor, pending_ending)
    assert out[cursor:] == text[previous_end:], "trailing gap altered"

    # Accounting: counters reconcile with decisions and the emitted bytes.
    kinds = [d.decision for d in decisions]
    assert report.blocks_total == len(blocks)
    assert report.blocks_dropped == kinds.count("drop")
    assert report.blocks_trimmed == kinds.count("trim")
    sanitized = sum(1 for d in decisions if d.source == "sanitized")
    assert report.blocks_protected == sum(1 for d in decisions if d.source == "protected")
    assert report.blocks_scored == len(blocks) - report.blocks_protected - sanitized
    assert report.chars_before == len(text)
    assert report.chars_after == len(out)
    assert report.chars_removed == len(text) - len(out)
    assert report.tokens_before == count_tokens(text, request.model).value
    assert report.tokens_after == count_tokens(out, request.model).value
    assert report.output_sha256 == hashlib.sha256(out.encode("utf-8")).hexdigest()
    expected_ratio = (len(text) - len(out)) / len(text) if text else 0.0
    assert report.reduction_ratio == pytest.approx(expected_ratio, abs=0, rel=0)
    mutated = any(kind != "keep" for kind in kinds)
    if not mutated:
        assert out == text
        assert report.first_mutation_offset is None
    else:
        first = next(b for b, d in zip(blocks, decisions, strict=True) if d.decision != "keep")
        assert report.first_mutation_offset == first.character_start
        assert out[: first.character_start] == text[: first.character_start]
    assert report.worth_it == (
        not mutated or request.min_reduction <= 0 or expected_ratio >= request.min_reduction
    )
    hits, misses = report.semantic_cache_hits, report.semantic_cache_misses
    assert hits >= 0 and misses >= 0
    assert hits + misses == report.blocks_scored


def _glue_guard(text: str, blocks: list[Any], position: int, replaced: bool) -> str:
    """Line ending owed after the marker that replaced a dropped run ending at ``position``.

    The marker keeps the replaced block's own line ending, so the next kept block stays on
    its own line and in its own paragraph (no marker glued onto a following code fence).
    A trimmed block carries its ending inside ``_render_trimmed``.
    """
    if not replaced:
        return ""
    for ending in ("\r\n", "\n", "\r"):
        if blocks[position].text.endswith(ending):
            return ending
    return ""


def _consume(out: str, cursor: int, ending: str) -> int:
    if ending:
        assert out.startswith(ending, cursor), "replacement glued onto the next block"
        return cursor + len(ending)
    return cursor


def _assert_segments_of(head: str, source: str, content_type: str | None) -> None:
    """A trimmed head is a prefix of its block, or (logs) a prefix + newline + a tail slice.

    The tail slice runs to the end of the block, short of trailing whitespace (the trim note
    goes right after it).
    """
    if source.startswith(head):
        return
    assert content_type == "logs", f"{content_type} trim altered bytes: {head[:60]!r}"
    for cut in [i for i, ch in enumerate(head) if ch == "\n"]:
        tail = head[cut + 1 :]
        if source.startswith(head[:cut]) and (
            source.endswith(tail) or source.rstrip().endswith(tail)
        ):
            return
    raise AssertionError(f"log trim is not head + tail of the block: {head[:60]!r}")


CORPUS = [(f"special{i}", t) for i, t in enumerate(SPECIAL_INPUTS)] + [
    (f"seed{s}", t) for s, t in edge_documents(40, start=100)
]
PROVIDERS = ("mechanical", "hash", "specialist-v3")


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize(("name", "text"), CORPUS, ids=[n for n, _ in CORPUS])
def test_compaction_is_verbatim_accounted_and_deterministic(
    name: str, text: str, provider: str
) -> None:
    request = _request(text, provider)
    first = compact_context(request)
    assert_verbatim_and_accounting(text, request, first)
    second = compact_context(_request(text, provider))
    assert second.compacted_text == first.compacted_text
    assert _stable_report(second.report) == _stable_report(first.report)


def test_trimmed_crlf_table_keeps_its_bytes() -> None:
    table = "| key | value |\r\n|---|---|\r\n" + "".join(
        f"| row{i} | some fairly long value number {i} |\r\n" for i in range(40)
    )
    from lcc.relevance.trim import trim_block_safe

    head, ok, ctype = trim_block_safe(table, 300)
    assert (ok, ctype) == (True, "markdown_table")
    assert table.startswith(head)


def test_trimmed_crlf_logs_keep_their_bytes() -> None:
    logs = "".join(f"2024-01-01 10:00:{i:02d} INFO worker step {i} done\r\n" for i in range(60))
    from lcc.relevance.trim import trim_block_safe

    head, ok, ctype = trim_block_safe(logs, 400)
    assert (ok, ctype) == (True, "logs")
    _assert_segments_of(head, logs, ctype)


def test_marker_never_glues_onto_a_following_code_fence() -> None:
    noise = "unrelated chatter about lunch plans and the weather today, nothing else at all:\n"
    fence = "```python\nrouter_cache_budget = 3  # router cache budget\n```\n"
    text = noise + fence
    result = compact_context(_request(text, "mechanical"))
    assert result.report.decisions[0].decision == "drop"
    assert result.compacted_text.endswith("]\n" + fence)


def test_trim_note_never_glues_onto_a_following_code_fence() -> None:
    prose = "router cache budget notes " + "with plenty of filler words here " * 40 + "\n"
    fence = "```\nrouter cache budget fence\n```\n"
    judge = HashJudge()
    judge.score = staticmethod(lambda text: 0.3 if text.startswith("router cache") else 0.9)
    request = RelevanceCompactionRequest(
        text=prose + fence, question=QUESTION, provider="jev", client=judge, max_workers=1
    )
    result = compact_context(request)
    assert [d.decision for d in result.report.decisions] == ["trim", "keep"]
    assert result.compacted_text.endswith("]\n" + fence)
    assert_verbatim_and_accounting(prose + fence, request, result)


# --- determinism across hash seeds and locales ---------------------------------------------

_DETERMINISM_SCRIPT = r"""
import hashlib, json, sys
sys.path[:0] = [sys.argv[1], sys.argv[2]]
from edge_corpus import edge_documents
from lcc.relevance import RelevanceCompactionRequest, compact_context, report_to_dict
from lcc.cleaning import safe_clean_text
digest = hashlib.sha256()
for seed, text in edge_documents(12, start=300):
    for provider in ("mechanical", "specialist-v3"):
        result = compact_context(RelevanceCompactionRequest(
            text=text, question="router cache budget tenant import", provider=provider))
        report = report_to_dict(result.report)
        for name in ("latency_ms", "compilation_ms"):
            report.pop(name, None)
        digest.update(result.compacted_text.encode("utf-8"))
        digest.update(json.dumps(report, sort_keys=True).encode("utf-8"))
    digest.update(safe_clean_text(text).encode("utf-8"))
print(digest.hexdigest())
"""


def test_output_is_independent_of_hash_seed_and_locale() -> None:
    digests = set()
    for seed, locale in (("0", "C"), ("1", "C.UTF-8"), ("4242", "C"), ("random", "C.UTF-8")):
        env = dict(os.environ, PYTHONHASHSEED=seed, LC_ALL=locale, LANG=locale)
        proc = subprocess.run(
            [sys.executable, "-c", _DETERMINISM_SCRIPT, str(ROOT / "src"), str(ROOT / "tests")],
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        digests.add(proc.stdout.strip())
    assert len(digests) == 1


# --- idempotence ----------------------------------------------------------------------------

#: Reasons a block survives on its own merits: they do not depend on which other blocks are
#: present, so a second pass over the output must keep the block again.
INTRINSIC_KEEP = re.compile(
    r"^(lexical_overlap|short_block|keep_pattern|prefix_protect|tail_preserve|"
    r"quoted_speech_present|evidence_in_\w+|high_risk_conservative_retention)$"
)


@pytest.mark.parametrize(("name", "text"), CORPUS, ids=[n for n, _ in CORPUS])
def test_second_pass_never_drops_an_intrinsically_kept_block(name: str, text: str) -> None:
    """compact(compact(x)) property, stated honestly.

    A second mechanical pass over the output, same objective, may drop a block the first pass
    kept only for a *relational* reason (a dependency link or a sufficiency restoration),
    because the block it was linked to can be gone and corpus-relative term rarity changes.
    It must never drop a block kept for an intrinsic reason, never drop a marker, and its
    own output is a fixed point once nothing relational is left.
    """
    first = compact_context(_request(text, "mechanical"))
    second = compact_context(_request(first.compacted_text, "mechanical"))
    intrinsic = {
        split.text
        for split, decision in zip(split_blocks(text), first.report.decisions, strict=True)
        if decision.decision == "keep" and INTRINSIC_KEEP.match(decision.reason)
    }
    for block, decision in zip(
        split_blocks(first.compacted_text), second.report.decisions, strict=True
    ):
        if decision.decision == "drop":
            assert block.text not in intrinsic, f"second pass dropped {block.text[:60]!r}"
            assert not _MARKER_RE.search(block.text)
    third = compact_context(_request(second.compacted_text, "mechanical"))
    fourth = compact_context(_request(third.compacted_text, "mechanical"))
    assert fourth.compacted_text == third.compacted_text or (
        len(fourth.compacted_text) < len(third.compacted_text)
    )


def test_compacting_a_keep_only_result_is_the_identity() -> None:
    text = "router cache budget one paragraph about the router cache budget and more.\n"
    first = compact_context(_request(text, "mechanical"))
    second = compact_context(_request(first.compacted_text, "mechanical"))
    assert first.compacted_text == second.compacted_text == text


# --- never-drop protections at any budget ---------------------------------------------------


def _transcript_text(seed: int = 3, turns: int = 10) -> tuple[str, Any, list[dict[str, Any]]]:
    payload = synthetic_payload(seed=seed, turns=turns)
    payload.append(
        {
            "role": "user",
            "text": "Please now fix the router cache budget bug in the tenant import job; "
            "the last traceback is what matters and nothing else should change.",
        }
    )
    text, segments = render_transcript(parse_transcript(payload))
    return text, segments, payload


@pytest.mark.parametrize("fraction", [0.0, 0.3, 0.6, 0.9, 0.99])
def test_protected_blocks_survive_any_specialist_budget(fraction: float) -> None:
    text, segments, _ = _transcript_text()
    request = RelevanceCompactionRequest(
        text=text,
        question=QUESTION,
        provider="specialist-v3",
        segments=segments,
        specialist_drop_fraction=fraction,
        keep_patterns=(r"def \w+_0\b",),
        preserve_tail_blocks=3,
        protect_prefix_chars=200,
    )
    result = compact_context(request)
    assert_verbatim_and_accounting(text, request, result)
    for decision in result.report.decisions:
        if decision.source == "protected":
            assert decision.decision == "keep"
    reasons = {d.reason for d in result.report.decisions if d.source == "protected"}
    assert {"tail_preserve", "prefix_protect", "short_block"} <= reasons
    if fraction == 0.0:
        assert result.report.blocks_dropped == 0


@pytest.mark.parametrize("fraction", [0.3, 0.6, 0.9, 0.99])
def test_latest_user_message_survives_any_specialist_budget(fraction: float) -> None:
    text, segments, payload = _transcript_text()
    result = compact_context(
        RelevanceCompactionRequest(
            text=text,
            question=QUESTION,
            provider="specialist-v3",
            segments=segments,
            specialist_drop_fraction=fraction,
        )
    )
    assert payload[-1]["text"] in result.compacted_text


@pytest.mark.parametrize("fraction", [0.3, 0.6, 0.9, 0.99])
def test_specialist_floor_blocks_survive_any_budget(fraction: float) -> None:
    from lcc.relevance.specialist import load_judge, meta_for_blocks

    text, segments, _ = _transcript_text(seed=11, turns=14)
    blocks = split_blocks(text)
    meta = meta_for_blocks([(b.character_start, b.character_end) for b in blocks], segments)
    _, floor = load_judge().score_and_floor(QUESTION, [b.text for b in blocks], meta)
    assert any(floor)
    result = compact_context(
        RelevanceCompactionRequest(
            text=text,
            question=QUESTION,
            provider="specialist-v3",
            segments=segments,
            specialist_drop_fraction=fraction,
        )
    )
    for block, flagged, decision in zip(blocks, floor, result.report.decisions, strict=True):
        if flagged:
            assert decision.decision == "keep", f"floor block {block.id} dropped at {fraction}"


# --- budget edges ---------------------------------------------------------------------------


def test_drop_fraction_bounds_are_validated() -> None:
    for bad in (-0.1, 1.0, 1.5):
        with pytest.raises(ValueError, match="specialist_drop_fraction"):
            compact_context(_request("x", "specialist-v3", specialist_drop_fraction=bad))


@pytest.mark.parametrize("head", [0, 1, 99, 100, 101, 10_000])
def test_trim_budget_edges(head: int) -> None:
    block = "router cache budget detail " * 11  # 297 chars, one block
    from lcc.relevance.trim import trim_block_safe

    trimmed, ok, _ = trim_block_safe(block, head)
    if head <= 0 or len(block) <= head + 200:
        assert (trimmed, ok) == (block, False)  # too small to trim: keep whole
    else:
        assert ok and block.startswith(trimmed) and len(trimmed) <= head


def test_single_block_bigger_than_the_block_budget_is_split_not_lost() -> None:
    text = "".join(f"line {i} router cache budget\n" for i in range(2000))
    request = _request(text, "mechanical", max_block_chars=500)
    result = compact_context(request)
    assert result.report.blocks_total > 1
    assert all(d.chars <= 500 + 80 for d in result.report.decisions)
    assert_verbatim_and_accounting(text, request, result)


def test_preserve_tail_and_prefix_larger_than_the_input_keep_everything() -> None:
    text = "\n\n".join(f"paragraph {i} about lunch and weather, nothing else, padding." * 2
                       for i in range(6))
    for options in ({"preserve_tail_blocks": 10_000}, {"protect_prefix_chars": 10**9}):
        result = compact_context(_request(text, "mechanical", **options))
        assert result.compacted_text == text
        assert result.report.blocks_dropped == 0


def test_reduction_gate_boundary_is_inclusive() -> None:
    text = "\n\n".join(
        [
            "The router cache budget is set per tenant import job and enforced at startup.",
            "Router cache budget overflow raises BudgetError before the import begins at all.",
            "Lunch on Friday: pizza or sushi, vote in the channel before eleven o'clock today.",
            "Weather looks sunny tomorrow afternoon; bring sunglasses for the picnic outside.",
        ]
    )
    probe = compact_context(_request(text, "mechanical", min_reduction=0.0))
    ratio = probe.report.reduction_ratio
    assert 0 < ratio < 1
    at = compact_context(_request(text, "mechanical", min_reduction=ratio))
    above = compact_context(_request(text, "mechanical", min_reduction=ratio + 1e-9))
    assert at.report.worth_it is True
    assert above.report.worth_it is False
    assert any(w.startswith("low_reduction") for w in above.report.warnings)
    # The gate only labels the pass; it never changes the emitted bytes.
    assert at.compacted_text == above.compacted_text == probe.compacted_text


# --- ties, graph cycles, restoration budgets ------------------------------------------------


def test_restoration_rank_ties_break_by_block_order() -> None:
    graph = ContextGraph()
    ids = ["blk_0003_" + "a" * 12, "blk_0001_" + "b" * 12, "blk_0002_" + "c" * 12]
    for bid in ids:
        graph.add_edge(GraphEdge(source=bid, target="blk_0009_" + "d" * 12,
                                 type=EdgeType.SUPPORTS, confidence=0.6))
    assert _restoration_rank(graph, ids, set()) == sorted(ids)
    assert _restoration_rank(None, ids, set()) == sorted(ids)


def test_restoration_rank_orders_numerically_past_9999_blocks() -> None:
    ids = ["blk_10000_" + "a" * 12, "blk_9999_" + "b" * 12]
    assert _restoration_rank(None, ids, set()) == ["blk_9999_" + "b" * 12, "blk_10000_" + "a" * 12]


def test_specialist_ties_drop_older_blocks_first() -> None:
    from lcc.relevance.blocks import TextBlock
    from lcc.relevance.compactor import _specialist_drop_texts

    pending = [
        TextBlock(id=f"b{i}", index=i, text=f"text {i} " * 20, character_start=0,
                  character_end=0, line_start=1, line_end=1)
        for i in range(1, 5)
    ]
    scores = {b.id: 0.0 for b in pending}
    dropped = _specialist_drop_texts(pending, scores, {}, 0.5)
    assert dropped == {pending[0].text, pending[1].text}


def test_graph_closure_terminates_on_cycles() -> None:
    graph = ContextGraph()
    for a, b in (("a", "b"), ("b", "c"), ("c", "a"), ("a", "a")):
        graph.add_edge(GraphEdge(source=a, target=b, type=EdgeType.DEPENDS_ON, confidence=0.9))
    pulled = graph.closure({"a"}, max_hops=50)
    assert set(pulled) == {"b", "c"}


def test_graph_is_deterministic_and_order_independent_of_hashing() -> None:
    blocks = [(f"blk_{i:04d}_{'0' * 12}", f"tenant import router budget item{i % 3} worker") for i
              in range(12)]
    first = build_graph(blocks, {"router"})
    second = build_graph(list(blocks), {"router"})
    assert first.edges == second.edges


def test_supersession_and_contradiction_edges_carry_the_cue_block_as_source() -> None:
    blocks = [
        ("blk_0001_" + "0" * 12, "Initial config: shard alpha uses a retry budget of three."),
        ("blk_0002_" + "0" * 12, "Revised: shard alpha retry budget is now seven."),
        ("blk_0003_" + "0" * 12, "The gamma ledger must not exceed its delta quota."),
        ("blk_0004_" + "0" * 12, "Notes on the gamma ledger and the delta quota."),
    ]
    graph = build_graph(blocks, set())
    by_type = {edge.type: edge for edge in graph.edges}
    assert by_type[EdgeType.SUPERSEDES].source == blocks[1][0]
    assert by_type[EdgeType.CONTRADICTS].source == blocks[2][0]


def test_accent_folding_makes_lexical_matching_accent_insensitive() -> None:
    from lcc.relevance.compactor import _lexical_terms, fold_accents

    assert fold_accents("Configuração NÃO Ação") == "configuracao nao acao"
    assert _lexical_terms("a função de exportação não") == {"funcao", "exportacao", "nao"}
    # Legacy (training-time) tokenization is unchanged for specialist-v3 features.
    assert _lexical_terms("a função de exportação", fold=False) == {"fun", "exporta"}


def test_portuguese_objective_without_accents_keeps_the_accented_evidence() -> None:
    text = "\n\n".join(
        [
            "A função de exportação do relatório falha quando o período tem mais de um ano.",
            "Lunch on Friday: pizza or sushi, vote in the channel before eleven o'clock today.",
        ]
    )
    result = compact_context(
        RelevanceCompactionRequest(
            text=text, question="por que a funcao de exportacao falha?", provider="mechanical"
        )
    )
    assert [d.decision for d in result.report.decisions] == ["keep", "drop"]


def test_specialist_features_keep_the_training_time_tokenization() -> None:
    from lcc.relevance.compactor import _score_mechanically
    from lcc.relevance.specialist import _mechanical

    goal = "exportação"
    texts = ["exportacao falhou no job noturno de relatorios e precisa ser refeito " * 2]
    assert _mechanical(goal, texts)[0][0] == 0.0  # as at training time
    blocks = split_blocks(texts[0])
    assert next(iter(_score_mechanically(blocks, goal).values()))[0] > 0.0


def test_portuguese_evidence_survives_an_english_objective_under_specialist_v3() -> None:
    sys.path.insert(0, str(ROOT / "benchmarks"))
    try:
        from run_judge_bench import edge_cases
    finally:
        sys.path.remove(str(ROOT / "benchmarks"))
    case = next(c for c in edge_cases() if c["id"] == "pt_en_mixed")
    text, segments = render_transcript(parse_transcript(case["payload"]))
    for fraction in (0.3, 0.5, 0.7):
        result = compact_context(
            RelevanceCompactionRequest(
                text=text, question=case["goal"], provider="specialist-v3",
                segments=segments, specialist_drop_fraction=fraction,
            )
        )
        assert case["needles"][0] in result.compacted_text, fraction
        reasons = {d.reason for d in result.report.decisions}
        assert "evidence_in_pt_not_en" in reasons


@pytest.mark.parametrize("budget", [0, 1, 2, 100])
def test_structural_restorations_never_exceed_their_budget(budget: int) -> None:
    linked = [
        f"tenant importer {i} uses ledger shard {i} and queue partition gamma delta epsilon."
        for i in range(6)
    ]
    text = "\n\n".join(
        ["router cache budget: the tenant importer ledger shard queue partition gamma delta."]
        + linked
    )
    request = _request(text, "mechanical", max_restorations=budget)
    result = compact_context(request)
    restored = [d for d in result.report.decisions
                if d.reason == "semantic_sufficiency_restoration"]
    assert len(restored) <= budget
    assert result.report.blocks_restored == len(restored)
    assert_verbatim_and_accounting(text, request, result)


@pytest.mark.parametrize(
    ("verifier", "decision", "review"),
    [
        ((0.9, 0.9, 0.0), "PASS", False),
        ((0.5, 0.5, 0.0), "PASS", False),  # boundaries are inclusive on the PASS side
        ((0.49, 0.69, 0.0), "REVIEW", True),  # low score, unsure judge -> doubt, not FAIL
        ((0.49, 0.7, 0.0), "FAIL", True),  # low score, confident judge -> FAIL
        ((0.9, 0.9, 0.7), "REVIEW", True),  # contradiction risk at the threshold
        ((0.9, 0.49, 0.0), "REVIEW", True),  # looks sufficient, judge unsure
    ],
)
def test_verifier_boundaries_and_restoration_budget(
    verifier: tuple[float, float, float], decision: str, review: bool
) -> None:
    text = "\n\n".join(
        f"block {i} {'router cache budget' if i % 2 else 'lunch chatter'} ledger shard "
        f"partition{i % 3} padding padding padding padding padding."
        for i in range(12)
    )
    judge = HashJudge(verifier=verifier)
    request = RelevanceCompactionRequest(
        text=text, question=QUESTION, provider="jev", client=judge, max_workers=1,
        enable_semantic_verify=True, verifier_max_restorations=1,
    )
    result = compact_context(request)
    report = result.report
    if report.blocks_dropped or report.blocks_trimmed or any(
        d.reason == "semantic_verifier_fail_restoration" for d in report.decisions
    ):
        assert report.semantic_verifier_decision == decision
        assert report.needs_review is review
    fail_restored = [d for d in report.decisions
                     if d.reason == "semantic_verifier_fail_restoration"]
    assert len(fail_restored) <= 1
    if decision != "FAIL":
        assert not fail_restored
    assert_verbatim_and_accounting(text, request, result)


def test_cache_hit_and_miss_counts_reconcile(tmp_path: Path) -> None:
    paragraphs = [f"paragraph {i} about widget {i}, padding padding padding padding padding" * 2
                  for i in range(6)]
    cache = tmp_path / "decisions.json"
    text = "\n\n".join(paragraphs)
    cold = compact_context(_request(text, "mechanical", decisions_cache_path=cache))
    assert (cold.report.semantic_cache_hits, cold.report.semantic_cache_misses) == (0, 6)
    warm = compact_context(_request(text, "mechanical", decisions_cache_path=cache))
    assert (warm.report.semantic_cache_hits, warm.report.semantic_cache_misses) == (6, 0)
    text2 = text + "\n\nZebra quokka marmot ibex: an appended block sharing no vocabulary at all, none whatsoever."
    request = _request(text2, "mechanical", decisions_cache_path=cache)
    result = compact_context(request)
    hits, misses = result.report.semantic_cache_hits, result.report.semantic_cache_misses
    assert hits >= 1 and misses >= 1  # misses used to be reported as len(pending) - hits
    assert hits + misses == result.report.blocks_scored == 7
    assert_verbatim_and_accounting(text2, request, result)


def test_report_json_is_stable_for_the_same_input() -> None:
    text = SPECIAL_INPUTS[-1]
    one = json.dumps(_stable_report(compact_context(_request(text, "hash")).report),
                     sort_keys=True)
    two = json.dumps(_stable_report(compact_context(_request(text, "hash")).report),
                     sort_keys=True)
    assert one == two
