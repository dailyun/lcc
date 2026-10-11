"""Without a workspace root, recoverable decisions stay byte-identical to v0.8 (LCC 4c39161).

The disk-authoritative rule (a file whose current content equals the removed output may be
replaced even after later writes) only runs when ``workspace_root`` is set. This pins the
transcript-proxy path: a fixed synthetic transcript must give exactly the v0.8 decisions and
compacted payload. Regenerate only on an intended change to the no-root path:
``LCC_REGEN_GOLDEN=1 pytest tests/test_transcript_recoverable_golden.py``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from lcc.relevance.transcript import (
    TranscriptCompactionRequest,
    compact_transcript,
    messages_to_payload,
)

GOLDEN = Path(__file__).parent / "fixtures" / "recoverable_v08_golden.json"
LISTING = "\n".join(f"src/module_{n}.py" for n in range(30))
BIG = "\n".join(f"test_{n} PASSED" for n in range(40))

def body(tag: str, n: int = 39, numbered: bool = False) -> str:
    rows = [f"{tag} line {i} of the parser" for i in range(1, n + 1)]
    return "\n".join(f"{i:>6}\t{r}" for i, r in enumerate(rows, 1)) if numbered else (
        "\n".join(rows) + "\n")


CALLS = [
    ("Bash", {"command": "pytest -q"}, BIG),
    ("Bash", {"command": "pytest -q"}, BIG),
    ("Read", {"file_path": "/r/src/a.py"}, body("a", numbered=True)),
    ("Read", {"file_path": "/r/src/b.py", "offset": 5, "limit": 8},
     "\n".join(body("b", numbered=True).split("\n")[4:12])),
    ("Bash", {"command": "cat src/c.py", "cwd": "/r"}, body("c")),
    ("Bash", {"command": "sed -n '5,12p' src/d.py", "cwd": "/r"},
     "".join(body("d").splitlines(keepends=True)[4:12])),
    ("Bash", {"command": "head -n 12 src/e.py", "cwd": "/r"}, body("e", 12)),
    ("Bash", {"command": "tail -n 5 src/f.py", "cwd": "/r"}, body("f", 5) + "\n" * 60),
    ("Bash", {"command": "ls src"}, LISTING),
    ("Bash", {"command": "git status"}, LISTING + "x"),
    ("Read", {"file_path": "/r/src/g.py"}, body("g", numbered=True)),
    ("Read", {"file_path": "/r/sub/h.py"}, body("h", numbered=True)),
    ("Read", {"file_path": "/tmp/t.py"}, body("t", numbered=True)),
    ("Read", {"file_path": "rel.py"}, body("rel", numbered=True)),
    ("Read", {"file_path": "/r/src/b.py", "offset": 50, "limit": 3},
     "\n".join(body("b2", numbered=True).split("\n")[4:12])),
    ("Edit", {"file_path": "/r/src/g.py", "old_string": "a", "new_string": "b"}, "ok"),
    ("exec_command", {"cmd": "make", "workdir": "/r/sub"}, "built"),
    ("Grep", {"pattern": "todo"}, "1 match"),
]


def transcript() -> list[dict]:
    messages: list[dict] = [{"role": "user", "text": "fix the parser"}]
    for n, (tool, tool_input, output) in enumerate(CALLS, start=1):
        messages.append({"role": "assistant", "text": f"step {n}",
                         "toolUses": [{"id": f"c{n}", "tool": tool, "input": tool_input}]})
        messages.append({"role": "user", "text": "", "toolResults": [{"id": f"c{n}",
                                                                      "text": output}]})
    messages.append({"role": "assistant", "text": "wrap"})
    return messages


def snapshot(allow: bool) -> dict:
    result = compact_transcript(TranscriptCompactionRequest(
        payload={"messages": transcript()}, question="fix the parser", provider="rules",
        rules_mode="recoverable", preserve_recent=1, allow_command_pointers=allow,
    ))
    return {"decisions": result.decisions, "messages": messages_to_payload(result.messages)}


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setenv("LCC_DISABLE_NETWORK", "1")


def test_no_root_decisions_match_v08():
    got = json.loads(json.dumps({str(a): snapshot(a) for a in (False, True)}))
    if os.environ.get("LCC_REGEN_GOLDEN"):
        GOLDEN.write_text(json.dumps(got, indent=1, sort_keys=True) + "\n")
    assert got == json.loads(GOLDEN.read_text())
