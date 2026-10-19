# Share your numbers (`lcc share-stats`)

Beta testers: one command turns your local compaction reports and request log into a short,
anonymous summary of counts you can paste into a GitHub issue. Nothing is sent anywhere.
The command reads local files and prints (or writes) the summary; you decide whether to paste
it.

```bash
# Reports written by `lcc compact -r` (either mode) plus the request log, if you enabled it:
lcc share-stats reports/*.json

# Only request-log entries from a given day on (reports carry no dates, so they all count):
lcc share-stats reports/*.json --since 2026-10-01

# Machine-readable, to a file:
lcc share-stats reports/*.json --json -o share-stats.json
```

| Option | Meaning |
| :--- | :--- |
| `REPORT...` | Report JSON files from `lcc compact -r` (`relevance-compaction-1.x` or `transcript-compaction-1.x`). Optional. |
| `--request-log DIR` | Request-log directory. Default: `LCC_REQUEST_LOG_DIR`, else `./.lcc/request-log`. The log only exists if you ran with `LCC_REQUEST_LOG=1`. |
| `--no-request-log` | Do not read the request log. |
| `--since YYYY-MM-DD` | Count only request-log entries on or after that day. |
| `--json` | Print the `lcc-share-stats-1.0` JSON instead of Markdown. |
| `-o, --output FILE` | Write the summary to FILE instead of stdout. |

Then open a new **"Share your results"** issue
(<https://github.com/lucasmartins-ai/lcc/issues/new/choose>) and paste the output.

## What is in it

- lcc version, OS family (`macOS`, `Linux`, `Windows` or `other`) and Python `major.minor`.
- From the reports: number of passes, counts by mode and by provider, degraded passes,
  fallback passes (`provider_used` is `degraded` or ends in `+mechanical_fallback` /
  `+partial_fail_safe`), passes under the 25% minimum-reduction gate, total tokens before and
  after (rounded to the nearest 100), p10 / median / p90 of the reduction ratio, and stashed
  tool outputs. Per-pass ratios (2 decimals, sorted, so not in file order) appear only from 5
  passes on.
- From the request log: entries, number of distinct active days, counts by entry point
  (`mcp:compact_transcript`, `cli:compact`, ...), by provider, degraded passes, idle-stash passes,
  their removed tokens (rounded to the nearest 100) and idle-gap buckets, and lcc versions.
  `recall` calls are not written to the request log by lcc 1.2.0, so that row reads `n/a`
  rather than a 0 the log cannot vouch for.
- How many inputs were skipped, by reason (`unreadable`, `not_json`, `not_a_report`,
  `unsupported_format`, `invalid_fields`, `request_log_line`).

## What is never in it

No session text, objectives or goals, file paths or file names, commands, ids, session hashes
or content hashes, usernames, hostnames, or any date finer than a day (only the `--since` day
you typed and a count of active days). Labels come from fixed allowlists: anything else, such
as a hand-edited provider name, is counted as `other`. A test feeds reports and a log full of
secrets, paths and ids and asserts none of those strings appears in either output.

Bad input never stops the command: an unreadable, corrupt, old-format or unrelated file is
skipped and counted, and the warning on stderr names only its position (`input 3 of 5`) and
the reason, never its contents.

## JSON schema `lcc-share-stats-1.0`

Top-level keys: `schema_version`, `tool_version`, `environment`, `inputs`, `reports`,
`request_log`. Keys are sorted and there is no timestamp, so the same inputs give
byte-identical output (ADR 0004). Additive fields keep `1.0`; removing or renaming one bumps
the major version.

## Sample

```markdown
## lcc share-stats (lcc-share-stats-1.0)

> Included: counts, token totals rounded to the nearest 100, reduction ratios rounded to 2 decimals, lcc version, OS family and Python version.
> Not included: session text, objectives or goals, file paths or names, commands, ids, hashes, usernames, hostnames, or any date finer than a day. Nothing was sent anywhere; read it before you paste it.

- lcc 1.2.0 · Linux · Python 3.13
- Since: 2026-10-01 (applies to the request log; reports carry no dates)
- Report files read: 5 · inputs skipped: 0

### Compaction reports

| metric | value |
| --- | --- |
| passes | 5 |
| by mode | blocks 3, tool-calls 2 |
| by provider | laya 1, laya+mechanical_fallback 1, mechanical 1, rules 1, specialist-v3 1 |
| degraded passes | 1 |
| fallback passes | 1 |
| passes under the 25% gate | 1 |
| tokens before (total, ~100) | 105500 |
| tokens after (total, ~100) | 49300 |
| reduction ratio p10 / median / p90 | 0.15 / 0.50 / 0.69 |
| per-pass reduction ratios | 0.03, 0.32, 0.50, 0.60, 0.75 |
| stashed tool outputs | 2 |

### Request log (LCC_REQUEST_LOG=1)

| metric | value |
| --- | --- |
| entries | 3 |
| active days | 2 |
| by entry point | cli:compact 1, mcp:compact_transcript 2 |
| by provider | rules 1, specialist-v3 1 |
| degraded passes | 0 |
| recall calls | n/a (not recorded by the log) |
| idle-stash passes | 1 |
| idle-stash removed tokens (total, ~100) | 10900 |
| idle gap buckets | 2h-8h 1 |
| lcc versions | 1.2.0 3 |

Paste this into a new "Share your results" issue: https://github.com/lucasmartins-ai/lcc/issues/new/choose
```

`share-stats` is a CLI command only; the MCP server does not expose it.
