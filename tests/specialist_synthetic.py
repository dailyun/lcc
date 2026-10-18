"""Deterministic synthetic agent transcripts for specialist-v3 tests and the latency bench.

No real session text: every string is generated from fixed word lists and a seeded RNG.
"""

from __future__ import annotations

import random
from typing import Any

_WORDS = [
    "cache",
    "parser",
    "budget",
    "router",
    "schema",
    "token",
    "module",
    "config",
    "adapter",
    "render",
    "index",
    "window",
    "session",
    "handler",
    "payload",
    "ledger",
    "metric",
    "batch",
    "policy",
    "verify",
    "archive",
    "stream",
    "bridge",
]
_FILES = (
    "src/app/router.py",
    "src/app/cache_store.py",
    "src/lib/parserCore.ts",
    "docs/SETUP.md",
    "tests/test_router.py",
    "config/settings.yaml",
    "src/app/budget_policy.py",
)
_TOOLS = ("Read", "Edit", "Bash", "Grep", "Glob", "WebFetch", "TodoWrite", "mcp__docs__search")


def _sentence(rng: random.Random, n: int) -> str:
    return " ".join(rng.choice(_WORDS) for _ in range(n)).capitalize() + "."


def _paragraphs(rng: random.Random, k: int) -> str:
    return "\n\n".join(_sentence(rng, rng.randint(14, 40)) for _ in range(k))


def _tool_input(rng: random.Random, tool: str) -> dict[str, Any]:
    path = rng.choice(_FILES)
    if tool == "Bash":
        return {"command": rng.choice(["pytest -q", "git diff", "git status", f"cat {path}"])}
    if tool in ("Grep", "Glob", "mcp__docs__search"):
        return {"pattern": rng.choice(_WORDS) + "_" + rng.choice(_WORDS), "path": "src"}
    if tool == "Edit":
        return {
            "file_path": path,
            "old_string": rng.choice(_WORDS),
            "new_string": rng.choice(_WORDS),
        }
    if tool == "TodoWrite":
        return {"todos": [_sentence(rng, 5) for _ in range(3)]}
    if tool == "WebFetch":
        return {"url": f"https://example.com/{rng.choice(_WORDS)}"}
    return {"file_path": path}


def _tool_output(rng: random.Random, tool: str, size: int) -> str:
    kind = rng.random()
    if tool == "Bash" and kind < 0.3:
        return (
            "Traceback (most recent call last):\n"
            f'  File "{rng.choice(_FILES)}", line {rng.randint(1, 400)}, in handler\n'
            f"ValueError: {_sentence(rng, 6)}\nexit code 1"
        )
    if tool == "Bash" and kind < 0.5:
        path = rng.choice(_FILES)
        return f"diff --git a/{path} b/{path}\n@@ -1,4 +1,5 @@\n-old = {rng.randint(1, 9)}\n+new = {rng.randint(1, 9)}"
    if tool in ("Read", "Edit"):
        lines = [
            f"def {rng.choice(_WORDS)}_{rng.choice(_WORDS)}(x):\n    return x + {rng.randint(1, 99)}"
            for _ in range(max(1, size // 60))
        ]
        return "\n".join(lines)
    return _paragraphs(rng, max(1, size // 200))


def synthetic_payload(seed: int = 7, turns: int = 12, big_every: int = 5) -> list[dict[str, Any]]:
    """A transcript payload in the shape ``parse_transcript`` accepts."""
    rng = random.Random(seed)
    messages: list[dict[str, Any]] = []
    call = 0
    for turn in range(turns):
        messages.append({"role": "user", "text": _paragraphs(rng, rng.randint(1, 2))})
        for _ in range(rng.randint(1, 3)):
            tool = rng.choice(_TOOLS)
            call += 1
            cid = f"toolu_{seed:02d}_{call:04d}"
            messages.append(
                {
                    "role": "assistant",
                    "text": _sentence(rng, rng.randint(8, 20)) if rng.random() < 0.6 else "",
                    "toolUses": [
                        {"tool_use_id": cid, "tool": tool, "input": _tool_input(rng, tool)}
                    ],
                }
            )
            size = 9000 if big_every and call % big_every == 0 else rng.randint(150, 1600)
            messages.append(
                {
                    "role": "user",
                    "text": "",
                    "toolResults": [{"tool_use_id": cid, "text": _tool_output(rng, tool, size)}],
                }
            )
        messages.append({"role": "assistant", "text": _paragraphs(rng, rng.randint(1, 3))})
        if turn % 4 == 3:  # repeated content: exercises the duplicate features
            messages.append({"role": "assistant", "text": messages[-1]["text"]})
    return messages


GOAL = "Fix the router cache_store budget policy failing in src/app/router.py?"
