# Claude Code plugin (`lcc` on the Claude Code marketplace)

`lcc` ships a Claude Code plugin that replaces the built-in compaction **summary** with
verbatim compaction: the session transcript goes to the plugin's own `lcc mcp` server, the
local `specialist-v3` ranker says which tool calls and results are spent, and those pairs are
dropped or trimmed. Kept messages carry
their original bytes; user and assistant text is never scored, trimmed or rewritten. The
decision logic, the report schema and the fallback rules are the ones documented in
[`docs/TOOL_CALLS.md`](TOOL_CALLS.md) for `lcc compact --mode tool-calls`.

## Install

```bash
export CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1   # required while function hooks are early access

pip install "local-context-compiler[tiktoken]"   # the `lcc` CLI and its MCP server

claude plugin marketplace add lucasmartins-ai/lcc
claude plugin install lcc@lcc
```

The hook reaches its server through `$.mcp.call`, which goes through tool permissions: allow
`mcp__plugin_lcc_lcc__compact_transcript` (in `permissions.allow`, or answer the prompt), or the
call is refused and the built-in summary runs.

Requirements: Claude Code **2.1.274 or newer** and `lcc` on `PATH`. The default judge
(`provider: auto`, i.e. `specialist-v3`) runs locally with no key and no network
([details](SPECIALIST.md)). `provider: rules` is the offline deterministic policy. Jev is
opt-in (`provider: jev`, needs `TYPESAFE_API_KEY`); Laya is deprecated and runs only when named.
Against a checkout, without installing:

```bash
CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1 claude --plugin-dir plugins/claude-code
```

## Layout

| Path | What it is |
| :--- | :--- |
| `.claude-plugin/marketplace.json` | the marketplace this repository hosts (name `lcc`) |
| `plugins/claude-code/.claude-plugin/plugin.json` | the plugin manifest and its configurable options |
| `plugins/claude-code/.mcp.json` | starts the plugin's own `lcc mcp` server |
| `plugins/claude-code/hooks/hooks.json` | registers the hook module and the two classic hooks |
| `plugins/claude-code/hooks/lcc-compact.mjs` | the adapter: `session.compact` → `compact_transcript` → messages |
| `plugins/claude-code/hooks/map.mjs` | the pure mapping helpers, unit-tested on their own |
| `plugins/claude-code/scripts/lcc-pin.mjs` | the classic-hook fallback: `PreCompact` instructions and `SessionStart` (`compact`) re-pin |
| `plugins/claude-code/hooks/README.md` | operator guide: what runs, what falls back, what is limited |

The hook is plain ESM with no build step and no dependencies: the Python package does the
work, the plugin translates.

## Options

Set through `/plugin` → lcc, or `claude plugin install lcc@lcc --config key=value`
(defaults in parentheses):

| Option | Meaning |
| :--- | :--- |
| `question` (`""`) | the objective; empty uses the text after `/compact`, or the newest user prompts |
| `threshold` (`0.5`) | below this keep-probability a call or result becomes a candidate for removal |
| `preserveRecentMessages` (`6`) | newest messages never scored, never mutated |
| `trimHeadChars` (`300`) | characters kept from a spent result whose call still matters |
| `maxStateTokens` (`25000`) | token ceiling for the conversation shown to the judge |
| `maxRequestTokens` (`30000`) | ceiling for state plus one batch of questions per request |
| `minReductionRatio` (`0.25`) | below this share removed, the plugin keeps the built-in summary |
| `server` (`plugin:lcc:lcc`) | MCP server name to call, as Claude Code names the plugin's own |

## Without the early-access flag or a key: pinned facts in the built-in summary

Two classic hooks ship in the same `hooks/hooks.json` and run on any Claude Code with plugin
hooks, with no `CLAUDE_CODE_ENABLE_FUNCTION_HOOKS`, no judge key and no network:

| Hook | What it adds |
| :--- | :--- |
| `PreCompact` | stdout becomes extra instructions for the built-in summary: the session objective (the two newest user prompts), the files edited and read, and the last tool error, with the order to copy file paths, identifiers and error text verbatim |
| `SessionStart`, matcher `compact` | the same facts, injected into the fresh context after the summary |

Both read the facts from the hook's `transcript_path` (the session JSONL, which compaction
appends to and does not rewrite), so they agree and nothing is stored between them. Lists are
capped at 20 files and the error at 400 characters. Anything unreadable prints nothing and exits
0, so compaction runs exactly as before. Covered by `node test/hook-pin.test.mjs`.

## Fallback: the built-in summary runs whenever anything is off

No key, a degraded pass, a conversation that cannot fit the state budget, a pass below
`minReductionRatio`, content the hook cannot rebuild (image or attachment blocks), or any MCP
failure — each of those calls `next(event)`, so Claude Code compacts exactly as it did before.
The plugin also never requests a compaction of its own; it only decides what a compaction
produces. Every outcome is one log line plus one toast, e.g.:

```
lcc: verbatim compaction, no summary (kept 18 messages, dropped 7 tool call(s), trimmed 2)
lcc: built-in summary (below the 25% minimum (4.2%))
lcc: built-in summary (judge unavailable (jev_unavailable_fail_safe))
```

## What is verified, and what is not

Verified: `claude plugin validate plugins/claude-code` passes and reports the registered
surface (`session.compact`; calls `$.mcp.call`, `$.ui.log`, `$.ui.toast`); the plugin installs
from this repository's marketplace with the documented commands (`claude plugin marketplace add
<repo>` → "Successfully added marketplace: lcc", `claude plugin install lcc@lcc` → installed at
user scope with its eight `userConfig` options reported), and `claude plugin details lcc` lists
the plugin's MCP server. The hook's mapping logic — option coercion, transcript mapping,
tool-result parsing, the replacement decision and the fallback on unsupported content — is
covered by `node test/hook-map.test.mjs` (15 cases, run by `npm test` and by CI); the Python side
is covered by `tests/test_transcript_compaction.py`, `tests/test_cli_tool_calls.py` and
`tests/test_mcp_server.py`.

> **`claude plugin details` counts only classic event hooks**, not the `session.compact`
> module (it reported "Hooks (0)" before the two fallback hooks existed). `claude plugin
> validate` parses the module and names the event it registers; the debug log shows
> `hooks module lcc@... loaded ... events: session.compact` at startup.

The compaction backend itself is now measured against the reference implementation on the same
transcripts: our mode removed **13.3 / 39.2 / 55.7%** of the transcript at fact recall
**1.00 / 1.00 / 1.00**, where `fast-jev-compaction` removed 70.5 / 78.9 / 83.4% at recall 0.00
— it dropped every evidence pair (`benchmarks/research/TRANSCRIPT_AB.md`, canonical row in
`RESEARCH_STATUS.md`). Read that study with its own limits: three synthetic sessions, one seed,
backends only, and one of four runs dropped an evidence pair on the largest session.

And it is measured on **real transcripts** (`benchmarks/research/REAL_SESSIONS.md`): a real
Claude Code session in this repository, in the exact shape the hook receives, went from 55 411
to 18 761 tokens (**−66.1%**) with all 42 user/assistant texts preserved byte for byte, and this
release's own 337-message Hermes session by 81.7% with all 58 texts preserved. That is **n=2**;
on 60 real Claude Code contexts Jev block mode cut 3.5%
([real-session benchmarks, section 3](REAL_SESSION_BENCHMARKS.md#3-against-jev-laya-and-mechanical)). That measurement
found and fixed a real defect: the state budget used a heuristic that undercounts JSON by about
a third, so large sessions built requests the API refused (`400 max_tokens_exceeded`) and
degraded to keep-everything after 38 wasted calls. It also exercised the 25% minimum-reduction
gate for real: one measured window landed below it and the hook would keep the built-in summary.

### Observed end to end (Claude Code 2.1.296, 2026-10-16)

The first real `/compact` runs through this plugin, from `claude -p` on a throwaway session
(one synthetic workspace: 4 file reads of about 6.5K characters each, 2 Bash calls, 1 Edit,
1 Grep). Counts only; one session shape, so read them as a smoke test, not a benchmark.

What the shipped hook did on the first run, and what was fixed:

| Run | Observed | Fix in this branch |
| :--- | :--- | :--- |
| 1 | `session.compact` fired (trigger `manual`, 16 messages, keys `role`, `text`, `toolUses`, `toolResults`, `handle`) and the hook threw `ReferenceError: process is not defined`: hook modules run in a worker with no `process`, and the event carries no `cwd`. The engine skipped the hook and its own summary ran. | `sessionCwd()` uses `globalThis.process?.cwd?.()`; no `workspace_root` is sent when there is none |
| 2 | `$.mcp.call` found no server named `lcc`: Claude Code names the plugin's server `plugin:lcc:lcc` | default `server` is `plugin:lcc:lcc` (manifest and `map.mjs`) |
| 3 | the call was refused until `mcp__plugin_lcc_lcc__compact_transcript` was allowed (`$.mcp.call` goes through tool permissions; headless `-p` cannot prompt) | documented below; allow it once |
| 4 | replacement accepted (receipt below), but a failed tool result came back as a success: the engine rebuilds `is_error` from `isError`, the payload said `is_error` | `toSessionMessages` emits `isError`; after the fix the run kept 1 of 1 error flags |

The receipt of the first accepted replacement (default provider `jev`; `lcc` found its key in
its own key store): judged by `jev-1.13.0`, 1 call, 329 ms, not degraded; 7 tool calls → 1 kept,
2 trimmed, 3 dropped; 29,039 → 2,845 characters (−90.2%); 20 messages in, 11 out. Claude Code
logged `a hook's 11 messages stand ... core never ran`, and the `compact_boundary` recorded
58,429 → 2,324 tokens in 482 ms, against 15–17 s for the built-in summary on the same session
shape. Two runs of the plugin directory from this branch kept 12 and 10 messages.

**Claude Code's post-compact re-attachments do not happen when the hook replaces messages.**
After the boundary, the hook path had 0 re-read files and 0 tool/agent/MCP/environment/model
deltas; only the 3 `SessionStart` hook outputs. The built-in path on the same session had 4
re-read files, 5 deltas (deferred tools, agent listing, MCP instructions, environment, model)
and the hook outputs. `PreCompact` does not run on the hook path (0 invocations) and does run on
the built-in path. Kept messages are verbatim, so a kept file read is still in context; a
dropped or trimmed one is not re-attached, and `lcc`'s `SessionStart` re-pin lists its path.

Two more observations: the hook module loaded with `CLAUDE_CODE_ENABLE_FUNCTION_HOOKS` unset
and with it set to `0` on this 2.1.296 install, so the variable was not the gate here (it may
be on other accounts or builds). And the classic fallback changed the built-in summary: with
the `PreCompact` pin it carried 4 of the 4 absolute file paths, without the plugin 0 of 4
(n=1 each; both kept the edited function name and the error text).

To repeat it:

```bash
cd <a throwaway repo> && CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1 claude --plugin-dir <lcc>/plugins/claude-code \
  --allowedTools mcp__plugin_lcc_lcc__compact_transcript --debug-file /tmp/lcc-compact.log
# after a few tool calls:  /compact
grep -E "\[lcc\]|session\.compact \(" /tmp/lcc-compact.log
```

Note that Claude Code is a *consumer*, not a requirement: the same pass runs from the CLI
(`lcc compact transcript.json --mode tool-calls`) and from any MCP client through `lcc mcp`
(`compact_transcript`), with no editor involved.

## Early-access caveat

Function hooks (`hooks/hooks.json` modules, the `session.compact` event, the `$` engine
surface) are early access: they require `CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1` and a Claude
Code release that carries the surface (2.1.274+). The API can change between releases; this
plugin is written against the declarations Claude Code 2.1.274 generated, and
`claude plugin validate` is the check to re-run after an upgrade.
