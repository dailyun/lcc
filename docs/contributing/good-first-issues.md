# Good first issues (drafts)

Five starter tasks, written so the maintainer can paste each one into a GitHub issue with the
`good first issue` label. Each names the gap as it stands in the code or docs today, so check
that it still holds before you start.

General rules for all of them: no session text, real paths or ids in fixtures (synthetic data
only, see [SECURITY.md](../../SECURITY.md)); `pytest -q`, `ruff check .` and `npm test` must
pass; docs must not describe anything as implemented before it is.

---

## 1. Convert Codex session logs into the tool-call transcript format

**Body.** `lcc compact --mode tool-calls` takes a plain JSON transcript (`messages` with
`toolUses` / `toolResults`, see [TOOL_CALLS.md](../TOOL_CALLS.md), section 1), but the only
converters in the repo read Claude Code and Hermes sessions:
`benchmarks/research/measure_session.py` has `from_claude_code` and `from_hermes`, and no
Codex reader. As a result, Codex numbers are thin: in the `specialist-v3` held-out test only 3
of 193 items come from Codex ([SPECIALIST.md](../SPECIALIST.md), "Measured results"). The rules
policy already understands some Codex output (it strips Codex exec headers), so the missing
piece is the input side.

**Acceptance criteria**

- A `from_codex(path)` reader next to the existing ones, returning the same
  `(messages, counts)` shape, with `--codex PATH` added to the script's source options.
- A small **synthetic** Codex-style fixture under `tests/` and a test that converts it and runs
  it through `lcc compact --mode tool-calls` without errors.
- One paragraph in [TOOL_CALLS.md](../TOOL_CALLS.md) on how to feed a Codex session in, stating
  that `specialist-v3` is out of distribution there.

## 2. More non-English cases in the judge bench

**Body.** `benchmarks/run_judge_bench.py` has one non-English case, `pt_en_mixed` (a Portuguese
answer to an English question). `specialist-v3` misses it at every cut
([BENCHMARKS.md](../BENCHMARKS.md), "How to read it"), and the README says so. One case is not
enough to tell a language problem from a one-off. Add a few more so the gap is measured, not
anecdotal. This issue is about measuring, not fixing the ranker.

**Acceptance criteria**

- At least three new cases in the existing case format, each with an English objective and the
  must-keep line in another language (for example Portuguese with no shared words, Spanish,
  and an English question with a mixed-language tool output).
- `python benchmarks/run_judge_bench.py --check` still passes; if a floor has to move because
  the case count changed, say why in the PR.
- The edge-case table and the "How to read it" note in [BENCHMARKS.md](../BENCHMARKS.md)
  updated with the new counts for every arm, including where `specialist-v3` loses.

## 3. Document what the Node package does and does not do

**Body.** The npm package `local-context-compiler` exports `compressContext`, `LccCompressor`,
`LccIntake`, `buildPrompt`, `estimateTokens` and related helpers (`index.js`, `index.d.ts`).
It has no `compact`, `explain` or `recall`; those exist only in the Python package and its MCP
server. No document says this, and [roadmap.md](../roadmap.md) does not list Node parity either,
so a Node user cannot tell what they are getting.

**Acceptance criteria**

- A short `NODE.md` in `docs/`: install, the exported functions with one example each, and a table
  of Python features with "available in Node: yes / no".
- It states plainly that relevance compaction (`compact`, `--mode tool-calls`), `explain` and
  `recall` are Python-only, and points Node users at `lcc mcp` instead.
- Linked from the README Docs table; `tests/test_docs.py` passes (it checks that `docs/*.md`
  links resolve).

## 4. Make the package descriptions match what lcc is today

**Body.** The README describes lcc as context compaction with verbatim output and a receipt.
The package metadata still uses older wording: `pyproject.toml` says "Deterministic,
local-first context optimization for LLM prompts: clean, dedupe, structure, and measure token
and cost savings before calling a large model", and `package.json` says "Deterministic,
local-first context optimization for LLM prompts & AI agents". The default transcript path now
uses a learned ranker, so "deterministic" no longer describes the whole package, and these
strings are what PyPI and npm show.

**Acceptance criteria**

- New `description` strings in `pyproject.toml` and `package.json` that match the README's
  first paragraph, with no claim the docs do not support.
- `package.json` keywords reviewed against `pyproject.toml` keywords so both registries list
  the same topics.
- A CHANGELOG `[Unreleased]` line.

## 5. Bring the bug and documentation issue templates up to date

**Body.** `.github/ISSUE_TEMPLATE/bug_report.yml` asks for "OS, Python/Node/Go version, Docker
version" with a `v1.0.0` placeholder, and asks for nothing lcc-specific. The fields that make a
compaction bug reproducible are in the report: `provider_used`, `degraded`, and the version
from `lcc --version`. `documentation.yml` uses "README.md → 'MVP scope'" as its example, a
heading the README no longer has.

**Acceptance criteria**

- `bug_report.yml`: a required `lcc --version` field, a `provider_used` / `degraded` field, an
  optional Claude Code version field for plugin bugs, and a reminder to share counts and
  report fields, not session text. Drop the Go and Docker mentions unless they apply.
- `documentation.yml`: the placeholder points at a heading that exists.
- Both forms still render (valid YAML, GitHub issue-form schema).
