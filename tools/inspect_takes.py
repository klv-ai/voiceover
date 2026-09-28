#!/usr/bin/env python3
"""What is actually in a project's rendered takes — length and level.

A take that was cut too short is not visible in the studio as an error; it is
visible as a block with no waveform, which is the same thing a failed
generation looks like.
"""
import json
import os
import sys
import wave

import numpy as np

proj = sys.argv[1]
want = {int(x) for x in sys.argv[2:]} if len(sys.argv) > 2 else None
p = json.load(open(proj))
print(f"{'line':6} {'file s':>7} {'raw s':>7} {'peak':>7} {'rms dB':>8}  text")
for s in p["segments"]:
    n = int(s["id"][1:])
    if want and n not in want:
        continue
    a = s.get("audio")
    if not a or not os.path.exists(a):
        print(f"{s['id']:6} {'MISSING':>7}")
        continue
    with wave.open(a) as w:
        d = w.getnframes() / w.getframerate()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    raw = a.replace(".wav", ".raw.wav")
    rd = None
    if os.path.exists(raw):
        with wave.open(raw) as w:
            rd = w.getnframes() / w.getframerate()
    peak = float(np.abs(x).max()) if x.size else 0.0
    rms = 20 * np.log10(max(float(np.sqrt((x ** 2).mean())), 1e-9)) if x.size else -99
    flag = "  <-- SHORT" if rd and d < rd * 0.75 else ""
    print(f"{s['id']:6} {d:7.2f} {rd if rd else 0:7.2f} {peak:7.3f} {rms:8.1f}  "
          f"{s['say'][:40]!r}{flag}")
