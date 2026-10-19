<div align="center">

# lcc — Local Context Compiler

**Shrink what you send to the model. Keep every byte you didn't cut. Get a receipt.**

[![PyPI](https://img.shields.io/pypi/v/local-context-compiler.svg)](https://pypi.org/project/local-context-compiler/)
[![Python](https://img.shields.io/pypi/pyversions/local-context-compiler.svg)](https://pypi.org/project/local-context-compiler/)
[![CI](https://github.com/lucasmartins-ai/lcc/actions/workflows/ci.yml/badge.svg)](https://github.com/lucasmartins-ai/lcc/actions/workflows/ci.yml)
[![MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![GitHub stars](https://img.shields.io/github/stars/lucasmartins-ai/lcc?style=social)](https://github.com/lucasmartins-ai/lcc/stargazers)

<img src="https://raw.githubusercontent.com/lucasmartins-ai/lcc/main/demos/compact.gif" alt="lcc compact dropping noise from a 23-block dossier, then lcc explain showing why each block went" width="820">

</div>

`lcc` decides which parts of a context to send to a model and drops the rest. It works on
documents, logs and agent transcripts: duplicates and boilerplate go, blocks that do not serve
the objective are dropped or trimmed, and everything kept stays **verbatim**. Nothing is
summarized or rewritten. Every pass writes a JSON report that says what was kept, trimmed or
dropped, and why.

**Why lcc**

- **Local.** The default judges run on your CPU in plain Python. No API key, no network, no
  external model. The transcript ranker (`specialist-v3`) is a 183 KB file that ships in the
  package.
- **Fast.** A 300-block agent transcript compacts in about 0.2 s; the ranker alone takes about
  0.1 s ([benchmark](docs/BENCHMARKS.md)).
- **Auditable.** Kept spans are byte-identical to the source, and `lcc explain` gives the
  reason for every drop.

## Quick start

```sh
pip install "local-context-compiler[tiktoken]"   # the CLI is `lcc`

lcc compact notes.md -q "what is wrong with the booking flow?" \
  --provider mechanical -o compacted.md -r report.json
lcc explain report.json --source notes.md        # why each block was kept or dropped
```

**Claude Code plugin.** Replaces Claude Code's `/compact` summary with verbatim tool-call
compaction, ranked locally by `specialist-v3`. Function hooks are early access, so the flag is
required:

```sh
export CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1        # Claude Code 2.1.274+
claude plugin marketplace add lucasmartins-ai/lcc
claude plugin install lcc@lcc
```

When a pass fails or removes too little, the plugin hands back to the built-in summary. See
the [Claude Code guide](docs/CLAUDE_CODE.md).

**MCP** (Claude Code, Cursor, Claude Desktop, any stdio client):

```json
{"mcpServers": {"lcc": {"command": "lcc", "args": ["mcp"]}}}
```

Tools: `compact`, `compact_transcript`, `inspect`, `prepare`, `explain`, `intake`
([MCP guide](docs/MCP.md)).

**Agent transcripts from the CLI:**

```sh
lcc compact transcript.json -q "finish the parser fix" --mode tool-calls \
  -o compacted.json -r report.json
```

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

**Real sessions (held-out, INFERIDO).** A one-shot final test on 193 items from 36 of the
maintainer's sessions that started after the training cutoff. Retention AUC over drop budgets
of 0–60% of candidate tokens:

| ranker | recall AUC | all-kept AUC |
| --- | ---: | ---: |
| specialist-v3 | **0.899** | **0.434** |
| mechanical | 0.829 | 0.238 |
| recency | 0.818 | 0.306 |
| bm25 | 0.803 | 0.270 |
| random | 0.689 | 0.138 |

The labels are next-action evidence from one maintainer's Claude Code sessions, which favours
v3's training target. The sessions are private; only aggregates are published
([SPECIALIST.md](docs/SPECIALIST.md)).

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
[BENCHMARKS.md](docs/BENCHMARKS.md). Rules modes on real sessions:
[REAL_SESSION_BENCHMARKS.md](docs/REAL_SESSION_BENCHMARKS.md).

Reduction depends on how much of a context is noise. Below a 25% gain, the report marks the
pass as not worth it, and the Claude Code plugin keeps the built-in summary.

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
Contributing: [CONTRIBUTING.md](CONTRIBUTING.md). Security: [SECURITY.md](SECURITY.md).
Changes: [CHANGELOG.md](CHANGELOG.md).

## License

[MIT](LICENSE). The optional Laya integration carries Apache-2.0 attribution in
[NOTICE](NOTICE). Maintained by [LookADev](https://lookadev.com).
