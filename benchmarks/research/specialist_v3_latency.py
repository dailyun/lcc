"""Latency / peak-RSS check for the specialist-v3 ranker on synthetic transcripts.

Budgets (from the v3 test protocol, which timed the judge alone): ranker median < 250 ms on a
transcript of the held-out median size (~300 blocks), peak RSS < 300 MB. The largest held-out
size (~1150 blocks) is reported, not gated. Full ``compact_context`` passes are shown next to
the mechanical provider on the same input, so the ranker's share of a pass is visible.

    python benchmarks/research/specialist_v3_latency.py [--repeats 9]
"""

from __future__ import annotations

import argparse
import json
import resource
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from lcc.relevance import RelevanceCompactionRequest, compact_context  # noqa: E402
from lcc.relevance.blocks import split_blocks  # noqa: E402
from lcc.relevance.specialist import load_judge, meta_for_blocks, render_transcript  # noqa: E402
from lcc.relevance.transcript import parse_transcript  # noqa: E402
from specialist_synthetic import GOAL, synthetic_payload  # noqa: E402

BUDGET_MS, BUDGET_RSS_MB = 250.0, 300.0


def _median_ms(fn, repeats: int) -> float:
    times = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        fn()
        times.append((time.perf_counter() - t0) * 1000)
    return round(statistics.median(times), 1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeats", type=int, default=9)
    args = ap.parse_args()
    started = time.perf_counter()
    judge = load_judge()
    out: dict[str, object] = {"load_ms": round((time.perf_counter() - started) * 1000, 1)}
    ok = True
    for label, turns in (("median_size", 15), ("largest_size", 56)):
        text, segments = render_transcript(parse_transcript(synthetic_payload(seed=5, turns=turns)))
        blocks = split_blocks(text)
        meta = meta_for_blocks([(b.character_start, b.character_end) for b in blocks], segments)
        texts = [b.text for b in blocks]
        ranker = _median_ms(lambda: judge.score(GOAL, texts, meta), args.repeats)  # noqa: B023
        passes = {}
        for provider in ("specialist-v3", "mechanical"):
            request = RelevanceCompactionRequest(
                text=text, question=GOAL, provider=provider, segments=segments
            )
            passes[provider] = _median_ms(lambda: compact_context(request), 3)  # noqa: B023
        out[label] = {
            "blocks": len(blocks),
            "chars": len(text),
            "ranker_ms_median": ranker,
            "full_pass_ms_median": passes,
        }
        if label == "median_size":
            ok = ok and ranker < BUDGET_MS
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    out["peak_rss_mb"] = round(rss / (2**20 if sys.platform == "darwin" else 2**10), 1)
    ok = ok and float(out["peak_rss_mb"]) < BUDGET_RSS_MB  # type: ignore[arg-type]
    out["pass"] = ok
    print(json.dumps(out, indent=1))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
