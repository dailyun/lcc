<div align="center">

# lcc — Local Context Compiler

**Shrink what you send to the model. Keep every byte you didn't cut. Get a receipt.**

[![PyPI](https://img.shields.io/pypi/v/local-context-compiler.svg)](https://pypi.org/project/local-context-compiler/)
[![Python](https://img.shields.io/pypi/pyversions/local-context-compiler.svg)](https://pypi.org/project/local-context-compiler/)
[![CI](https://github.com/lucasmartins-ai/lcc/actions/workflows/ci.yml/badge.svg)](https://github.com/lucasmartins-ai/lcc/actions/workflows/ci.yml)
[![MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![GitHub stars](https://img.shields.io/github/stars/lucasmartins-ai/lcc?style=social)](https://github.com/lucasmartins-ai/lcc/stargazers)

<img src="https://raw.githubusercontent.com/lucasmartins-ai/lcc/main/demos/session.gif" alt="lcc compacting a 50-message Claude Code debugging session offline with --provider rules --rules-mode stashed: 9,211 to 6,634 tokens, every user and assistant message byte-identical, five old file reads replaced by lcc recall pointers, lcc explain listing why each one went, and lcc recall returning a removed output byte for byte" width="900">

</div>

<!-- mcp-name: io.github.lucasmartins-ai/lcc -->

`lcc` cuts what an agent or a prompt does not need from its context, keeps everything else
**byte for byte**, and writes a receipt saying what went and why. Nothing is summarized or
rewritten. It runs locally on your CPU, with no API key by default.

## Claude Code: `/compact` without the summary

- **Before:** Claude Code's `/compact` replaces the history with a model-written summary. What
  the summary leaves out is no longer in the context.
- **After, with the plugin:** the history stays as it was, user and assistant text byte for
  byte; only spent tool calls and results are dropped or trimmed, each with a reason in the
  report. If a pass fails or removes less than 25%, the plugin hands back to the built-in
  summary.

One real Claude Code session in this repository went from **55,411 to 18,761 tokens
(−66.1%)** with all 42 user/assistant texts byte-identical. That is **n=2** real sessions (the
other, a 337-message Hermes session, −81.7%), measured with the opt-in Jev judge; the plugin's
default local ranker, `specialist-v3`, was not part of that run
([REAL_SESSIONS.md](benchmarks/research/REAL_SESSIONS.md)). Do not expect that everywhere: the
`rules` modes, which remove only provable duplicates, cut 0.2–1.9% pooled on sets of real
sessions ([real-session benchmarks](docs/REAL_SESSION_BENCHMARKS.md)).

Install. Requires **Claude Code 2.1.274+** and `CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1`
(function hooks are early access):

```sh
export CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1
pip install "local-context-compiler[tiktoken]"   # the plugin calls the `lcc` CLI
claude plugin marketplace add lucasmartins-ai/lcc && claude plugin install lcc@lcc
```

Allow `mcp__plugin_lcc_lcc__compact_transcript` when asked, or the built-in summary runs. Options,
fallbacks and limits: [Claude Code guide](docs/CLAUDE_CODE.md).

## Quick start (CLI and MCP)

```sh
pip install "local-context-compiler[tiktoken]"   # the CLI is `lcc`

lcc compact notes.md -q "what is wrong with the booking flow?" \
  --provider mechanical -o compacted.md -r report.json
lcc explain report.json --source notes.md        # why each block was kept or dropped

lcc compact transcript.json -q "finish the parser fix" --mode tool-calls \
  -o compacted.json -r report.json               # agent transcripts
```

**MCP** (Claude Code, Cursor, Claude Desktop, any stdio client):

```json
{"mcpServers": {"lcc": {"command": "lcc", "args": ["mcp"]}}}
```

Tools: `compact`, `compact_transcript`, `inspect`, `prepare`, `explain`, `intake`
([MCP guide](docs/MCP.md)).

**Why lcc**

- **Local.** The default judges are plain Python on your CPU. The transcript ranker
  (`specialist-v3`) is a 183 KB file that ships in the package; no network, no external model.
- **Fast.** A 300-block agent transcript compacts in about 0.2 s; the ranker alone takes about
  0.1 s ([benchmark](docs/BENCHMARKS.md)).
- **Auditable.** Kept spans are byte-identical to the source, and every pass writes a JSON
  report with the reason for each drop.

## Providers

| Provider | Runs | When it is used | Report says |
| --- | --- | --- | --- |
| `specialist-v3` | local, CPU | default (`auto`) whenever transcript blocks are present: the Claude Code hook, `compact_transcript`, `--mode tool-calls` | `semantic_guarantee: none` (a rank, not a judgment) |
| `mechanical` | local, CPU | `--provider mechanical`; plain text under `auto` (a configured TypeSafe key does not change that unless `LCC_AUTO_REMOTE=1`) | `semantic_guarantee: none` |
| `rules` (`lossless`, `recoverable`, `stashed`) | local, no model | `provider: rules` for transcripts | `lossless`: only provable duplicates removed; `recoverable`: also outputs that can be re-read; `stashed` (recommended): removed outputs stay byte-recoverable through `recall` |
| `jev` | remote (TypeSafe API) | opt-in: `--provider jev` / `provider: jev`, needs `TYPESAFE_API_KEY` | semantic judgment or explicit degradation |
| `laya` | local model | deprecated; runs only when named | named model and fallback state |

`specialist-v3` details and limits: [SPECIALIST.md](docs/SPECIALIST.md). Rules modes:
[TOOL_CALLS.md](docs/TOOL_CALLS.md). Jev is an optional path you name explicitly
(`--provider jev`); it sends the objective and context blocks to the TypeSafe API, and
`LCC_DISABLE_NETWORK=1` disables it. `auto` never picks it on its own: having a key is not
consent to send context off-machine, so plain text under `auto` stays on `mechanical` unless
you set `LCC_AUTO_REMOTE=1`. `compact` writes a `relevance-compaction-1.2` report;
check `provider_used` and `degraded` before trusting the output.

## Benchmarks

**Synthetic transcript A/B** against a reference implementation of the same drop-the-spent-calls
idea (`fast-jev-compaction`), three sessions, one seed: lcc removed 13.3 / 39.2 / 55.7% at fact
recall 1.00; the reference removed 70.5 / 78.9 / 83.4% at recall 0.00, losing every evidence
pair ([TRANSCRIPT_AB.md](benchmarks/research/TRANSCRIPT_AB.md)).

**Real sessions, ranker only (held-out, INFERIDO).** A one-shot final test on 193 items from 36
of the maintainer's sessions that started after the training cutoff. Recall AUC is the share of
labelled evidence a ranker keeps as it is forced to drop more (0–60% of candidate tokens),
averaged into one number; all-kept AUC counts an item only when all of its evidence survives.

| ranker | recall AUC | all-kept AUC |
| --- | ---: | ---: |
| specialist-v3 | **0.899** | **0.434** |
| mechanical | 0.829 | 0.238 |
| recency | 0.818 | 0.306 |
| bm25 | 0.803 | 0.270 |
| random | 0.689 | 0.138 |

The labels are next-action evidence from one maintainer's Claude Code sessions, which favours
v3's training target. The sessions are private; only aggregates are published
([SPECIALIST.md](docs/SPECIALIST.md)). Receipts for the real-session numbers live in a private
repository, so published aggregates are owner-attested until a public dataset exists.

**Synthetic, reproducible** (`python benchmarks/run_judge_bench.py`, about 20 s, no network).
Nine hand-written edge cases: supersession, contradiction, config change, exception rule,
pt/en mixed, prompt injection in a tool output, large git diff, old root cause, re-read.

| raw ranking | specialist-v3 | mechanical | bm25 | recency |
| --- | ---: | ---: | ---: | ---: |
| cases with every must-keep block kept at a 30 / 50 / 70% cut (of 9) | 8 / 7 / 7 | 9 / 9 / 9 | 9 / 9 / 7 | 8 / 6 / 3 |

| full pass, default settings | must-keep kept | 302 blocks | 1,138 blocks | peak RSS |
| --- | ---: | ---: | ---: | ---: |
| `specialist-v3`, tool-call mode | 9/9 | 181 ms | 3.4 s | 71 MB |
| `rules` lossless | 9/9 | 2 ms | 16 ms | 55 MB |

These cases are lexically easy, so the lexical baselines do well; v3 misses the Portuguese
case. Timings are from macOS arm64 under heavy machine load. Method, every arm and the limits:
[BENCHMARKS.md](docs/BENCHMARKS.md). Below a 25% gain, the report marks a pass as not worth it.

## Share your numbers

Ran lcc on your own sessions? Open a
[results issue](https://github.com/lucasmartins-ai/lcc/issues/new?template=results.yml) with
counts only: version, provider, tokens before and after, `recall` calls, and whether the agent
lost anything. Never paste session text. Numbers from other agents and other people's
workloads are the evidence this project is missing.

## What it does not do

- It does not check that a model will answer correctly. A structural `PASS` is not an answer
  quality result.
- No RAG, embeddings or vector store.
- Protection rules and bounded restoration reduce, but do not rule out, lost evidence; see the
  [safety model](docs/adr/0014-minimum-sufficient-context.md).
- `specialist-v3` was trained on Claude Code transcripts. Other agents and plain documents are
  out of distribution; plain text uses `mechanical` unless a TypeSafe key is configured.

## Docs

| Topic | Link |
| --- | --- |
| Five-minute tour of the CLI | [QUICKSTART.md](docs/QUICKSTART.md) |
| Claude Code plugin | [CLAUDE_CODE.md](docs/CLAUDE_CODE.md) |
| MCP server | [MCP.md](docs/MCP.md) |
| Tool-call compaction and rules modes | [TOOL_CALLS.md](docs/TOOL_CALLS.md) |
| The local ranker | [SPECIALIST.md](docs/SPECIALIST.md) |
| Benchmarks | [BENCHMARKS.md](docs/BENCHMARKS.md), [REAL_SESSION_BENCHMARKS.md](docs/REAL_SESSION_BENCHMARKS.md), [benchmarks/](benchmarks/README.md) |
| Python API (`lcc.msi.compile`) | [msi-api.md](docs/lcc/msi-api.md) |
| Architecture and decisions | [architecture.md](docs/architecture.md), [ADR 0010](docs/adr/0010-deterministic-first-preparation-model-assistance.md), [ADR index](docs/adr/README.md) |
| Opt-in and deprecated providers | [JEV.md](docs/JEV.md), [LAYA.md](docs/LAYA.md) |
| Research archive | [paper.md](research/paper.md), [results-master.md](research/results-master.md), [TRANSCRIPT_AB.md](benchmarks/research/TRANSCRIPT_AB.md) |
| Privacy | [PRIVACY_POLICY.md](docs/PRIVACY_POLICY.md) |

## Development

Python 3.11+.

```sh
python -m pip install -e '.[dev,tiktoken]'
pytest -q && ruff check . && mypy
npm test                                   # Node API and hook tests
python benchmarks/run_judge_bench.py --check --out /tmp/judge-bench
```

The PyPI package named `lcc` is unrelated; this one is `local-context-compiler`.
Contributing: [CONTRIBUTING.md](CONTRIBUTING.md), starter tasks in
[good-first-issues.md](docs/contributing/good-first-issues.md). Security: [SECURITY.md](SECURITY.md).
Changes: [CHANGELOG.md](CHANGELOG.md).

## License

[MIT](LICENSE). The optional Laya integration carries Apache-2.0 attribution in
[NOTICE](NOTICE). Maintained by [LookADev](https://lookadev.com).
