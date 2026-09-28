#!/usr/bin/env python3
"""Which takes are too short to contain the line they are supposed to say.

Length alone does not say a take is broken — a four-word line IS short. What
says it is broken is the ratio: how long the audio is against how long those
words could possibly take to speak. Nobody says six words in a second.
"""
import json
import os
import sys
import wave

FAST_WPM = 260.0     # faster than anyone narrates; a floor on plausible length
RATIO = 0.60         # below this share of the floor, the take cannot be whole

proj = sys.argv[1]
p = json.load(open(proj))
bad = []
for s in p["segments"]:
    if s.get("drop") or s.get("locked"):
        continue
    a = s.get("audio")
    words = len(s["say"].split())
    if not words:
        continue
    floor = words / FAST_WPM * 60.0
    if not a or not os.path.exists(a):
        bad.append((s["id"], None, floor, s["say"]))
        continue
    with wave.open(a) as w:
        d = w.getnframes() / w.getframerate()
    if d < floor * RATIO:
        bad.append((s["id"], d, floor, s["say"]))

print(f"{len(bad)} take(s) too short for their line\n")
print(f"{'line':6} {'have':>6} {'floor':>7}  text")
for sid, d, floor, say in bad:
    print(f"{sid:6} {('--' if d is None else f'{d:.2f}'):>6} {floor:7.2f}  {say[:52]!r}")
print()
print(",".join(sid for sid, *_ in bad))
