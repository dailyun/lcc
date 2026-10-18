# Specialist v3: local block ranker

`provider="specialist-v3"` ranks the blocks of a rendered agent transcript and drops the
lowest-ranked ones. It runs on the CPU in plain Python, with no network, no API key and no extra
dependency. The model file is 183 KB and ships with the package.

It is a ranker, not a judge. It answers "which of these blocks is least likely to be needed
next", not "is this block relevant to the question", so reports always say
`semantic_guarantee: "none"`.

## When it runs

`auto` uses v3 whenever the caller passes structured transcript blocks (`segments`), ahead
of Jev and Laya. Jev runs only when named (`provider="jev"`); Laya is deprecated and runs only
when named (`provider="laya"`); no automatic or fallback path selects either.

| Request | Provider used |
| --- | --- |
| `provider="auto"` with `segments`, no decision cache | `specialist-v3`, `degraded: false` (Jev is not even resolved) |
| `provider="auto"`, plain text | Jev when available, else `mechanical` (unchanged; never Laya) |
| `provider="specialist-v3"` with `segments` | `specialist-v3` |
| `provider="specialist-v3"` without `segments` | `specialist-v3`, with warning `specialist_v3_no_block_metadata` (every block has unknown role/kind/tool, which the model never saw in training) |
| `provider="jev"` / `provider="laya"` | that judge, as before (opt-in) |
| model file missing, altered (sha256 mismatch) or unreadable | `specialist-v3+mechanical_fallback`, `degraded: true`, warning plus a log line on `lcc.relevance` |

Plain-text compaction keeps the Jev/mechanical behaviour: v3 was trained and evaluated only on
rendered transcripts with block metadata.

The old `provider="specialist"` is unchanged. It is a shadow seam for an injected ranker, it
never drops, and the real-session benchmark harness uses it. Reusing that name for a provider
that drops blocks would silently change what those callers get.

### Why v3 is the default ahead of Jev (INFERIDO)

- Held-out final test (36 sessions, LLM subset below): v3 ties Opus on recall AUC
  (0.876 vs 0.884, difference −0.009 [−0.055, +0.031]) and beats Gemini Flash on all-kept AUC
  (+0.118 [+0.026, +0.220]).
- Dev set (43 sessions, the maintainer's measurement, not reproduced in this repo): v3 0.886
  vs Jev 0.818 recall AUC. Dev numbers are optimistic: the same sessions informed v3's
  configuration.
- Jev was not measured on the held-out set (paid API), so "v3 ≥ Jev" rests on the dev set only.
- Cost: v3 is local, free and has no network dependency; Jev needs a key and a network call
  per batch; Laya is slow on CPU and its 512/1024-token window fits few transcripts.

## Tool-call compaction and the Claude Code hook

`compact_transcript` (the Claude Code hook, the MCP tool and `lcc compact --mode tool-calls`)
always has structured messages, so `auto`, the default on all three, is v3:

| Request | Provider used |
| --- | --- |
| `provider="auto"` (default) or `"specialist-v3"` | `specialist-v3`, `degraded: false`; Jev and Laya are never resolved |
| `provider="jev"` | Jev; without a key every call is kept, `jev_unavailable_fail_safe` (unchanged) |
| `provider="laya"` (deprecated) | Laya, as before |
| `provider="rules"` | the offline policy, unchanged (`lossless` default) |
| ranker error (model missing/altered, any exception) | every call kept, `degraded: true`, `specialist_v3_failed_fail_safe` |

Tool-call mode has no lexical fallback (mechanical is refused there), so the fail-closed
outcome on this path is "keep every call", and the hook then stands down to Claude Code's own
summary.

How a call is decided: the transcript is rendered with `render_transcript`, block
compaction runs with `provider="specialist-v3"` and the segments (so every protection rule
applies unchanged), then each unpinned call maps back by character span. If every block of
the call and of its result was dropped, the pair is dropped; if only the result's blocks
were, the result is trimmed to `trim_head_chars`; otherwise the pair is kept. User and
assistant text is never touched, even when the ranker would drop it, so a pass removes less
than `specialist_drop_fraction`. Short blocks are protected, so a call's own (short) input
block usually survives and the typical outcome is a trim, not a drop. On the synthetic
transcripts in `tests/specialist_synthetic.py` the pass removed 5–7% of the characters,
below the hook's default `minReductionRatio` of 0.25; real sessions, where tool output
dominates, were not measured here.

### Enable it in Claude Code

It is the plugin's default (`provider: auto`) once the plugin and the `lcc` package are
updated. To pin it explicitly: `/plugin` → lcc → configure `provider` = `specialist-v3`, or
`claude plugin install lcc@lcc --config provider=specialist-v3`. To go back to Jev, set
`provider` = `jev`. Lower `minReductionRatio` if passes keep falling below it. The toast
reads `lcc: verbatim compaction via specialist-v3, ...` when v3 ran.

### Verify it is active

Start Claude Code with `LCC_REQUEST_LOG=1` in its environment (the plugin's `lcc mcp`
server inherits it; `LCC_REQUEST_LOG_DIR` picks the directory, default `./.lcc/request-log`
relative to the server's working directory). Each `compact_transcript` call appends an entry
with `"provider"` (`specialist-v3`, `jev`, `degraded`, ...) and `"degradation_reason"`
(e.g. `specialist_v3_failed_fail_safe`, or `"none"`). Labels only, no content. Then:

```bash
lcc request-log          # ... by provider {'specialist-v3': 3} ...
lcc request-log --json   # "by_provider": {"specialist-v3": 3}
```

From the command line: `lcc compact session.json --mode tool-calls -r r.json --dry-run` and
check `provider_used` in `r.json`.

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
