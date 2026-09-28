#!/usr/bin/env python3
"""Score checkpoints against each other, on the faults that actually happen.

Auditioned one at a time they all sound like the speaker, which is true and
useless: the checkpoint that filled a video with noise also sounded fine on a
single line. Every fault reported from real renders is INTERMITTENT and shows
up ACROSS lines, never within one - so generate several lines several times
per checkpoint and count.

What is counted, and which complaint each one answers:

    says the line   "17 sounds like HEY", "23 doesn't finish"
    clean tail      "67 ends with an omm", "92 ends in a click + bee"
    pace spread     "80 is next-level fast", "106 and 107 super slow"
    pitch spread    "68 is a completely different voice"
    floor spread    "95-100 sounds like a different room"

A checkpoint is not better for sounding good once. It is better for failing
less often over many tries.

Prints one JSON object per checkpoint, then a final summary line.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--voice-ref", required=True)
    ap.add_argument("--checkpoints", nargs="+", required=True,
                    help="checkpoint dirs; the literal word 'stock' for none")
    ap.add_argument("--lines", nargs="+", required=True)
    ap.add_argument("--seeds", nargs="+", type=int, default=[11, 22, 33])
    ap.add_argument("--out", required=True)
    ap.add_argument("--progress", help="write one JSON line per result HERE")
    # ASSIST OFF by default. Handing the model a sample of the real recording
    # is handing it the accent, the mic distance and the room - so with assist
    # on, every checkpoint sounds alike and scores alike, because most of what
    # is being measured is the sample rather than the training. A checkpoint
    # has to be judged on what it learned.
    # Walk from the earliest checkpoint upward and stop once the run has
    # clearly stopped improving - but not at the FIRST step that scores worse.
    # A real sweep went 44.2, 27.4, 30.8, 24.4, 51.5, 43.7: the 30.8 is a dip,
    # not a peak, and stopping there would have quit one checkpoint before the
    # best of the whole run. Twelve takes cannot tell one wrong line from two,
    # so the dip has to be sustained before it means anything. 0 scores all.
    ap.add_argument("--patience", type=int, default=3,
                    help="stop after N checkpoints in a row that beat nothing")
    ap.add_argument("--assist", action="store_true",
                    help="clone from the reference as well (masks differences)")
    a = ap.parse_args()

    import numpy as np
    import prosody
    import synth_one
    import verify_take
    import render_repair as R

    os.makedirs(a.out, exist_ok=True)

    prog = open(a.progress, "w", buffering=1) if a.progress else None
    if prog:
        # Send the libraries' own chatter to a file too. Model loading writes
        # progress bars to STDERR by the screenful, and a piped stderr fills
        # and blocks exactly like a piped stdout - which is what wedged this
        # after the fix that only closed stdout. Nothing this process writes
        # may depend on somebody reading it.
        log = open(a.progress + ".log", "w", buffering=1)
        os.dup2(log.fileno(), 1)
        os.dup2(log.fileno(), 2)

    def emit(obj):
        """Say it down a pipe AND into a file.

        A pipe is only drained while somebody is listening, and the listener
        here lives in a module that gets replaced on every edit. When that
        happened the child kept writing until the OS buffer filled and then
        BLOCKED - eighty-eight minutes elapsed, eight minutes of CPU, holding
        the card and still reporting itself as running. A file cannot do that
        to us.
        """
        line = json.dumps(obj)
        if prog:
            # ONE destination, not two. Writing to both meant the pipe could
            # still fill and block even with the file in place - which it did,
            # and the process sat in sock_alloc_send_pskb with the card held.
            # A fallback is only a fallback when it is not also the problem.
            prog.write(line + "\n")
        else:
            print(line, flush=True)

    def measure(y, sr=48000):
        h = int(sr * 0.02)
        f = y[:(len(y) // h) * h].reshape(-1, h)
        db = 20 * np.log10(np.sqrt((f ** 2).mean(axis=1)) + 1e-12)
        sp = db[db > db.max() - 30]
        q = db[(db < db.max() - 40) & (db > -119)]
        z = np.interp(np.linspace(0, len(y) - 1, int(len(y) * prosody.SR / sr)),
                      np.arange(len(y)), y).astype(np.float32)
        _t, f0, _d, *_ = prosody.f0_track(z)
        v = f0[~np.isnan(f0)]
        return (float(np.median(sp)) if len(sp) else None,
                float(np.median(q)) if len(q) > 3 else None,
                float(np.median(v)) if len(v) > 8 else None)

    rows = []
    emit({"total": len(a.checkpoints), "lines": len(a.lines),
          "seeds": len(a.seeds), "assist": bool(a.assist),
          "patience": a.patience})
    # Best among the CHECKPOINTS, never counting stock. Stock is the control
    # and it lands mid-table; letting it set the bar would end the walk three
    # checkpoints in and call that a result.
    best, since = None, 0
    for ck in a.checkpoints:
        name = "stock" if ck == "stock" else os.path.join(
            os.path.basename(os.path.dirname(ck)), os.path.basename(ck))
        vo = {} if ck == "stock" else {"lora": ck}
        if vo and not a.assist:
            vo["no_prompt"] = True
        bad = hot = 0
        lv, fl, pi, wp = [], [], [], []
        first = None
        n = 0
        for i, text in enumerate(a.lines):
            for sd in a.seeds:
                tag = ("stock" if ck == "stock" else name.replace("/", "_"))
                p = os.path.join(a.out, f"{tag}_{i}_{sd}.wav")
                try:
                    r = synth_one.synth(text, a.voice_ref, p, target_wpm=0,
                                        seed=sd, provider="voxcpm", voice_opts=vo)
                except Exception as e:
                    emit({"checkpoint": name, "error": str(e)[:200]})
                    bad = -1
                    break
                n += 1
                first = first or p
                v = verify_take.check(p, text)
                if not v.get("ok"):
                    bad += 1
                td = v.get("tail_db")
                if td is not None and td > verify_take.TAIL_FLOOR_DB:
                    hot += 1
                y, sr = R.read_wav(p)
                x, z, c = measure(np.asarray(y, dtype=np.float32).reshape(-1), sr)
                if x is not None: lv.append(x)
                if z is not None: fl.append(z)
                if c is not None: pi.append(c)
                if r.get("speech_wpm"): wp.append(r["speech_wpm"])
            if bad == -1:
                break
        if bad == -1 or not n:
            continue
        sd_of = lambda v: round(statistics.pstdev(v), 2) if len(v) > 2 else None
        row = {"checkpoint": name, "path": ("" if ck == "stock" else ck),
               "takes": n, "wrong": bad, "hot_tail": hot,
               "pitch_sd": sd_of(pi), "level_sd": sd_of(lv),
               "floor_sd": sd_of(fl), "wpm_sd": sd_of(wp),
               "wpm": round(statistics.mean(wp)) if wp else None,
               "sample": first}
        # One number, so a list of ten can be ordered. Faults first because a
        # take that says the wrong words or trails off is not a matter of
        # degree; the spreads are what separates the rest.
        row["score"] = round(
            (bad + hot) * 10
            + (row["pitch_sd"] or 20) * 0.6
            + (row["floor_sd"] or 10) * 0.4
            + (row["wpm_sd"] or 40) * 0.05, 2)
        rows.append(row)
        if ck != "stock":
            if best is None or row["score"] < best:
                best, since = row["score"], 0
            else:
                since += 1
        row["stalled"] = since
        emit(row)
        if a.patience and since >= a.patience:
            emit({"stopped": name, "scored": len(rows),
                  "of": len(a.checkpoints), "stalled": since})
            break

    rows.sort(key=lambda r: r["score"])
    emit({"summary": True, "best": rows[0]["checkpoint"] if rows else None,
          "rows": rows})
    if prog:
        prog.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
