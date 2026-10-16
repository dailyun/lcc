// Claude Code function-hook adapter for `lcc compact --mode tool-calls`.
//
// The engine hands this module a `session.compact` event: the transcript it is about to
// summarize. The module hands that transcript to the `lcc mcp` server shipped with the
// plugin (`compact_transcript`), which pairs every tool call with its result, scores the
// pairs with TypeSafe Jev, and returns the conversation minus the pairs that are spent.
//
// Nothing is rewritten: kept messages carry their original bytes, and user/assistant text
// is never touched. Every failure path calls `next(event)`, so a hook that cannot do its
// job lets Claude Code's own compaction run instead of leaving the session uncompacted.
//
// EARLY ACCESS: function hooks require Claude Code 2.1.274+ and
// CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1. See plugins/claude-code/hooks/README.md.

import {
  buildCompactArgs,
  decideReplacement,
  hasUnsupportedContent,
  parseToolResult,
  resolveOptions,
  toSessionMessages,
} from "./map.mjs";

function notify($, text) {
  $.ui.log(text);
  $.ui.toast(text, { timeoutMs: 15000 });
}

function message(error) {
  return error instanceof Error ? error.message : String(error);
}

/**
 * The session's working directory, which becomes `workspace_root` for rules_mode recoverable.
 * Preferred: `event.cwd` when it is absolute. Fallback: `process.cwd()` when there is a
 * `process`. Claude Code 2.1.296 runs hook modules in a worker with no `process` and puts no
 * `cwd` on `session.compact`, so there this is undefined and no workspace_root is sent.
 */
export function sessionCwd(event) {
  if (typeof event?.cwd === "string" && event.cwd.startsWith("/")) return event.cwd;
  return globalThis.process?.cwd?.();
}

/** @type {import('claude-code').Register} */
export const register = (on, options) => {
  const configured = resolveOptions(options);

  on("session.compact", async ($, event, next) => {
    if (event.trigger === "precompute") return next(event);

    const unsupported = hasUnsupportedContent(event.messages);
    if (unsupported) {
      notify($, `lcc: built-in summary (this session has ${unsupported})`);
      return next(event);
    }

    const args = buildCompactArgs({
      messages: event.messages ?? [],
      instructions: event.instructions,
      options: configured,
      cwd: sessionCwd(event),
    });
    if (!args) return next(event);

    try {
      const { content, isError } = await $.mcp.call(
        configured.server,
        "compact_transcript",
        args,
      );
      if (isError) throw new Error(`compact_transcript failed on server '${configured.server}'`);

      const payload = parseToolResult(content);
      const decision = decideReplacement({
        report: payload.report,
        messages: payload.messages,
        minReductionRatio: configured.minReductionRatio,
      });
      if (!decision.replace) {
        notify($, `lcc: built-in summary (${decision.reason})`);
        return next(event);
      }
      const via = payload.report?.provider_used ?? configured.provider;
      notify($, `lcc: verbatim compaction via ${via}, no summary (${decision.reason})`);
      return { messages: toSessionMessages(payload.messages) };
    } catch (error) {
      notify($, `lcc: built-in summary (${message(error)})`);
      return next(event);
    }
  });
};
