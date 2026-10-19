// Idle stash (opt-in): the decision helpers and the hooks, against a fake `$`.
//
//   node test/hook-idle.test.mjs

import assert from "node:assert/strict";

import { register, stashIdle } from "../plugins/claude-code/hooks/lcc-compact.mjs";
import {
  IDLE_MARKER,
  estimateRemovableTokens,
  gapBucket,
  idleDecision,
  resolveOptions,
} from "../plugins/claude-code/hooks/map.mjs";

const ON = { idleStash: true, idleTtlSeconds: 60, idleMinRemovableTokens: 100, preserveRecentMessages: 1 };
const big = (id, chars) => ({ role: "user", text: "", toolResults: [{ tool_use_id: id, text: "x".repeat(chars) }] });
const TRANSCRIPT = [
  { role: "user", text: "Fix the parser." },
  { role: "assistant", text: "", toolUses: [{ tool_use_id: "t1", tool: "Read", input: { file_path: "/r/a.py" } }] },
  big("t1", 4000),
  { role: "assistant", text: "Done." },
];

// --- options and pure helpers -------------------------------------------------------------
const defaults = resolveOptions();
assert.equal(defaults.idleStash, false, "off by default");
assert.equal(defaults.idleTtlSeconds, 7200, "1-hour TTL + 60-minute keepalive window");
assert.equal(defaults.idleMinRemovableTokens, 10000);
assert.equal(defaults.idleMaxPassesPerSession, 8);
assert.equal(resolveOptions({ idleStash: "true" }).idleStash, true);
assert.equal(resolveOptions({ idleStash: "yes" }).idleStash, false);
assert.equal(resolveOptions({ idleTtlSeconds: "300" }).idleTtlSeconds, 300);

// First message and the newest `preserveRecent` are pinned; chars/4 of the rest.
assert.equal(estimateRemovableTokens([big("a", 400), big("b", 400), big("c", 400)], 1), 100);
assert.equal(estimateRemovableTokens([big("a", 400)], 0), 0);
assert.equal(estimateRemovableTokens(undefined, 6), 0);

assert.equal(gapBucket(10), "<5m");
assert.equal(gapBucket(3700), "1h-2h");
assert.equal(gapBucket(40000), ">8h");
assert.equal(gapBucket(Number.NaN), "unknown");

const base = { options: ON, lastEnd: 0, now: 61_000, passes: 0 };
assert.equal(idleDecision({ ...base, options: {} }).reason, "off");
assert.equal(idleDecision({ ...base, lastEnd: undefined }).fire, false);
assert.equal(idleDecision({ ...base, now: 59_000 }).reason, "cache warm");
assert.equal(idleDecision({ ...base, passedEnd: 0 }).fire, false, "never twice over the same history");
assert.equal(idleDecision(base).fire, true, "past the TTL, removable not yet counted");
assert.equal(idleDecision({ ...base, removable: 99 }).fire, false);
assert.equal(idleDecision({ ...base, removable: 99 }).notify, true);
assert.equal(idleDecision({ ...base, removable: 100 }).fire, true);
assert.equal(idleDecision({ ...base, passes: 8, options: { ...ON, idleMaxPassesPerSession: 8 } }).fire, false);

// --- stashIdle: { messages, removed, stashed } or { skip }, never throws -------------------
const report = (over = {}) => ({
  degraded: false, reduction_ratio: 0.9, tool_calls_replaced: 1, tool_calls_stashed: 1,
  tokens_before: 1200, tokens_after: 100, ...over,
});
const fake = (reply) => {
  const seen = { toasts: [], args: null };
  return {
    seen,
    $: {
      ui: { log() {}, toast: (text) => seen.toasts.push(text) },
      mcp: {
        call: async (_server, _tool, args) => {
          seen.args = args;
          if (reply instanceof Error) throw reply;
          return { content: [{ type: "text", text: JSON.stringify(reply) }] };
        },
      },
    },
  };
};
const configured = resolveOptions(ON);
const KEPT = [{ role: "user", text: "Fix the parser." }];

{
  const { $, seen } = fake({ report: report(), messages: KEPT });
  const result = await stashIdle($, TRANSCRIPT, configured, "1h-2h");
  assert.equal(result.messages.length, 1);
  assert.equal(result.removed, 1100);
  assert.equal(seen.args.provider, "rules");
  assert.equal(seen.args.rules_mode, "stashed");
  assert.equal(seen.args.trigger, "idle");
  assert.equal(seen.args.idle_gap_bucket, "1h-2h");
  assert.equal(seen.args.question, "Fix the parser.", "the marker is not the objective");
  assert.equal(seen.args.ref_unchanged, true);
}
{
  // A ref answer: unchanged messages are the caller's own objects (engine handle kept).
  const source = [{ ...TRANSCRIPT[0], handle: "h0" }, ...TRANSCRIPT.slice(1)];
  const placeholder = { role: "user", text: "", toolResults: [{ tool_use_id: "t1", text: "[removed: 1000 tokens · lcc recall abc]" }] };
  const { $, seen } = fake({ report: report(), messages: [{ ref: 0 }, { ref: 1 }, placeholder, { ref: 3 }] });
  const result = await stashIdle($, source, configured, "2h-8h", "/repo");
  assert.equal(seen.args.workspace_root, "/repo");
  assert.equal(result.messages[0], source[0]);
  assert.equal(result.messages[0].handle, "h0");
  assert.match(result.messages[2].toolResults[0].text, /lcc recall/);
  const bad = await stashIdle(fake({ report: report(), messages: [{ ref: 9 }] }).$, source, configured);
  assert.match(bad.skip, /outside the transcript/);
}
for (const [reply, why] of [
  [new Error("server down"), /server down/],
  [{ report: report({ degraded: true, degradation_reason: "x" }), messages: [{}] }, /judge unavailable/],
  [{ report: report({ tokens_after: 1150 }), messages: [{}] }, /removed 50 tokens, under 100/],
  [{ report: report({ tool_calls_replaced: 0, tool_calls_stashed: 0 }), messages: [{}] }, /removed no tool output/],
]) {
  const result = await stashIdle(fake(reply).$, TRANSCRIPT, configured);
  assert.match(result.skip ?? "", why);
}

// --- register: off adds nothing; on wires turn.complete -> session.start -> compact -----
const hooks = (options) => {
  const table = {};
  register((name, fn) => {
    table[name] = fn;
    return { catch: (handler) => { table[`${name}.catch`] = handler; } };
  }, options);
  return table;
};
assert.deepEqual(Object.keys(hooks({})), ["session.compact", "session.compact.catch"], "off: only the compact hook");

// `/compact lcc:idle` by hand: the idle pass, and a skip (never next) on failure.
{
  const table = hooks(ON);
  const event = { trigger: "manual", instructions: IDLE_MARKER, messages: TRANSCRIPT };
  const never = () => { throw new Error("next must not run for an idle compaction"); };
  const good = fake({ report: report(), messages: KEPT });
  assert.equal((await table["session.compact"](good.$, event, never)).messages.length, 1);
  assert.match(good.seen.toasts[0], /idle stash \(removed 1100 tokens/);
  assert.ok((await table["session.compact"](fake(new Error("down")).$, event, never)).skip);
  // Re-entry with nothing prepared: the handler vetoes, it never falls through.
  assert.ok(table["session.compact.catch"]({}, event, never).skip);
  // Re-entry for any other compaction: the handler passes it on.
  assert.equal(table["session.compact.catch"]({}, { trigger: "manual", messages: [] }, () => "core"), "core");
}

{
  const table = hooks(ON);
  const store = new Map();
  let now = 1_000;
  const compactCalls = [];
  const toasts = [];
  const $ = {
    ui: { log() {}, toast: (text) => toasts.push(text) },
    mcp: {
      connect: async () => ({ isConnected: true, server: "plugin:lcc:lcc" }),
      call: async () => ({ content: [{ type: "text", text: JSON.stringify({ report: report(), messages: KEPT }) }] }),
    },
    clock: { now: async () => now },
    store: {
      get: async (k) => store.get(k),
      set: async (k, v) => { store.set(k, v); },
      keys: async () => [...store.keys()],
      delete: async (k) => { store.delete(k); },
    },
    session: {
      id: async () => "s1",
      cwd: async () => "/r",
      messages: async () => TRANSCRIPT,
      // Called from the session.start hook, the plugin's own session.compact hook answers.
      compact: async (args) => {
        compactCalls.push(args);
        const event = { trigger: "plugin", ...args, messages: TRANSCRIPT };
        return table["session.compact"]($, event, () => { throw new Error("must not fall through"); });
      },
    },
  };
  const start = () => table["session.start"]($, { cwd: "/r" }, async (e) => ({ cwd: e.cwd }));
  await table["turn.complete"]($, {}, async () => ({ text: "" }));
  assert.equal(store.get("idle:s1").end, 1_000);
  await table["turn.complete"]($, { agentId: "sub" }, async () => ({ text: "" }));
  now = 30_000;
  await start();
  assert.equal(compactCalls.length, 0, "warm: nothing");

  now = 62_000; // resumed 61 s after the last turn
  await start();
  assert.deepEqual(compactCalls, [{ instructions: IDLE_MARKER }]);
  assert.equal(store.get("idle:s1").passes, 1, "the prepared pass stood");
  assert.match(toasts.at(-1), /idle stash \(removed 1100 tokens/);

  // Resumed again over the same history: no second pass.
  now = 500_000;
  await start();
  assert.equal(compactCalls.length, 1);

  // Claude Code compacting on its own (hook skipped): reported, never counted as a pass.
  $.session.compact = async () => ({ messages: [] });
  await table["turn.complete"]($, {}, async () => ({ text: "" }));
  now = 900_000;
  await start();
  assert.match(toasts.at(-1), /did not stand/);
  assert.equal(store.get("idle:s1").passes, 1);

  // A rejecting compact (headless) never throws into the engine.
  $.session.compact = async () => { throw new Error("not available"); };
  await table["turn.complete"]($, {}, async () => ({ text: "" }));
  now = 2_000_000;
  assert.deepEqual(await start(), { cwd: "/r" });
}

console.log("hook-idle: ok");
