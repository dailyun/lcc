// Idle stash cache-contract edges: TTL boundaries, thresholds, pass limits, fail-safe paths
// and idempotent placeholders, against a fake `$`. docs/CACHE_ALIGNMENT.md "Invariants and tests".
//
//   node test/hook-cache.test.mjs

import assert from "node:assert/strict";

import { register, stashIdle } from "../plugins/claude-code/hooks/lcc-compact.mjs";
import {
  IDLE_MARKER,
  estimateRemovableTokens,
  expandRefs,
  idleDecision,
  passCount,
  resolveOptions,
} from "../plugins/claude-code/hooks/map.mjs";

const TTL = 3600;
const ON = { idleStash: true, idleTtlSeconds: TTL, idleMinRemovableTokens: 1000, idleMaxPassesPerSession: 2, preserveRecentMessages: 1 };
const configured = resolveOptions(ON);
const NOTE = "[removed: 1000 tokens · lcc recall 0123456789ab]";
const TRANSCRIPT = [
  { role: "user", text: "Fix the parser." },
  { role: "assistant", text: "", toolUses: [{ tool_use_id: "t1", tool: "Read", input: { file_path: "/r/a.py" } }] },
  { role: "user", text: "", toolResults: [{ tool_use_id: "t1", text: "x".repeat(4000) }] },
  { role: "assistant", text: "Done." },
];
const report = (over = {}) => ({
  degraded: false, reduction_ratio: 0.9, tool_calls_replaced: 1, tool_calls_stashed: 1,
  tokens_before: 2000, tokens_after: 1000, ...over,
});
const STASHED = [{ ref: 0 }, { ref: 1 }, { ref: 2, toolUses: [], toolResults: [{ ref: 0, tool_use_id: "t1", text: NOTE }] }, { ref: 3 }];
let checks = 0;
const ok = (name, fn) => Promise.resolve(fn()).then(() => { checks += 1; }, (error) => { error.message = `${name}: ${error.message}`; throw error; });

// --- idleDecision: the TTL boundary and the cache-state edges --------------------------------
const at = (gapMs, over = {}) => idleDecision({ options: ON, lastEnd: 1_000_000, now: 1_000_000 + gapMs, passes: 0, ...over });
await ok("exactly at the TTL is cold", () => assert.equal(at(TTL * 1000).fire, true));
await ok("1 ms below the TTL is warm", () => assert.equal(at(TTL * 1000 - 1).reason, "cache warm"));
await ok("1 ms above the TTL is cold", () => assert.equal(at(TTL * 1000 + 1).fire, true));
await ok("negative gap is clock skew, never cold", () => {
  const decision = at(-5_000);
  assert.equal(decision.fire, false);
  assert.match(decision.reason, /clock skew/);
  assert.equal(decision.notify, undefined, "no toast for a skewed clock");
});
await ok("huge negative gap (clock reset) is not cold", () => assert.equal(at(-1e12).fire, false));
await ok("very long idle is cold", () => assert.equal(at(1e13).fire, true));
for (const [label, lastEnd, now] of [
  ["missing lastEnd", undefined, 5],
  ["NaN lastEnd", Number.NaN, 5],
  ["string lastEnd", "1000", 5_000_000],
  ["Infinity now", 0, Number.POSITIVE_INFINITY],
  ["missing now", 0, undefined],
]) {
  await ok(`missing timestamp: ${label}`, () => {
    const decision = idleDecision({ options: ON, lastEnd, now, passes: 0 });
    assert.equal(decision.fire, false);
    assert.equal(decision.reason, "no previous request");
  });
}
await ok("removable exactly at idleMinRemovableTokens fires", () => assert.equal(at(TTL * 1000, { removable: 1000 }).fire, true));
await ok("removable one under the minimum stands down with a toast", () => {
  const decision = at(TTL * 1000, { removable: 999 });
  assert.equal(decision.fire, false);
  assert.equal(decision.notify, true);
});
await ok("pass count below the cap fires", () => assert.equal(at(TTL * 1000, { passes: 1 }).fire, true));
await ok("pass count at the cap stands down", () => {
  const decision = at(TTL * 1000, { passes: 2 });
  assert.equal(decision.fire, false);
  assert.match(decision.reason, /2 passes already/);
});
await ok("idleMaxPassesPerSession 0 disables the stash", () =>
  assert.equal(idleDecision({ options: { ...ON, idleMaxPassesPerSession: 0 }, lastEnd: 0, now: 1e12, passes: 0 }).fire, false));
await ok("corrupted pass counts read as 0", () => {
  for (const bad of ["8", "abc", -3, Number.NaN, null, undefined, {}, Number.POSITIVE_INFINITY]) assert.equal(passCount(bad), 0, String(bad));
  assert.equal(passCount(2.7), 2);
  // Before the fix "abc" survived as a string and `passes += 1` concatenated: never capped.
  assert.equal(at(TTL * 1000, { passes: "abc" }).fire, true);
});
await ok("estimateRemovableTokens floors chars/4 at the boundary", () => {
  const msg = (n) => ({ role: "user", text: "", toolResults: [{ tool_use_id: "x", text: "y".repeat(n) }] });
  assert.equal(estimateRemovableTokens([msg(1), msg(4003), msg(1)], 1), 1000);
  assert.equal(estimateRemovableTokens([msg(1), msg(3999), msg(1)], 1), 999);
});

// --- stashIdle: the removed-tokens gate and the cache state it reports --------------------------
const fake = (reply) => {
  const seen = { args: [] };
  return {
    seen,
    $: {
      ui: { log() {}, toast() {} },
      mcp: {
        call: async (_server, _tool, args) => {
          seen.args.push(args);
          if (reply instanceof Error) throw reply;
          if (typeof reply === "function") return reply(args);
          return { content: [{ type: "text", text: JSON.stringify(reply) }] };
        },
      },
    },
  };
};
await ok("removed exactly at the minimum stands", async () => {
  const result = await stashIdle(fake({ report: report(), messages: STASHED }).$, TRANSCRIPT, configured, "1h-2h");
  assert.equal(result.removed, 1000);
  assert.equal(result.messages[2].toolResults[0].text, NOTE);
});
await ok("removed one under the minimum skips", async () => {
  const result = await stashIdle(fake({ report: report({ tokens_after: 1001 }), messages: STASHED }).$, TRANSCRIPT, configured, "1h-2h");
  assert.match(result.skip, /removed 999 tokens, under 1000/);
});
await ok("automatic pass reports a cold cache; manual pass an unknown one", async () => {
  const auto = fake({ report: report(), messages: STASHED });
  await stashIdle(auto.$, TRANSCRIPT, configured, "2h-8h");
  assert.equal(auto.seen.args[0].cache_state, "cold");
  const manual = fake({ report: report(), messages: STASHED });
  await stashIdle(manual.$, TRANSCRIPT, configured, "manual");
  assert.equal(manual.seen.args[0].cache_state, "unknown");
});
for (const [label, reply] of [
  ["server throws", new Error("boom")],
  ["isError result", () => ({ isError: true, content: [] })],
  ["non-JSON content", () => ({ content: [{ type: "text", text: "not json" }] })],
  ["empty content", () => ({ content: [] })],
  ["ref outside the transcript", { report: report(), messages: [{ ref: 42 }] }],
  ["patch that does not fit", { report: report(), messages: [{ ref: 2, toolResults: [{ ref: 0, tool_use_id: "zz", text: "n" }] }] }],
  ["report without token counts", { report: report({ tokens_before: undefined }), messages: STASHED }],
]) {
  await ok(`stashIdle never throws: ${label}`, async () => {
    const result = await stashIdle(fake(reply).$, TRANSCRIPT, configured, "1h-2h");
    assert.equal(typeof result.skip, "string");
  });
}

// --- idempotent placeholders: the same stash answer twice gives the same bytes ----------------
await ok("expanding the same answer twice is byte-identical", () => {
  const first = expandRefs(STASHED, TRANSCRIPT);
  const second = expandRefs(STASHED, TRANSCRIPT);
  assert.equal(JSON.stringify(first), JSON.stringify(second));
});
await ok("a second stash over stashed history keeps the placeholder bytes", async () => {
  const stashed = expandRefs(STASHED, TRANSCRIPT);
  // The server leaves an existing pointer alone (lcc.relevance.transcript_stash: already_stashed),
  // so a second pass answers with refs only and removes nothing: the hook must stand down.
  const again = fake({ report: report({ tool_calls_replaced: 0, tool_calls_stashed: 0, tokens_after: 2000 }), messages: [{ ref: 0 }, { ref: 1 }, { ref: 2 }, { ref: 3 }] });
  const result = await stashIdle(again.$, stashed, configured, "2h-8h");
  assert.ok(result.skip, "nothing new to stash: no compaction, no rewritten prefix");
  assert.equal(stashed[2].toolResults[0].text, NOTE);
});

// --- register: the session.start / turn.complete wiring at the boundaries ---------------------
const hooks = (options) => {
  const table = {};
  register((name, fn) => {
    table[name] = fn;
    return { catch: (handler) => { table[`${name}.catch`] = handler; } };
  }, options);
  return table;
};

function engine(table, over = {}) {
  const store = new Map();
  const state = { now: 0, compacts: 0, toasts: [] };
  const $ = {
    ui: { log() {}, toast: (text) => state.toasts.push(text) },
    mcp: {
      connect: async () => ({ isConnected: true }),
      call: async () => ({ content: [{ type: "text", text: JSON.stringify({ report: report(), messages: STASHED }) }] }),
    },
    clock: { now: async () => state.now },
    store: {
      get: async (k) => store.get(k),
      set: async (k, v) => { store.set(k, v); },
      keys: async () => [...store.keys()],
      delete: async (k) => { store.delete(k); },
    },
    session: {
      id: async () => "s",
      cwd: async () => "/r",
      messages: async () => TRANSCRIPT,
      compact: async (args) => {
        state.compacts += 1;
        return table["session.compact"]($, { trigger: "plugin", ...args, messages: TRANSCRIPT }, () => {
          throw new Error("must not fall through");
        });
      },
    },
    ...over,
  };
  const turn = () => table["turn.complete"]($, {}, async () => ({ text: "t" }));
  const start = () => table["session.start"]($, {}, async () => "started");
  return { $, store, state, turn, start };
}

await ok("resume exactly at idleTtlSeconds compacts; 1 ms earlier does not", async () => {
  const early = engine(hooks(ON));
  early.state.now = 10_000;
  await early.turn();
  early.state.now = 10_000 + TTL * 1000 - 1;
  assert.equal(await early.start(), "started");
  assert.equal(early.state.compacts, 0);

  const exact = engine(hooks(ON));
  exact.state.now = 10_000;
  await exact.turn();
  exact.state.now = 10_000 + TTL * 1000;
  assert.equal(await exact.start(), "started");
  assert.equal(exact.state.compacts, 1);
  assert.equal(exact.store.get("idle:s").passes, 1);
});

await ok("a resume with the clock behind the last turn does nothing", async () => {
  const e = engine(hooks(ON));
  e.state.now = 10_000_000;
  await e.turn();
  e.state.now = 5_000;
  assert.equal(await e.start(), "started");
  assert.equal(e.state.compacts, 0);
});

await ok("idleMaxPassesPerSession caps passes across many idle returns", async () => {
  const e = engine(hooks(ON));
  for (let round = 0; round < 4; round += 1) {
    e.state.now += 1_000;
    await e.turn();
    e.state.now += TTL * 1000;
    await e.start();
  }
  assert.equal(e.state.compacts, 2);
  assert.equal(e.store.get("idle:s").passes, 2);
  assert.match(e.state.toasts.at(-1), /2 passes already this session/);
});

await ok("a corrupted store record cannot lift the pass cap", async () => {
  const e = engine(hooks(ON));
  e.store.set("idle:s", { end: 0, passes: "abc" });
  e.state.now = TTL * 1000;
  await e.start();
  assert.equal(e.store.get("idle:s").passes, 1, "counted as a number, not 'abc1'");
  e.store.set("idle:s", "garbage");
  await e.turn();
  assert.deepEqual(Object.keys(e.store.get("idle:s")), ["end"], "a non-object record is replaced");
});

// Every failure path returns the engine's own result and never throws into Claude Code.
for (const [label, breakIt] of [
  ["store.get throws", (e) => { e.$.store.get = async () => { throw new Error("store"); }; }],
  ["store.set throws", (e) => { e.$.store.set = async () => { throw new Error("store"); }; }],
  ["clock throws", (e) => { e.$.clock.now = async () => { throw new Error("clock"); }; }],
  ["session.id throws", (e) => { e.$.session.id = async () => { throw new Error("id"); }; }],
  ["session.messages throws", (e) => { e.$.session.messages = async () => { throw new Error("messages"); }; }],
  ["session.cwd throws", (e) => { e.$.session.cwd = async () => { throw new Error("cwd"); }; }],
  ["mcp.connect rejects", (e) => { e.$.mcp.connect = async () => { throw new Error("connect"); }; }],
  ["mcp.call rejects", (e) => { e.$.mcp.call = async () => { throw new Error("call"); }; }],
  ["session.compact rejects", (e) => { e.$.session.compact = async () => { throw new Error("compact"); }; }],
  ["session.compact returns skip", (e) => { e.$.session.compact = async () => ({ skip: "no" }); }],
  ["ui.toast throws (headless)", (e) => { e.$.ui.toast = () => { throw new Error("toast"); }; }],
  ["ui.log throws (headless)", (e) => { e.$.ui.log = () => { throw new Error("log"); }; }],
  ["store and ui both throw", (e) => {
    e.$.store.set = async () => { throw new Error("store"); };
    e.$.ui.log = () => { throw new Error("log"); };
  }],
]) {
  await ok(`fail-safe: ${label}`, async () => {
    const e = engine(hooks(ON));
    e.state.now = 1_000;
    await e.turn();
    breakIt(e);
    e.state.now = 1_000 + TTL * 1000;
    assert.equal(await e.turn().then((r) => r.text), "t");
    e.state.now += TTL * 1000;
    assert.equal(await e.start(), "started");
    const passes = passCount(e.store.get("idle:s")?.passes);
    assert.ok(passes <= 1);
  });
}

await ok("an idle compaction answers skip, never next, on every failure", async () => {
  const table = hooks(ON);
  const never = () => { throw new Error("next must not run"); };
  const event = { trigger: "manual", instructions: IDLE_MARKER, messages: TRANSCRIPT };
  for (const reply of [new Error("down"), () => ({ isError: true, content: [] }), { report: report({ degraded: true }), messages: STASHED }]) {
    const answer = await table["session.compact"](fake(reply).$, event, never);
    assert.match(answer.skip, /lcc idle stash/);
  }
  assert.ok((await table["session.compact"]({}, { ...event, agentId: "a" }, never)).skip);
});

console.log(`hook-cache: ok (${checks} checks)`);
