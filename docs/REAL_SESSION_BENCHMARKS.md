# Real-session benchmarks (tool-call mode)

What LCC's tool-call compaction does on real agent sessions, as opposed to the curated
corpora behind [JEV.md](JEV.md) and the two sessions in
[`benchmarks/research/REAL_SESSIONS.md`](../benchmarks/research/REAL_SESSIONS.md).
Measured 2026-10-09 to 2026-10-11. The `rules` arms ran on PR #45 branch commits (`413b6f4`,
`8997be6`, `d0cb548`, `7b92ad2`, `1bc2005`, `653eeae`, `4c39161`; squash-merged as `74f10e1`).
The mechanical, specialist and Jev rows in section 3 ran on `dd5dd80`, which predates #45
(`real-context-bench-v0/PROTOCOL.md`, `real-context-bench-v0.1/RESULTS.md`).

**Where the evidence lives.** Every number below comes from a committed receipt in
`github.com/lucasmartins-ai/lcc-judge` (a **private** repository) under
`research/goal-engine-2026-10-04/`. Readers outside the owner's account cannot open them, so
treat every number here as owner-attested. Those receipts hold counts only. Session text, ids and
per-item outputs stay on the owner's machine and are not published anywhere. The path
next to each table is relative to that folder.

**Read the limits first.** All runs are single, offline passes. Labels are mostly LLM-made,
token counts use LCC's heuristic or tokenizer as stated, and the blind audits were judged by
LLM subagents, not by the session owner. Treat every row as exploratory.

## 1. Recommended configuration

| Setting | Value | Why |
| :--- | :--- | :--- |
| `provider` | `rules` | offline, no key, milliseconds per session |
| `rules_mode` | `recoverable` | adds re-readable file pointers on top of `lossless` |
| `workspace_root` | the session's working directory | turns on the disk check (section 3) |
| `allow_command_pointers` | off (the default) | command pointers have never been audited |

`lossless` stays the default `rules_mode`: it needs no disk and makes no recovery claim.

- **MCP / Python:** pass `provider: "rules"`, `rules_mode: "recoverable"` and
  `workspace_root` to `compact_transcript` ([TOOL_CALLS.md](TOOL_CALLS.md), section 5).
- **Claude Code hook:** set the plugin options `provider=rules` and `rulesMode=recoverable`.
  The hook then sends the session's `cwd` as `workspace_root`
  ([hook README](../plugins/claude-code/hooks/README.md)). Its `minReductionRatio` default
  (0.25) is far above what lossless or recoverable remove on real sessions (section 2), so
  the hook will usually hand over to Claude Code's built-in summary unless you lower it.

## 2. The three rules modes

| Mode | What a removal is | How safety is checked | Measured reduction |
| :--- | :--- | :--- | :--- |
| `lossless` | a result replaced by a pointer to an identical later result, or to an exact line range of one | mechanically, three independent ways; no judge needed | pooled .0016 to .0185 per set |
| `recoverable` | `lossless`, plus old file reads (and, if enabled, allowlisted read-only commands) replaced by a "re-read / re-run to recover" pointer | transcript proxy, or the file on disk when `workspace_root` is set | .0123 pooled over 847 items (transcript proxy) |
| `lossy` (experimental) | old outputs dropped or trimmed by structural rules | blind LLM audit of sampled decisions | .19 to .64, but failed two blind audits |

### Lossless: 0 violations on 707 items

Source: `lossless-dedup-v1/RESULTS.md`, `lossless-dedup-v1/summary.json`.

| Set | Items | Pooled token reduction | Item / probe retention |
| :--- | ---: | ---: | :--- |
| Claude frozen 60 | 60 | .0034 | 49/49, 69/69 |
| Claude fresh 60 | 60 | .0016 | 55/55, 77/77 |
| Claude dev 326 | 326 | .0063 | 323/323, 455/455 |
| Codex v0.4 test 60 | 60 | .0125 | 54/54, 69/69 |
| Codex v0.5 dev 141 | 141 | .0185 | 137/137, 189/189 |
| Codex v0.5 test 60 | 60 | .0153 | 54/54, 72/72 |
| **All** | **707** | — | **672/672, 931/931** |

4,506 results were replaced, with 0 violations from the run-time self-check, from
`verify_lossless`, and from an independent re-check. Median per item is tiny (.0002 to
.0026); the savings come from a few sessions with many identical reruns (up to .53 on one
item). The independent check reuses the same Codex header normalisation, so a wrong rule
there would pass both.

### Recoverable: safe, but adds about 0.3 points over lossless offline

Sources: `real-context-bench-v0.6/RESULTS.md`, `real-context-bench-v0.7/RESULTS.md`,
`recoverable-v0.8/RESULTS.md`, `recoverable-v0.8/summary.json`.

| Run | Items | Recoverable | Lossless | Blind audit | Verdict |
| :--- | ---: | ---: | ---: | :--- | :--- |
| v0.6, Hermes held-out | 60 | .0411 | .0081 | 0/77 flagged | invalid: one fail-safe trip on a 1.4M-token item |
| v0.7, Hermes held-out | 80 | .0343 | .0033 | 1/54 flagged (upper bound .0977 vs .10) | PASS, no margin |
| v0.8, all eight sets, fixed pointers | 847 | .0123 | .0093 | none new (subset of v0.7) | 0 subset violations, verification 0/0/0 |

The v0.7 review found that a later script or build could rewrite a file the pointer said to
re-read. The v0.8 fix blocks those pointers (1,862 of 1,978 removed pointers were
`file_written_later`), which costs most of the gain. Only whole-file pointers were ever
audited; range pointers (41 survive at v0.8) and command pointers have no audit.

**Disk mode is not measured.** With `workspace_root` set, the file on disk decides: a read is
replaced only if the file's current bytes equal the removed output, re-hashed right before
emit ([TOOL_CALLS.md](TOOL_CALLS.md), section 5). Recorded sessions have no disk snapshot, so
no offline bench can measure it. Real-use numbers have to come from the opt-in request log
(section 5).

### Lossy: failed on session-disjoint data

Sources: `real-context-bench-v0.2/RESULTS.md`, `real-context-bench-v0.3/RESULTS.md`,
`real-context-bench-v0.4/RESULTS.md`, `real-context-bench-v0.5/RESULTS.md`.

| Run | Data | Token reduction | Blind audit (needed or unsure / judged) | Result |
| :--- | :--- | ---: | :--- | :--- |
| v0.2 | Claude frozen 60 | .386 | none; probes copy the policy's own predicates | not evidence of safety |
| v0.3 | Claude, 60 more items | .350 | 7/150 | review FAIL: shares all 25 sessions with v0.2 |
| v0.4 | Codex held-out 60 | .641 | 21/150 (upper .2046 vs .10) | FAIL |
| v0.5 | Codex test 60, after tuning on dev | .191 | 36/132 (upper .3543 vs .10) | FAIL |

Most flags were trims of large old command outputs whose removed middle held something the
next request used. `lossy` stays experimental and is not recommended.

## 3. Against Jev, Laya and mechanical

Source: `real-context-bench-v0/RESULTS.md`, `real-context-bench-v0.1/RESULTS.md`,
`real-context-bench-v0.2/RESULTS.md` (same 60 real Claude contexts (items), 49 with an applicable probe).
Lossless row: `lossless-dedup-v1/RESULTS.md`.

| Arm | Item retention | Pooled token reduction | Median latency | Cost |
| :--- | :--- | ---: | ---: | :--- |
| full context | 49/49 | 0 | — | — |
| mechanical | 48/49 | .0001 | 0.7 s | $0 |
| specialist (shadow, never drops) | 49/49 | 0 | 24.6 s | $0 |
| Jev, block mode | 47/49 | .035 | 14.0 s | $43.52 for 3,226 calls |
| Jev, transcript mode | 30/49 | .252 | — | invalid run: 102 of 379 requests failed |
| rules `lossless` | 49/49 | .0034 | ms | $0 |
| rules `lossy` (pre-fix policy) | 49/49 | .386 | 7 ms | $0, but see section 2 |

- **Jev block mode cuts 3.5% on real sessions**, against about 50% on curated corpora
  ([JEV.md](JEV.md)). LCC's protection and block policy keep nearly everything whatever the
  provider.
- The transcript-mode Jev run hit the API's real token limit on 77 requests; PR #44 fixed
  the request fitting, but the arm was not re-run.
- **Laya was not run on real sessions.** No receipt exists, so no claim is made.
- Probes check that a needed block survives verbatim, not that the next answer is correct,
  and most of them mirror the rules' own predicates. Retention columns are weak evidence; the
  blind audits in section 2 are the primary safety evidence.

## 4. What "real requests" means here

Sources: `genuine-requests-v1/README.md`, `request-taxonomy-v1/README.md`,
`human-review-v2-pt/RESULTS.md`.

- 3,782 redacted prompts the owner typed into seven coding agents (1,045 sessions; 2,748
  Portuguese, 940 English), imported with LCC's own `request_log.redact`. They never leave
  `~/.lcc`.
- A 13-category catalog keyed on *what context the agent needs*, built from a 619-prompt
  sample. Owner confirmation: 41/56 on hard items (v1), then 26/27 on held-out
  high-confidence items (v2).
- The earlier authored corpus scored 25/60 with the same owner, which is why it was dropped
  as the target.

## 5. Opt-in request log

`LCC_REQUEST_LOG=1` makes the CLI and MCP server append one line per request to
`./.lcc/request-log/requests.jsonl` (`LCC_REQUEST_LOG_DIR` moves it). Each line holds a random
12-hex entry `id`, the redacted goal, a language guess, a salted session hash, the UTC date,
the entry point, the redacted `field` value (or `"none"`), the LCC version and the redaction
ruleset hash. Context is never logged. It is off by default.
`lcc request-log` counts entries and `--delete ID` removes one. See
[`src/lcc/request_log.py`](../src/lcc/request_log.py) and [PRIVACY_POLICY.md](PRIVACY_POLICY.md).

This log is how recoverable disk mode and future policies can be measured on live use
instead of on recorded transcripts.

## 6. Open gaps

Source: `PLAN.md` (section "Desfecho 2026-10-11") and the files cited above.


- Disk-mode recoverable has no measurement yet.
- Range and command pointers have no blind audit.
- Every audit used LLM judges; none was the session owner.
- No learned local model passed out-of-training checks, so none is used here.
