# Demo recordings

The demo GIFs (`session.gif` is the top-level README's hero), plus the inputs and the recipes that
produce them.

| file | what it shows |
|---|---|
| `session.gif` | `lcc compact --mode tool-calls --provider rules --rules-mode stashed` on a 50-message Claude Code session: tokens before and after, the compacted transcript checked against the input, `lcc explain` for the removed outputs, and `lcc recall` bringing one back byte for byte |
| `compact.gif` | `lcc compact` dropping noise from a 23-block dossier, then `lcc explain` showing why each block went |
| `compile.gif` | `lcc optimize` turning a brief dumped from three places into a structured XML prompt |
| `audio-to-prompt.gif` | `lcc intake` cleaning a Whisper-style transcript and compiling it into a prompt |

## Regenerating them

```bash
python3 demos/make_gifs.py            # all four
python3 demos/make_gifs.py session    # one
```

Requires Pillow and `lcc` on PATH. The font is Menlo on macOS, else the first of Fira Code /
DejaVu Sans Mono found; set `LCC_DEMO_FONT=/path/to/font.ttf` to choose one. The script runs the real
commands, captures their real output, and draws it into frames, so the GIFs are a recording of
what the tool actually printed rather than a mock-up. The render is deterministic: regenerating
produces byte-identical files.

`session.gif` comes from `demos/session_gif.py` (the hero needs title and end cards and a
transcript view, so it has its own module on the same palette and font). It copies
`session.json` to `/tmp/lcc-demo-session`, runs every command there with `LCC_STASH_DIR` pointing
inside it, and draws the scenes from what they printed and wrote: the transcript view is read
from the real `compacted.json`, and its "identical" marks are computed by comparing it with
the input. The script stops if a command fails. Token counts are `approximate` when no
tokenizer is installed (the case for the committed GIF, and the end card says so); with
`tiktoken` and a cached encoding the numbers change and so does the file.

### Why rendered rather than recorded

`vhs` 0.12.0 is installed and does **not** work on this machine. It exits `0`, prints
`Creating demos/compact.gif...`, and writes no file. That was reproduced with a one-command tape
(`echo`, `Sleep 2s`) as well as with the real ones, so it is not a tape problem:

- `ttyd 1.7.7` works on its own (serves `HTTP 200`).
- `ffmpeg 9.0.1` works on its own.
- The failure is therefore inside vhs's recording path.

The cause is **not** identified. `ttyd` 1.6.3 is the usual suspect for vhs 0.12, but that release
publishes Linux binaries only, so it could not be tested here without building it. If you can run
`vhs demos/compact.tape` and get a GIF, the tapes are correct and you should use them: they are
the same recipes in the standard format.

The `*.tape` files are checked by `demos/validate_tapes.py`, because `vhs validate` parses a tape
but does not check the command the tape types — a tape can validate and still type something that
fails. A syntax check is not enough either (`lcc compact --provider` is valid bash, and so is a
command whose flags were swallowed by an unbalanced quote), so the script can run them:

```bash
python3 demos/validate_tapes.py          # syntax check, fast
python3 demos/validate_tapes.py --run    # run every command the tapes type
```

`--run` is the check that counts, and it currently passes against the real `lcc`.

## Inputs

| file | purpose |
|---|---|
| `session.json` | a synthetic Claude Code session debugging a booking API: the goal, a failing test log whose `KeyError: 'booking_id'` line matters later, repeated reads of the same files, two fixes, re-runs and a follow-up request (50 messages, 23 tool calls) |
| `make_session.py` | writes `session.json`, deterministically (`python3 demos/make_session.py demos/session.json`) |
| `session_gif.py` | renders `session.gif` from a real run on `session.json` |
| `compact-dossier.md` | a dossier with a stable prefix, tool output, logs, chatter, and one item of each ground-truth category |
| `messy-brief.md` | a brief with repeated paragraphs, page markers, an email signature and a decorative rule |
| `audio-transcript.txt` | a Whisper-style capture with English and Portuguese fillers, `[Music]`, and a subtitle credit |
| `cleaning_steps.py` | prints the cleaning steps from an `lcc optimize` report, so no tape needs nested shell quoting |
| `validate_tapes.py` | checks that the tapes type complete, working commands |
