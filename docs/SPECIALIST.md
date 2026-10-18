# Specialist v3: local block ranker

`provider="specialist-v3"` ranks the blocks of a rendered agent transcript and drops the
lowest-ranked ones. It runs on the CPU in plain Python, with no network, no API key and no extra
dependency. The model file is 183 KB and ships with the package.

It is a ranker, not a judge. It answers "which of these blocks is least likely to be needed
next", not "is this block relevant to the question", so reports always say
`semantic_guarantee: "none"`.

## When it runs

| Request | Provider used |
| --- | --- |
| `provider="specialist-v3"` with `segments` | `specialist-v3` |
| `provider="specialist-v3"` without `segments` | `specialist-v3`, with warning `specialist_v3_no_block_metadata` (every block has unknown role/kind/tool, which the model never saw in training) |
| `provider="auto"`, Jev unavailable, `segments` given, no decision cache | `specialist-v3`, `degraded: true`, `jev_unavailable_specialist_fallback` |
| `provider="auto"`, Jev unavailable, plain text | `mechanical`, as before |
| model file missing, altered (sha256 mismatch) or unreadable | `specialist-v3+mechanical_fallback`, `degraded: true`, warning plus a log line on `lcc.relevance` |

So v3 is the default local path only when the caller passes structured transcript blocks.
Plain-text compaction keeps the mechanical fallback: v3 was trained and evaluated only on
rendered transcripts with block metadata.

The old `provider="specialist"` is unchanged. It is a shadow seam for an injected ranker, it
never drops, and the real-session benchmark harness uses it. Reusing that name for a provider
that drops blocks would silently change what those callers get.

## Usage

```python
from lcc.relevance import (
    RelevanceCompactionRequest, compact_context, parse_transcript, render_transcript,
)

text, segments = render_transcript(parse_transcript(payload))
result = compact_context(RelevanceCompactionRequest(
    text=text, question="why does the router test fail?",
    provider="specialist-v3", segments=segments,
    specialist_drop_fraction=0.3,  # default
))
```

`render_transcript` writes one `[mNNNN role kind tool]` header per content part (text, tool
call, tool result) and returns one `BlockSegment` per part: character span, message index,
role, kind and tool name. The compactor maps every block to its segment by offset.

**Block metadata is never parsed from block text.** A tool output can contain a line shaped
like a header; it changes nothing, because role/kind/tool come from the segments. The research
version read them from header lines, which is why this was a requirement before integration.

## Drop policy

1. LCC's protection runs first and is unchanged: short blocks, `keep_patterns`, the protected
   prefix, `preserve_tail_blocks` and standalone-instruction sanitizing. Only blocks that
   survive it are candidates.
2. v3 scores the whole context in order (position and neighbours are features).
3. Candidates with a deterministic-protection reason (quoted speech, other-language evidence
   and the other rules of the mechanical safety net) and `high_risk` content are never dropped.
4. The rest are dropped lowest score first (ties: older first) until `specialist_drop_fraction`
   of the candidate characters is removed. Identical texts share one decision.

The `score` in the report is a logistic squash of the raw v3 score, kept for ordering. It is not
a probability. Decision caches are refused (`ValueError`), since a rank depends on the whole
context and a content-keyed sticky decision would be wrong.

## Model

- Logistic regression and 350 depth-4 boosted trees over 50 per-block features (position,
  recency, block kind, tool group, BM25 against the goal and the last turns, identifier reuse,
  duplicates, errors, LCC's own mechanical overlap and protection flag), averaged, minus
  0.5 × log(chars).
- Rule floor: +3.0 for the block with the highest BM25 match to the goal and the 2 most recent
  blocks with an error marker.
- `src/lcc/relevance/specialist_v3.json` holds numbers, the feature names and member tags only.
  A test checks that no other string is in it. Its sha256 is pinned in `specialist.py`.
- Trained on the maintainer's own agent sessions with next-action labels (which blocks the
  agent used next). No session text ships.

Parity: `tests/test_specialist_v3.py` compares LCC's scores, computed from structured fields,
with frozen scores from the research implementation, computed from rendered headers, on three
synthetic transcripts (75, 82 and 94 blocks). Maximum difference: 5e-10.

## Measured results

From the one-shot final test on a held-out set of sessions that started after the training
cutoff. Counts and aggregates only.

**Read first:** the labels are rule-based next-action evidence, which favours v3's training
target. Generalist LLMs judge generic relevance. This measures fit to one maintainer's
sessions (INFERIDO), not general reading skill. 182 of 193 items come from Claude Code
sessions; the codex (3) and hermes (8) rows are anecdotal.

Primary set: 193 items in 36 session groups, 65,161 candidate blocks. Retention AUC over
drop budgets of 0–60% of candidate tokens:

| arm | recall AUC | all-kept AUC |
| --- | ---: | ---: |
| specialist-v3 | 0.899 | 0.434 |
| mechanical | 0.829 | 0.238 |
| recency | 0.818 | 0.306 |
| bm25 | 0.803 | 0.270 |
| random | 0.689 | 0.138 |

v3 beat every local baseline on both metrics. The 95% CI lower bound of the paired
group bootstrap was above 0 in all six comparisons. Against mechanical: recall +0.070
[+0.044, +0.102], all-kept +0.197 [+0.134, +0.283].

LLM subset (36 items, one per group):

| arm | recall AUC | all-kept AUC | v3 − arm, recall [95% CI] |
| --- | ---: | ---: | --- |
| specialist-v3 | 0.876 | 0.417 | |
| Opus | 0.884 | 0.347 | −0.009 [−0.055, +0.031] |
| Gemini Flash | 0.838 | 0.299 | +0.038 [−0.007, +0.083] |

- **Opus: a tie.** The protocol labels the result "inferior" because the CI lower bound fell
  below −0.02. The point estimates are about even on recall and favour v3 on all-kept, and
  neither gap is significant.
- **Gemini Flash:** v3 is not inferior on recall and better on all-kept (+0.118
  [+0.026, +0.220]).
- v3 is not shown to be better than either generalist on recall.

**Edge suite (35 synthetic hard cases, passes at 30/50/70% cut):** v3 28/23/20, mechanical
22/21/18, Opus 35/35/35, Gemini Flash 35/35/34. This is v3's weak spot. It misses must-keep
blocks that both LLMs keep. The suite also helped choose v3's configuration, so its v3 numbers
are not held-out.

**Cost (final test, real items, numpy implementation):** median 178 ms, p90 567 ms per item
(median 296 blocks), peak RSS 97.5 MB.

**Cost of this implementation** (`python benchmarks/research/specialist_v3_latency.py`, synthetic
transcripts, one core, shared machine at load average ~110):

| transcript | ranker median | full pass, v3 | full pass, mechanical |
| --- | ---: | ---: | ---: |
| 302 blocks (held-out median size) | 105 ms | 450 ms | 171 ms |
| 1,138 blocks (held-out largest size) | 776 ms | 5.4 s | 3.7 s |

Peak RSS 66 MB. The gate is ranker median < 250 ms at the median size and RSS < 300 MB. Most
of a full pass on large inputs is LCC's own graph and sufficiency work, which mechanical
pays too.

## Limits

- It suits Claude Code transcripts like the ones it was trained on. Other agents and plain
  documents are out of distribution.
- It ranks next-action usefulness. Evidence that matters to the goal but is not reused soon
  can rank low. The goal floor covers only the single best-matching block.
- It has no notion of a sufficient context. It drops a fixed share and relies on LCC's
  protection rules for the rest.
