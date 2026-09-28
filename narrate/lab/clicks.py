"""Clicks in the quiet stretches of a recording — the ticking a clone copies.

A clone given a sample of the real recording copies its background along with
the voice, clicks included, and a click beside a word is heard as a stutter.
Measured on the same lines and seeds: a sample cut from screencasts (56 clicks
a minute — mouse and keyboard) gave 43.9 a minute in the output; a sample with
10 a minute gave 27.3.

Counted only where it is quiet, because that is where the ear finds them:
speech masks a click, a pause exposes it.
"""
from __future__ import annotations

import numpy as np


def clicks(y, sr) -> list[float]:
    """Times (s) of short broadband spikes standing out from the 120 ms around
    them, in stretches at least 30 dB under the loudest part of the file."""
    y = np.asarray(y, dtype=np.float32).reshape(-1)
    hp = np.diff(y, prepend=y[:1])                  # clicks live at the top of the spectrum
    w = int(sr * 0.002)
    m = len(hp) // w
    if m < 70:
        return []
    e = np.abs(hp[:m * w]).reshape(m, w).max(1)
    lvl = np.sqrt((y[:m * w].reshape(m, w) ** 2).mean(1))
    top = 20 * np.log10(lvl.max() + 1e-9)
    k, out = 30, []
    for i in range(k, m - k):
        loc = np.median(e[i - k:i + k]) + 1e-9
        quiet = 20 * np.log10(np.median(lvl[i - k:i + k]) + 1e-9) < top - 30
        if quiet and e[i] > loc * 5 and (not out or i - out[-1] > 5):
            out.append(i)
    return [round(i * 0.002, 3) for i in out]


def per_minute(y, sr) -> float:
    d = len(np.asarray(y).reshape(-1)) / float(sr)
    return len(clicks(y, sr)) / d * 60 if d > 0 else 0.0
