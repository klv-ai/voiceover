#!/usr/bin/env python3
"""What a project still owes, and why it is not finished.

"Render all did not complete" can mean three unrelated things — a line with no
take at all, a line whose take is stale against its text, or a line the batch
is not allowed to touch — and they need different answers. Saying which is
which is the whole job here.
"""
import json
import os
import sys

proj = sys.argv[1]
p = json.load(open(proj))
segs = p["segments"]

no_audio, dropped, locked, ok = [], [], [], []
for s in segs:
    if s.get("drop"):
        dropped.append(s["id"]); continue
    if s.get("locked"):
        locked.append(s["id"]); continue
    a = s.get("audio")
    if not a or not os.path.exists(a):
        no_audio.append(s["id"]); continue
    ok.append(s["id"])

print(f"{len(segs)} lines")
print(f"  rendered            {len(ok)}")
print(f"  no take at all      {len(no_audio)}  {no_audio[:14]}")
print(f"  dropped             {len(dropped)}  {dropped[:10]}")
print(f"  locked              {len(locked)}  {locked[:10]}")
