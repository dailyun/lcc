#!/usr/bin/env python3
"""Render demos/session.gif, the README hero, from a real `lcc` run.

The input is demos/session.json: a synthetic, 50-message Claude Code-style session debugging a
booking API (repeated reads of the same files, long test logs, a `KeyError` found early and
needed later, a fix, a re-run). Every number, line and id in the GIF comes from running the
commands below on it; nothing is typed in by hand. The transcript view (scene 3) is drawn from
the real compacted file, and its "byte-identical" marks are computed by comparing it with the
input.

    python3 demos/make_gifs.py session     # or: python3 demos/session_gif.py

Commands run, in a scratch directory holding a copy of session.json:

    lcc compact session.json -q "fix the booking KeyError" --mode tool-calls \
        --provider rules --rules-mode stashed -o compacted.json -r report.json
    jq -c '{provider_used, degraded, semantic_guarantee}' report.json
    lcc explain report.json --only replace --limit 3
    lcc recall <id> --lines 50-54
    lcc recall <id> | cmp - <(jq -j '.messages[12].toolResults[0].text' session.json)

The stash goes to the scratch directory (LCC_STASH_DIR), not ~/.cache, so the run is isolated
and repeatable; the ids are content hashes, so they are the same on every machine.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent))
from make_gifs import (  # noqa: E402  (sibling module, shared palette and font)
    ACCENT,
    ANSI_RE,
    BAR,
    BG,
    DIM,
    DOTS,
    FG,
    FONT_PATH,
    OUT,
    PROMPT,
    ROOT,
    WARN,
)

WORK = Path("/tmp/lcc-demo-session")
QUESTION = "fix the booking KeyError"

WIDTH, HEIGHT = 1040, 620
BAR_HEIGHT = 30
PAD = 16
SIZE = 14
LINE_H = 19
#: Characters that fit on one line at SIZE on WIDTH (DejaVu Sans Mono / Menlo advance ~0.6em).
COLS = 118
SURFACE = (49, 50, 68)
RED = DOTS[0]
GREEN = PROMPT

BOLD_CANDIDATES = (
    os.environ.get("LCC_DEMO_FONT_BOLD", ""),
    FONT_PATH.replace("DejaVuSansMono.ttf", "DejaVuSansMono-Bold.ttf"),
    FONT_PATH.replace("FiraCode-Regular.ttf", "FiraCode-Bold.ttf"),
)
BOLD_PATH = next((p for p in BOLD_CANDIDATES if p and os.path.isfile(p)), FONT_PATH)


def font(size: int = SIZE, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(BOLD_PATH if bold else FONT_PATH, size)


def run(command: str) -> str:
    """Run one real shell command in the scratch directory; return what it printed."""
    env = dict(os.environ, COLUMNS="100", LCC_STASH_DIR=str(WORK / "stash"))
    proc = subprocess.run(
        ["bash", "-c", command], cwd=WORK, env=env, capture_output=True, text=True, timeout=180
    )
    if proc.returncode != 0:
        raise SystemExit(f"demo command failed ({proc.returncode}): {command}\n{proc.stderr}")
    return ANSI_RE.sub("", proc.stdout + proc.stderr).rstrip("\n")


def clip(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


# --------------------------------------------------------------------------- drawing


def canvas(title: str) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    image = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(image)
    draw.rectangle([0, 0, WIDTH, BAR_HEIGHT], fill=BAR)
    for index, colour in enumerate(DOTS):
        x = PAD + index * 18
        draw.ellipse([x, 10, x + 10, 20], fill=colour)
    draw.text((WIDTH // 2, BAR_HEIGHT // 2), title, font=font(12), fill=DIM, anchor="mm")
    return image, draw


#: A line is a list of (text, colour) runs, optionally highlighted (a surface band behind it).
Run = tuple[str, tuple[int, int, int]]


def draw_lines(
    draw: ImageDraw.ImageDraw, lines: list[tuple[list[Run], bool]], top: int = BAR_HEIGHT + PAD
) -> None:
    body = font()
    char_w = body.getlength("M")
    y = top
    for runs, highlighted in lines:
        if highlighted:
            draw.rectangle([PAD - 6, y - 2, WIDTH - PAD + 6, y + LINE_H - 3], fill=SURFACE)
        x = float(PAD)
        for text, colour in runs:
            draw.text((x, y), text, font=body, fill=colour)
            x += char_w * len(text)
        y += LINE_H


def output_colour(line: str) -> tuple[int, int, int]:
    if "[removed:" in line or line.lstrip().startswith("now:"):
        return WARN
    if line.startswith(("Tokens", "Reduction", "Rules mode")) or line == "byte-identical":
        return ACCENT
    if line.startswith(("Provider", "Semantic guarantee", "{")):
        return GREEN
    return DIM


def terminal(history: list[tuple[str, str]], title: str) -> Image.Image:
    """``history``: ("prompt", command) / ("out", text). Wrapped, never clipped."""
    image, draw = canvas(title)
    lines: list[tuple[list[Run], bool]] = []
    for kind, text in history:
        if kind == "prompt":
            for index, part in enumerate(_wrap(text, COLS - 2)):
                prefix = "$ " if index == 0 else "  "
                lines.append(([(prefix, GREEN), (part, FG)], False))
        else:
            for line in text.split("\n"):
                for part in _wrap(line, COLS):
                    lines.append(([(part, output_colour(line))], False))
    budget = (HEIGHT - BAR_HEIGHT - 2 * PAD) // LINE_H
    if len(lines) > budget:
        raise SystemExit(f"{title}: {len(lines)} lines do not fit in {budget}; trim the scene")
    draw_lines(draw, lines)
    return image


def _wrap(line: str, width: int) -> list[str]:
    if len(line) <= width:
        return [line]
    indent = " " * (len(line) - len(line.lstrip()) + 4)
    out, current = [], ""
    for word in line.split(" "):
        candidate = f"{current} {word}" if current else word
        if len(candidate) <= width:
            current = candidate
        else:
            out.append(current)
            current = indent + word
    out.append(current)
    return out


def centred(draw, y: int, text: str, size: int, colour, bold: bool = False) -> None:
    draw.text((WIDTH // 2, y), text, font=font(size, bold), fill=colour, anchor="mm")


# --------------------------------------------------------------------------- scenes


def title_card(count: int) -> Image.Image:
    image, draw = canvas("lcc — Local Context Compiler")
    centred(draw, 220, "lcc", 64, FG, bold=True)
    centred(draw, 290, "compact agent context without summarizing it", 26, FG)
    centred(draw, 350, "A summary rewrites your session.", 18, DIM)
    centred(draw, 378, "lcc keeps it verbatim and tells you what it cut.", 18, ACCENT)
    centred(draw, 520, "rendered from a real run on demos/session.json:", 13, DIM)
    story = f"a synthetic {count}-message Claude Code session debugging a booking API"
    centred(draw, 540, story, 13, DIM)
    return image


def transcript_view(original: list[dict], compacted: list[dict]) -> Image.Image:
    """The compacted session, message by message, checked against the input."""
    image, draw = canvas("compacted.json — the first messages, compared with session.json")
    by_id = {
        result["tool_use_id"]: result["text"]
        for message in original
        for result in message.get("toolResults", [])
    }
    calls = {
        use["tool_use_id"]: use
        for message in compacted
        for use in message.get("toolUses", [])
    }
    budget = (HEIGHT - BAR_HEIGHT - 2 * PAD) // LINE_H - 3  # room for the footer
    lines: list[tuple[list[Run], bool]] = []
    shown = 0
    for index, (before, after) in enumerate(zip(original, compacted, strict=True)):
        rows: list[tuple[list[Run], bool]] = []
        role = after["role"]
        if after.get("text"):
            same = after["text"] == before["text"]
            mark = ("  = identical", GREEN) if same else ("  CHANGED", RED)
            text = clip(after["text"].replace("\n", " "), COLS - 14 - len(mark[0]))
            rows.append(([(f"{index:>2} ", DIM), (f"{role:<11}", ACCENT if role == "user" else FG),
                          (text, FG), mark], "KeyError" in after["text"]))
        for result in after.get("toolResults", []):
            use = calls[result["tool_use_id"]]
            detail = str(use["input"].get("command") or use["input"].get("file_path") or use[
                "input"].get("pattern", "")).replace("/srv/booking-api/", "")
            call = clip(f"{use['tool']} {detail}", 31)
            prefix = [(f"{index:>2} ", DIM), ("tool result ", DIM), (f"{call:<32}", DIM)]
            text = result["text"]
            if text.startswith("[removed:"):
                rows.append(([*prefix, (text, WARN)], False))
                continue
            assert text == by_id[result["tool_use_id"]], "a kept result changed"
            rows.append(([*prefix, (f"kept, {len(text):,} chars, identical", GREEN)], False))
            error = next((ln for ln in text.split("\n") if ln.startswith("E   KeyError")), None)
            if error:
                rows.append(([(" " * 15, DIM), (f"{error}", FG),
                              ("   ← the error line, still there", GREEN)], True))
        if len(lines) + len(rows) > budget:
            break
        lines += rows
        shown = index + 1
    draw_lines(draw, lines)

    texts = [(b, a) for b, a in zip(original, compacted, strict=True) if a.get("text")]
    identical = sum(1 for b, a in texts if b["text"] == a["text"])
    removed = sum(
        1 for m in compacted for r in m.get("toolResults", []) if r["text"].startswith("[removed:")
    )
    results = sum(len(m.get("toolResults", [])) for m in compacted)
    footer_y = HEIGHT - PAD - 2 * LINE_H
    draw.line([PAD, footer_y - 10, WIDTH - PAD, footer_y - 10], fill=SURFACE, width=1)
    draw_lines(draw, [
        ([(f"all {len(compacted)} messages: ", DIM),
          (f"{identical}/{len(texts)} texts identical", GREEN),
          (f" · {results - removed}/{results} tool results identical", GREEN),
          (f" · {removed} replaced by a recall pointer", WARN)], False),
        ([(f"(messages 0-{shown - 1} shown; long lines are cut for display only)", DIM)], False),
    ], top=footer_y)
    return image


def end_card(report: dict, identical: str) -> Image.Image:
    image, draw = canvas("lcc — Local Context Compiler")
    before, after = report["tokens_before"], report["tokens_after"]
    cut = 1 - after / before
    centred(draw, 90, f"{before:,} → {after:,} tokens  (−{cut:.0%})", 30, FG, bold=True)
    left, right = 170, WIDTH - 170
    span = right - left
    label_font = font(15)
    for y, value, colour, name in ((150, before, RED, "session.json"),
                                   (196, after, GREEN, "compacted.json")):
        draw.text((left - 12, y + 13), name, font=label_font, fill=DIM, anchor="rm")
        draw.rectangle([left, y, left + span * value / before, y + 26], fill=colour)
        draw.text((left + span * value / before + 10, y + 13), f"{value:,}", font=label_font,
                  fill=FG, anchor="lm")
    rows = [
        ("kept text: byte-identical", identical, GREEN),
        ("removed: recoverable", f"{report['tool_calls_stashed']} tool outputs stashed, "
         "`lcc recall <id>` returns them byte for byte", WARN),
        ("receipt", "`lcc explain report.json` says why each one went", ACCENT),
    ]
    y = 270
    for head, text, colour in rows:
        draw.text((left - 40, y), "●", font=font(16), fill=colour)
        draw.text((left - 12, y), head, font=font(17, bold=True), fill=FG)
        draw.text((left - 12, y + 24), text, font=font(14), fill=DIM)
        y += 62
    centred(draw, 482, 'pip install "local-context-compiler[tiktoken]"', 20, GREEN, bold=True)
    centred(draw, 545, f"provider {report['provider_used']}, rules_mode {report['rules_mode']}, "
            f"degraded {str(report['degraded']).lower()} · tokens {report['token_count_method']} "
            "(no tokenizer installed for this run)", 12, DIM)
    centred(draw, 565, "input: demos/session.json, a synthetic Claude Code session", 12, DIM)
    return image


# --------------------------------------------------------------------------- script


def typed(
    history: list[tuple[str, str]], command: str, title: str
) -> list[tuple[Image.Image, int]]:
    """A command typed in five steps, then a beat before Enter."""
    frames = []
    for step in (1, 2, 3, 4):
        cut = len(command) * step // 5
        frames.append((terminal([*history, ("prompt", command[:cut])], title), 40))
    frames.append((terminal([*history, ("prompt", command)], title), 190))
    return frames


def demo_session() -> None:
    if not shutil.which("lcc"):
        raise SystemExit("lcc is not on PATH")
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True)
    shutil.copyfile(ROOT / "demos" / "session.json", WORK / "session.json")

    count = len(json.loads((WORK / "session.json").read_text(encoding="utf-8"))["messages"])
    frames: list[tuple[Image.Image, int]] = [(title_card(count), 1800)]

    # Scene 2: the compaction itself.
    title = f"lcc compact — a {count}-message agent session, offline"
    compact = (
        f'lcc compact session.json -q "{QUESTION}" --mode tool-calls '
        "--provider rules --rules-mode stashed -o compacted.json -r report.json"
    )
    flags = "jq -c '{provider_used, degraded, semantic_guarantee}' report.json"
    history: list[tuple[str, str]] = []
    frames += typed(history, compact, title)
    history += [("prompt", compact), ("out", run(compact)), ("out", "")]
    frames.append((terminal(history, title), 2600))
    frames += typed(history, flags, title)
    history += [("prompt", flags), ("out", run(flags))]
    frames.append((terminal(history, title), 1400))

    original = json.loads((WORK / "session.json").read_text(encoding="utf-8"))["messages"]
    compacted = json.loads((WORK / "compacted.json").read_text(encoding="utf-8"))
    report = json.loads((WORK / "report.json").read_text(encoding="utf-8"))
    if len(original) != len(compacted):
        raise SystemExit("message count changed; the transcript view assumes 1:1 messages")

    # Scene 3: what the compacted session looks like.
    frames.append((transcript_view(original, compacted), 4400))

    # Scene 4: the receipt.
    title = "lcc explain — the receipt for every cut"
    explain = "lcc explain report.json --only replace --limit 3"
    receipt = run(explain).split("\n")
    # Keep the header rows that matter here and the replaced group; the full receipt also
    # lists the 18 kept calls.
    keep_rows = ("lcc explain", "Provider", "Semantic", "Decisions", "Tokens")
    head = [ln for ln in receipt[: receipt.index("REPLACED (5)")] if ln.startswith(keep_rows)]
    body = receipt[receipt.index("REPLACED (5)") :]
    frames += typed([], explain, title)
    frames.append((terminal([("prompt", explain), ("out", "\n".join([*head, "", *body]))],
                            title), 3200))

    # Scene 5: getting a removed output back.
    title = "lcc recall — removed, not lost"
    stash_id = next(
        d["note"].split("lcc recall ")[1].rstrip("]")
        for d in report["decisions"]
        if d["decision"] == "replace" and d["id"] == "toolu_0106"
    )
    recall = f"lcc recall {stash_id} --lines 50-54"
    check = (
        f"lcc recall {stash_id} | cmp - <(jq -j '.messages[12].toolResults[0].text' "
        "session.json) && echo byte-identical"
    )
    history = [("out", "# the first read of app/serializers.py, removed from the session")]
    frames += typed(history, recall, title)
    history += [("prompt", recall), ("out", run(recall))]
    frames.append((terminal(history, title), 1200))
    history += [("out", ""), ("out", "# compare it with the original tool output")]
    frames += typed(history, check, title)
    history += [("prompt", check), ("out", run(check))]
    frames.append((terminal(history, title), 2000))

    texts = [(b, a) for b, a in zip(original, compacted, strict=True) if a.get("text")]
    same = sum(1 for b, a in texts if b["text"] == a["text"])
    identical = f"{same} of {len(texts)} user and assistant texts, compared with the input"
    frames.append((end_card(report, identical), 3400))

    save(frames)


def save(frames: list[tuple[Image.Image, int]]) -> None:
    dest = OUT / "session.gif"
    # One shared adaptive palette for every frame: the frames use a handful of colours, and a
    # single palette keeps the file small and the colours stable from frame to frame.
    sheet = Image.new("RGB", (WIDTH, HEIGHT * len(frames)))
    for index, (frame, _) in enumerate(frames):
        sheet.paste(frame, (0, HEIGHT * index))
    palette = sheet.quantize(colors=64, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    images = [
        frame.quantize(palette=palette, dither=Image.Dither.NONE) for frame, _ in frames
    ]
    images[0].save(
        dest,
        save_all=True,
        append_images=images[1:],
        duration=[duration for _, duration in frames],
        loop=0,
        optimize=True,
    )
    with Image.open(dest) as saved:
        frame_count = getattr(saved, "n_frames", len(images))
    total = sum(duration for _, duration in frames) / 1000
    size_kb = dest.stat().st_size / 1024
    print(f"{dest.relative_to(ROOT)}  {frame_count} frames  {total:.1f} s  {size_kb:.0f} kB")


if __name__ == "__main__":
    demo_session()
