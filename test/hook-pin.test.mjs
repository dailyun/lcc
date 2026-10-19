// The classic-hook fallback: PreCompact instructions and SessionStart(compact) re-pin.
//
//   node test/hook-pin.test.mjs

import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import {
  extractFacts,
  precompactText,
  sessionStartText,
} from "../plugins/claude-code/scripts/lcc-pin.mjs";

const SCRIPT = fileURLToPath(new URL("../plugins/claude-code/scripts/lcc-pin.mjs", import.meta.url));

const user = (content, extra = {}) => ({ type: "user", message: { role: "user", content }, ...extra });
const assistant = (content) => ({ type: "assistant", message: { role: "assistant", content } });
const use = (id, name, input) => ({ type: "tool_use", id, name, input });
const result = (id, content, isError = false) => ({ type: "tool_result", tool_use_id: id, content, is_error: isError });

const LINES = [
  user("Fix the failing parse_header test in src/lcc/parser.py."),
  assistant([use("t1", "Read", { file_path: "/repo/src/lcc/parser.py" })]),
  user([result("t1", "1\tdef parse_header(raw):")]),
  assistant([use("t2", "Bash", { command: "pytest -q tests/test_parser.py" })]),
  user([result("t2", [{ type: "text", text: "E   KeyError: 'x-lcc-id'\nFAILED tests/test_parser.py::test_header" }], true)]),
  assistant([use("t3", "Edit", { file_path: "/repo/src/lcc/parser.py", old_string: "a", new_string: "b" })]),
  assistant([use("t4", "Write", { file_path: "/repo/tests/test_parser.py", content: "" })]),
  assistant([use("t5", "Edit", { file_path: "/repo/src/lcc/parser.py", old_string: "b", new_string: "c" })]),
  // Not prompts: hook/command wrappers, meta lines and an earlier compaction summary.
  user("<command-name>/compact</command-name>"),
  user("caveat", { isMeta: true }),
  user("This session is being continued from a previous conversation...", { isCompactSummary: true }),
  "not json",
  user("Also keep the CHANGELOG entry."),
];

const facts = extractFacts(LINES);
assert.equal(
  facts.objective,
  "Fix the failing parse_header test in src/lcc/parser.py.\nAlso keep the CHANGELOG entry.",
);
// Distinct, newest last; Write counts as an edit.
assert.deepEqual(facts.edited, ["/repo/tests/test_parser.py", "/repo/src/lcc/parser.py"]);
assert.deepEqual(facts.read, ["/repo/src/lcc/parser.py"]);
assert.equal(facts.lastError.tool, "Bash");
assert.match(facts.lastError.text, /KeyError: 'x-lcc-id'/);

const instructions = precompactText(facts);
assert.match(instructions, /verbatim/);
assert.match(instructions, /- \/repo\/src\/lcc\/parser\.py/);
assert.match(instructions, /Last tool error \(Bash\):\nE   KeyError: 'x-lcc-id'/);
assert.match(sessionStartText(facts), /^lcc: facts pinned from before compaction/);

// Nothing to pin: both hooks stay silent instead of adding an empty header.
const empty = extractFacts([]);
assert.equal(precompactText(empty), "");
assert.equal(sessionStartText(empty), "");

// Long errors are cut, so a 40K stack trace cannot flood the summary instructions.
const long = extractFacts([user([result("x", "y".repeat(5000), true)])]);
assert.equal(long.lastError.text.length, 400);
assert.equal(long.lastError.tool, "tool");

// End to end as Claude Code runs it: hook JSON on stdin, text on stdout.
const dir = mkdtempSync(join(tmpdir(), "lcc-pin-"));
const transcript = join(dir, "session.jsonl");
writeFileSync(transcript, LINES.map((line) => (typeof line === "string" ? line : JSON.stringify(line))).join("\n"));
const run = (mode, input) => execFileSync("node", [SCRIPT, mode], { input, encoding: "utf8" });
const hookInput = JSON.stringify({ hook_event_name: "PreCompact", trigger: "manual", transcript_path: transcript });
assert.equal(run("precompact", hookInput), `${instructions}\n`);
assert.equal(run("sessionstart", hookInput), `${sessionStartText(facts)}\n`);
// A missing transcript or bad stdin prints nothing and exits 0 (execFileSync throws otherwise).
assert.equal(run("precompact", JSON.stringify({ transcript_path: join(dir, "missing.jsonl") })), "");
assert.equal(run("precompact", "{"), "");
console.log("hook-pin: ok");
