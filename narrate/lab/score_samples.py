#!/usr/bin/env python3
"""Choose a clone sample by what the clone makes of it.

A sample is judged by listening to it at our peril: the one that sounded
cleanest also slowed every render by 14%. So generate the same lines with the
same seeds from each candidate, through the same fine-tune, and measure what
comes out:

    wrong      takes that did not say the line, or said a product name wrong
    clicks     per minute of output, in the quiet stretches — the ticking
    pace       speaking rate, pauses excluded, against the target
    pace sd    how much that rate wanders from take to take
    pitch sd   how far each take's median pitch strays, in semitones —
               "a different voice in the same paragraph"
    level sd   the same for loudness, in dB

Progress goes to a FILE, one JSON line per result, never a pipe (see
compare_ckpts.py for what an undrained pipe did).

    ../.venv-tts/bin/python3 lab/score_samples.py --samples a.wav b.wav \\
        --lora CKPT --lines "…" "…" --out DIR --progress DIR/progress.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


# A sample that says the wrong words in more than this share of takes is set
# aside. Below it, wrong words do not rank samples at all.
WRONG_GATE = 0.2


def score_row(row: dict, target: float) -> tuple[float, bool]:
    """(score, set aside) — lower scores are better.

    Wrong words are a GATE, not a weight. Fourteen takes cannot tell word-error
    rates apart — even 0 against 3 is well within chance — and the render
    retries a take that says the wrong words, so a slip costs time rather than
    quality. Weighted ten points a word, one slip in fourteen handed the star
    to a sample with 70% more clicks and more wander than the one the
    operator's ear kept. What ranks is what is heard in EVERY take: clicks,
    distance from the presenting pace, and how much pace, pitch and level
    wander. Checked against both choices made by ear so far; it picks the
    same sample each time.
    """
    gated = row["wrong"] / max(1, row["takes"]) > WRONG_GATE
    score = (row["clicks_per_min"] * 0.3 + abs(row["wpm"] - target) * 0.25
             + row["wpm_sd"] * 0.3 + (row["pitch_sd_st"] if row["pitch_sd_st"] is not None else 3) * 4
             + row["level_sd_db"] * 2)
    return round(score, 2), gated


def rescore(path: str, target: float) -> int:
    """Re-rank a finished run's progress file under the current rule."""
    out, rows = [], []
    for line in open(path):
        t = line.strip()
        if not t.startswith("{"):
            continue
        m = json.loads(t)
        if m.get("sample"):
            m["score"], m["gated"] = score_row(m, target)
            rows.append(m)
        if not m.get("summary"):
            out.append(m)
    rows.sort(key=lambda r: (r["gated"], r["score"]))
    out.append({"summary": True, "best": rows[0]["sample"] if rows else None, "rows": rows})
    with open(path, "w") as f:
        for m in out:
            f.write(json.dumps(m) + "\n")
    for r in rows:
        print(f"{r['sample']:24} {r['score']:6.2f}{'  set aside: wrong words' if r['gated'] else ''}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", nargs="+", required=True)
    ap.add_argument("--lines", nargs="+", required=True)
    ap.add_argument("--seeds", nargs="+", type=int, default=[101, 202])
    ap.add_argument("--lora", default="")
    ap.add_argument("--wpm", type=float, default=185.0)
    ap.add_argument("--out", required=True)
    ap.add_argument("--progress")
    ap.add_argument("--rescore", help="re-rank an existing progress file and exit")
    if "--rescore" in sys.argv:
        r = argparse.ArgumentParser()
        r.add_argument("--rescore", required=True)
        r.add_argument("--wpm", type=float, default=185.0)
        ra, _ = r.parse_known_args()
        return rescore(ra.rescore, ra.wpm)
    a = ap.parse_args()

    import prosody
    import synth_one
    import verify_take
    import render_repair as R
    from clicks import clicks

    os.makedirs(a.out, exist_ok=True)
    prog = open(a.progress, "w", buffering=1) if a.progress else None
    if prog:
        log = open(a.progress + ".log", "w", buffering=1)
        os.dup2(log.fileno(), 1)
        os.dup2(log.fileno(), 2)

    def emit(obj):
        (prog.write(json.dumps(obj) + "\n") if prog else print(json.dumps(obj), flush=True))

    rows = []
    for sample in a.samples:
        name = os.path.splitext(os.path.basename(sample))[0]
        wrong, n_clicks, secs = 0, 0, 0.0
        wpm, pitch, level = [], [], []
        for i, text in enumerate(a.lines):
            for sd in a.seeds:
                p = os.path.join(a.out, f"{name}_{i}_{sd}.wav")
                synth_one.synth(text, sample, p, target_wpm=0, seed=sd, provider="voxcpm",
                                voice_opts=({"lora": a.lora} if a.lora else {}))
                v = verify_take.check(p, text)
                if (not v.get("ok") and v.get("why") != "tail") or v.get("must_say"):
                    wrong += 1
                y, sr = R.read_wav(p)
                y = np.asarray(y, dtype=np.float32).reshape(-1)
                d = len(y) / sr
                secs += d
                n_clicks += len(clicks(y, sr))
                speak = max(0.2, d - sum((b - x) for x, b in R._quiet_runs(y, sr)) / sr)
                wpm.append(len(text.split()) / speak * 60)
                z = np.interp(np.linspace(0, len(y) - 1, int(len(y) * prosody.SR / sr)),
                              np.arange(len(y)), y).astype(np.float32)
                _t, f0, *_ = prosody.f0_track(z)
                f = f0[~np.isnan(f0)]
                if len(f) > 8:
                    pitch.append(float(np.median(f)))
                h = int(sr * 0.02)
                e = 20 * np.log10(np.sqrt((y[:len(y) // h * h].reshape(-1, h) ** 2).mean(1)) + 1e-9)
                level.append(float(np.median(e[e > e.max() - 30])))
        pc = statistics.median(pitch) if pitch else 1.0
        row = {"sample": name, "path": sample, "takes": len(a.lines) * len(a.seeds),
               "wrong": wrong, "clicks_per_min": round(n_clicks / secs * 60, 1),
               "wpm": round(statistics.mean(wpm)), "wpm_sd": round(statistics.pstdev(wpm), 1),
               "pitch_sd_st": round(statistics.pstdev([12 * np.log2(x / pc) for x in pitch]), 2) if pitch else None,
               "level_sd_db": round(statistics.pstdev(level), 2)}
        row["score"], row["gated"] = score_row(row, a.wpm)
        rows.append(row)
        emit(row)
    rows.sort(key=lambda r: (r["gated"], r["score"]))
    emit({"summary": True, "best": rows[0]["sample"] if rows else None, "rows": rows})
    return 0


if __name__ == "__main__":
    sys.exit(main())
