#!/usr/bin/env python3
"""Cut a take at the end of its last word.

A fine-tuned model can keep generating speech-like sound after the sentence is
over — measured at 20 dB louder than stock, and only about 8 dB under the
speech itself. Nothing level-based can remove that: the head/tail trim in
synth() cuts below -48 dB relative to peak, and this is nowhere near, because
it IS speech. It is simply speech that was not asked for.

The transcriber knows where the words stop, and it has already run on this
take for the text check, so the timestamp costs nothing extra.

Deliberately conservative. A pad is left after the last word so a release or a
soft final consonant survives, the cut is only made when there is a real
amount to remove, and it fades rather than truncates.

    python3 lab/trim_tail.py take.wav --end 3.42
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import render_repair as R  # noqa: E402

PAD = 0.22        # kept after the last word: release, breath, soft consonant
MIN_CUT = 0.30    # below this, not worth touching
FADE = 0.08

# Never cut most of a take away. The transcriber can return a last-word time
# from a fragment — it heard "Today" and stopped — and acting on that took two
# lines down to FORTY MILLISECONDS, which reaches the studio as a block with no
# waveform and no error. A tail is a tail: if what is being removed is a large
# share of the take, the timestamp is wrong, not the audio.
KEEP_MIN = 0.55   # fraction of the original that must survive
FLOOR_S = 0.60    # and never leave less than this in absolute terms


QUIET_RUN = 0.12  # a pause is this much sound under the quiet line in a row


def speech_end(y, sr, heard_end: float) -> float | None:
    """Where the sound actually stops, searching forward from the
    transcriber's last-word time — or None if it never falls into a pause.

    The transcriber's word times end EARLY on the last word: across every
    trimmed take in use, all thirteen were cut while the sound was still at
    speech level, 0.3-0.5 s from the real end — the back half of
    "opportunities", heard as the word being clipped. Its timestamp says where
    to start looking, not where to cut. The cut goes where the audio itself
    falls quiet for a beat; sound running straight on from the last word is
    kept, because it is far more likely the word than babble — a clipped word
    is worse than a take a little long.
    """
    h = int(sr * 0.02)
    n = len(y) // h
    if n < 4:
        return None
    e = 20 * np.log10(np.sqrt((np.asarray(y[:n * h], dtype=np.float64).reshape(n, h) ** 2).mean(1)) + 1e-9)
    floor = float(np.percentile(e, 10))
    speech = float(np.median(e[e > e.max() - 30]))
    quiet = max(floor + 10.0, speech - 30.0)
    need = max(1, int(round(QUIET_RUN / 0.02)))
    run = 0
    for k in range(max(0, int(heard_end / 0.02)), n):
        run = run + 1 if e[k] < quiet else 0
        if run >= need:
            return (k - run + 1) * 0.02
    return None


def trim(path: str, end: float, pad: float = PAD,
         min_cut: float = MIN_CUT) -> dict:
    y, sr = R.read_wav(path)
    total = len(y) / sr
    real = speech_end(y, sr, end)
    if real is None:
        return {"path": path, "trimmed": False, "was": round(total, 3),
                "why": "the sound runs on from the last word without a pause — kept whole"}
    keep = real + pad
    if keep >= total - min_cut:
        return {"path": path, "trimmed": False, "was": round(total, 3),
                "why": "nothing worth removing"}
    if keep < max(FLOOR_S, total * KEEP_MIN):
        # Refuse rather than clamp. A timestamp this far from the end of the
        # take is not a tail measurement that needs rounding, it is a
        # transcription that failed, and trimming to a guessed point would
        # cut real words.
        return {"path": path, "trimmed": False, "was": round(total, 3),
                "why": f"would keep only {keep:.2f}s of {total:.2f}s — "
                       f"the transcript is not trustworthy here"}
    cut = int(keep * sr)
    out = y[:cut].copy()
    n = min(int(FADE * sr), len(out))
    if n:
        out[-n:] *= np.linspace(1.0, 0.0, n, dtype=np.float32)
    R.write_wav(path, out, sr)
    return {"path": path, "trimmed": True, "was": round(total, 3),
            "now": round(len(out) / sr, 3),
            "removed": round(total - len(out) / sr, 3)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("wav")
    ap.add_argument("--end", type=float, required=True,
                    help="seconds at which the last word ends")
    ap.add_argument("--pad", type=float, default=PAD)
    a = ap.parse_args()
    print(json.dumps(trim(a.wav, a.end, a.pad)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
