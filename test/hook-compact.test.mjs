// Direct test of the hook module's cwd plumbing: the session cwd reaches workspace_root.
//
//   node test/hook-compact.test.mjs

import assert from "node:assert/strict";

import { register, sessionCwd } from "../plugins/claude-code/hooks/lcc-compact.mjs";

const TRANSCRIPT = [
  { role: "user", text: "Fix the failing parser test." },
  {
    role: "assistant",
    text: "Reading.",
    toolUses: [{ tool_use_id: "toolu_1", tool: "Read", input: { file_path: "/repo/p.py" } }],
  },
  { role: "user", text: "", toolResults: [{ tool_use_id: "toolu_1", text: "1\tx" }] },
];

async function argsSent(event, options) {
  let handler;
  register((name, fn) => { if (name === "session.compact") handler = fn; }, options);
  let sent = null;
  const $ = {
    ui: { log() {}, toast() {} },
    mcp: { call: async (_server, _tool, args) => { sent = args; throw new Error("stop"); } },
  };
  let fellBack = false;
  await handler($, { trigger: "manual", messages: TRANSCRIPT, ...event }, () => { fellBack = true; });
  assert.equal(fellBack, true, "a failed MCP call falls back to next(event)");
  return sent;
}

const RECOVERABLE = { provider: "rules", rulesMode: "recoverable" };

assert.equal((await argsSent({ cwd: "/session/repo" }, RECOVERABLE)).workspace_root, "/session/repo");
// No cwd on the event, or a relative one: the hook process's own cwd.
assert.equal((await argsSent({}, RECOVERABLE)).workspace_root, process.cwd());
assert.equal((await argsSent({ cwd: "rel" }, RECOVERABLE)).workspace_root, process.cwd());
assert.equal(sessionCwd({ cwd: "/x" }), "/x");
assert.equal(sessionCwd(undefined), process.cwd());
// Lossless and the default mode never send a workspace root.
assert.equal("workspace_root" in (await argsSent({ cwd: "/s" }, { provider: "rules", rulesMode: "lossless" })), false);
assert.equal("workspace_root" in (await argsSent({ cwd: "/s" }, {})), false);
console.log("hook-compact: ok");
