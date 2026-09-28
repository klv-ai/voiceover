#!/usr/bin/env python3
"""Profile wavs: length, speech level, noise floor, signal-to-noise, clipping,
and holes of digital silence — what the studio shows beside a voice's sample
and each read.

    python3 lab/measure_audio.py a.wav b.wav        # one JSON array on stdout

Measured on the TRIMMED span, never the whole file: leading silence flatters a
signal-to-noise figure by ten decibels, and the browser's recorder starts every
take with about 650 ms of true digital zero that nobody keeps.
"""
from __future__ import annotations

import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import render_repair as R  # noqa: E402

WIN = 0.02              # 20 ms analysis window, as everywhere else in the lab
MARGIN = 0.12           # silence left at each end of the measured span


def db(v: float) -> float:
    return 20.0 * np.log10(max(float(v), 1e-9))


def _ceiling_run(x: np.ndarray, sr: int) -> float:
    """Longest unbroken stretch pinned to full scale, in milliseconds.

    Clipping is a RUN of samples at the ceiling, never a peak value: takes
    arrive peak-normalised, so most touch full scale on a sample or two."""
    at = np.abs(x) >= 0.999
    if not at.any():
        return 0.0
    d = np.diff(np.concatenate(([0], at.view(np.int8), [0])))
    starts, ends = np.where(d == 1)[0], np.where(d == -1)[0]
    return float((ends - starts).max()) / sr * 1000.0


def extent(x: np.ndarray, sr: int) -> tuple[float, float] | None:
    """Where the speech starts and stops: frames above 6% of the loudest."""
    win = max(1, int(sr * WIN))
    f = x[:len(x) // win * win].reshape(-1, win)
    if not len(f):
        return None
    rms = np.sqrt((f ** 2).mean(axis=1))
    thr = max(rms.max() * 0.06, 200.0 / 32768.0)
    on = np.where(rms > thr)[0]
    if not len(on):
        return None
    return on[0] * WIN, min(len(x) / sr, (on[-1] + 1) * WIN)


def _holes(x: np.ndarray, sr: int) -> dict:
    """Stretches of true digital zero, and how much of the clip they are.

    Invisible otherwise: a level meter reads a hole as a spectacular noise
    floor, while the model is being asked to learn a voice from audio that
    repeatedly stops existing."""
    at = np.abs(x) < 1e-6
    if not at.any():
        return {"zero_pct": 0.0, "holes": 0, "hole_s": 0.0}
    d = np.diff(np.concatenate(([0], at.view(np.int8), [0])))
    runs = (np.where(d == -1)[0] - np.where(d == 1)[0]) / sr
    big = runs[runs > 0.1]
    return {"zero_pct": round(float(at.sum()) / len(x) * 100.0, 2),
            "holes": int(len(big)), "hole_s": round(float(big.sum()), 2)}


def profile(x: np.ndarray, sr: int) -> dict:
    """Level and noise floor of one trimmed clip."""
    win = max(1, int(sr * WIN))
    f = x[:len(x) // win * win].reshape(-1, win)
    if not len(f):
        return {}
    rms = np.sqrt((f ** 2).mean(axis=1)) + 1e-9
    ranked = np.sort(rms)
    floor = ranked[int(len(ranked) * 0.05)]
    speech = ranked[int(len(ranked) * 0.90)]
    return {
        "floor_db": db(floor),
        "speech_db": db(speech),
        "snr_db": db(speech) - db(floor),
        "peak": float(np.abs(x).max()),
        "clip_ms": _ceiling_run(x, sr),
        "clip_pct": float((np.abs(x) >= 0.999).sum()) / max(len(x), 1) * 100.0,
        "speech_s": len(x) / sr - 2 * MARGIN,
        **_holes(x, sr),
    }


def measure(paths: list[str]) -> list[dict]:
    out = []
    for path in paths:
        row: dict = {"path": path}
        try:
            x, sr = R.read_wav(path)
            span = extent(x, sr)
            if span:
                a = max(0, int((span[0] - MARGIN) * sr))
                b = min(len(x), int((span[1] + MARGIN) * sr))
                row.update(profile(x[a:b], sr))
            row["seconds"] = round(len(x) / sr, 2)
        except Exception as e:  # noqa: BLE001 — one bad file must not hide the rest
            row["error"] = str(e)
        out.append(row)
    return out


if __name__ == "__main__":
    print(json.dumps(measure(sys.argv[1:])))
