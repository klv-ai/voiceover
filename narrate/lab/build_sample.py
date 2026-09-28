#!/usr/bin/env python3
"""Build a voice's clone sample from EVERYTHING recorded for it.

The sample decides three things about every line the clone speaks, and each
has been got wrong once:

  THE BACKGROUND. The clone copies it. A sample cut from screencasts carried
  56 mouse-and-keyboard clicks a minute, and the renders ticked.

  THE PACE. The clone copies that too. The cleanest sentences of the Voice Lab
  reads were also the slowest — 147 wpm, read off a page — and the renders
  slowed to match. The pace setting cannot undo it: it moves pauses, it does
  not speak the words faster.

  THE VOICE. A sample that wanders in pitch and level is re-read on every
  line, so its wandering becomes the video's.

So score every clip with known words on all three, and choose clips that are
clean, at the speaker's presenting pace, and close to one another in pitch and
level — separate sentences, never neighbours, so there is nothing for the model
to carry on reading. Several candidates are written; `score_samples.py`
decides between them by what they actually produce.

    ../.venv/bin/python3 lab/build_sample.py VOICE_DIR -o OUT_DIR [--wpm 185]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import prosody          # noqa: E402
import render_repair as R  # noqa: E402
from clicks import per_minute  # noqa: E402
from scrub import scrubbed  # noqa: E402


def sentences(t: str):
    return [s for s in re.split(r"(?<=[.!?])\s+", (t or "").strip()) if s]


def pool(voice: str) -> list[dict]:
    """Every recording of this voice whose words are known."""
    items = []
    man = os.path.join(voice, "manifest.jsonl")
    if os.path.exists(man):
        for line in open(man):
            m = json.loads(line)
            f = os.path.join(voice, m.get("audio", ""))
            if m.get("text") and os.path.exists(f):
                items.append((f, m["text"], m.get("project") or "clips", m.get("segment") or ""))
    rs = os.path.join(voice, "read_script.json")
    if os.path.exists(rs):
        script = {p["id"]: p for p in json.load(open(rs))["passages"]}
        for f in sorted(glob.glob(os.path.join(voice, "read_clips", "read_p*s*.wav"))):
            mm = re.search(r"read_(p\d+)s(\d+)", f)
            ss = sentences(script.get(mm.group(1), {}).get("text", ""))
            k = int(mm.group(2))
            if k <= len(ss):
                items.append((f, ss[k - 1], mm.group(1), f"s{k:02d}"))
    return items


def measure(f: str, text: str) -> dict | None:
    y, sr = R.read_wav(f)
    y = np.asarray(y, dtype=np.float32).reshape(-1)
    d = len(y) / sr
    if d < 2.0:
        return None
    speak = max(0.2, d - sum((b - a) for a, b in R._quiet_runs(y, sr)) / sr)
    z = np.interp(np.linspace(0, len(y) - 1, int(len(y) * prosody.SR / sr)),
                  np.arange(len(y)), y).astype(np.float32)
    _t, f0, *_ = prosody.f0_track(z)
    v = f0[~np.isnan(f0)]
    if len(v) < 8:
        return None
    h = int(sr * 0.02)
    e = 20 * np.log10(np.sqrt((y[:len(y) // h * h].reshape(-1, h) ** 2).mean(1)) + 1e-9)
    return {"secs": d, "wpm": len(text.split()) / speak * 60, "cpm": per_minute(y, sr),
            "pitch": float(np.median(v)), "level": float(np.median(e[e > e.max() - 30]))}


def choose(rows, *, wpm, tol, min_secs, total, max_cpm):
    ok = [r for r in rows if r["cpm"] <= max_cpm and abs(r["wpm"] - wpm) <= tol and r["secs"] >= min_secs]
    if not ok:
        return []
    pc = float(np.median([r["pitch"] for r in ok]))
    lc = float(np.median([r["level"] for r in ok]))
    # Distance from the typical clip: a semitone of pitch weighs as much as two
    # decibels of level. What is chosen is a tight cluster — one steady voice.
    for r in ok:
        r["dist"] = abs(12 * np.log2(r["pitch"] / pc)) + abs(r["level"] - lc) / 2.0
    ok.sort(key=lambda r: r["dist"])
    out, used, got = [], set(), 0.0
    for r in ok:
        # Never two neighbouring lines: contiguous material is continued, and
        # the takes open with the sample's own words.
        n = int(re.sub(r"\D", "", r["seg"]) or -99)
        if any((r["src"], n + d) in used for d in (-1, 0, 1)):
            continue
        out.append(r)
        used.add((r["src"], n))
        got += r["secs"]
        if got >= total:
            break
    return out


def write(chosen, path):
    tmp = path + ".parts"
    os.makedirs(tmp, exist_ok=True)
    parts = []
    for i, r in enumerate(chosen):
        q = os.path.join(tmp, f"{i:02d}.wav")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", r["f"], "-ac", "1", "-ar", "48000",
                        "-c:a", "pcm_s16le", q], check=True)
        parts.append(q)
    lst = os.path.join(tmp, "list.txt")
    open(lst, "w").write("".join(f"file '{p}'\n" for p in parts))
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", lst,
                    "-c:a", "pcm_s16le", path], check=True)
    open(os.path.splitext(path)[0] + ".txt", "w").write(" ".join(r["text"] for r in chosen))
    # Scrub it now, so the cache exists before any render asks for it.
    scrubbed(path)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("voice")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--wpm", type=float, default=185.0,
                    help="the presenting pace to match (speech only, pauses excluded)")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    rows = []
    for f, text, src, seg in pool(a.voice):
        m = measure(f, text)
        if m:
            rows.append({"f": f, "text": text, "src": src, "seg": seg, **m})
    print(f"{len(rows)} clips scored", flush=True)

    plans = {
        # One steady voice at the presenting pace, from the cleanest clips.
        "steady": dict(wpm=a.wpm, tol=12, min_secs=3.0, total=36, max_cpm=6),
        # Fewer, longer chunks: more of each to condition on.
        "long": dict(wpm=a.wpm, tol=15, min_secs=5.0, total=36, max_cpm=6),
        # A touch quicker, in case the clone lands under its sample's pace.
        "brisk": dict(wpm=a.wpm + 10, tol=12, min_secs=3.0, total=36, max_cpm=6),
    }
    summary = {}
    for name, plan in plans.items():
        chosen = choose([dict(r) for r in rows], **plan)
        if not chosen:
            print(f"{name}: nothing qualifies")
            continue
        path = os.path.join(a.out, f"sample_{name}.wav")
        write(chosen, path)
        summary[name] = {
            "path": path, "clips": len(chosen), "secs": round(sum(r["secs"] for r in chosen), 1),
            "wpm": round(float(np.mean([r["wpm"] for r in chosen]))),
            "cpm": round(float(np.mean([r["cpm"] for r in chosen])), 1),
            "pitch_spread_st": round(float(np.std([12 * np.log2(r["pitch"] / np.median([c["pitch"] for c in chosen]))
                                                   for r in chosen])), 2),
            "from": sorted({r["src"] for r in chosen}),
            "files": [os.path.relpath(r["f"], a.voice) for r in chosen]}
        s = summary[name]
        print(f"{name:7} {s['clips']:2d} clips {s['secs']:5.1f}s  {s['wpm']} wpm  {s['cpm']} clicks/min  "
              f"pitch spread {s['pitch_spread_st']} st  from {', '.join(s['from'])}", flush=True)
    json.dump(summary, open(os.path.join(a.out, "samples.json"), "w"), indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
