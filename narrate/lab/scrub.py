"""Take the clicks out of a clone sample — once, cached beside it.

The clone copies the sample's background along with the voice, and a sample
cut from a screencast carries its mouse and keyboard: 56 clicks a minute,
reproduced as ticks between words that are heard as stutters.

Two passes of ffmpeg's declicker at a low threshold, chosen by what the clone
PRODUCED rather than by how the sample sounds: over 7 lines x 2 seeds through
the fine-tune, the screencast-cut sample went from 44.4 clicks a minute in the
output to 14.5, kept its speaking pace (182 wpm, where a clean-but-slow sample
gave 174), and gave the steadiest pitch and level of seven candidates. The
default single pass removed a third of the sample's clicks and changed nothing
measurable in the output. Only transients go; the room stays, because a sample
with its floor removed makes the clone invent one.

SLOW — minutes for a 40-second sample — hence the cache, and hence
build_sample.py scrubbing what it writes: a first scrub inside a render
request ran past the worker's six-minute limit and had it restarted.

Shared by the generator and the sample builder, so the setting lives in one
place. Standard library only.
"""
from __future__ import annotations

import os
import subprocess
import sys

SCRUB = "adeclick=threshold=1:burst=4,adeclick=threshold=1"


def cached(wav: str) -> str:
    return os.path.splitext(wav)[0] + ".scrubbed.wav"


def scrubbed(wav: str) -> str:
    """The scrubbed copy of `wav`, made if missing or older than `wav`.
    Never fatal: if it cannot be made, `wav` itself is returned."""
    out = cached(wav)
    try:
        if os.path.exists(out) and os.path.getmtime(out) >= os.path.getmtime(wav):
            return out
        # This process's own temp file: two processes starting a scrub at once
        # (the worker and a scoring run) must not write the same file.
        tmp = f"{out}.{os.getpid()}.tmp.wav"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", wav, "-af", SCRUB,
                        "-c:a", "pcm_s16le", tmp], check=True)
        os.replace(tmp, out)
        return out
    except Exception as e:  # noqa: BLE001
        print(f"[scrub] could not scrub {wav!r} ({e}); using it as it is",
              file=sys.stderr, flush=True)
        return wav


if __name__ == "__main__":
    for w in sys.argv[1:]:
        print(scrubbed(w))
