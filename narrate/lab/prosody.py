"""Prosody measurement: what makes a delivery read as hesitant rather than flat.

The repair pipeline in narrate/ removes the *sounds* of hesitation — the ums,
the restarts, the dead air. What it cannot touch is the shape of the delivery
underneath: where the pitch goes at the end of a sentence, whether energy is
sustained or trails away, how much melodic range there is at all. That shape is
most of what a listener hears as confidence, and it survives every edit we make.

This measures it, so that any intervention is aimed at something real.

  F0            YIN (cumulative mean normalised difference), 10ms hop
  terminal      slope of the last 400ms of voiced pitch, in semitones/second
  range         interquartile pitch range in semitones
  sustain       energy slope over the final third of a phrase, dB/second
"""
from __future__ import annotations

import json
import subprocess
import sys
import wave

import numpy as np

SR = 16000            # plenty for F0 up to 400 Hz
HOP = 0.010
FRAME = 0.040
F0_MIN, F0_MAX = 70.0, 350.0
YIN_THRESH = 0.15


def load(path: str) -> np.ndarray:
    """Decode any media to mono float32 at SR."""
    if not path.endswith(".wav"):
        tmp = "/tmp/_prosody.wav"
        subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-map", "0:a:0", "-vn",
                        "-ac", "1", "-ar", str(SR), "-c:a", "pcm_s16le", tmp, "-y"],
                       check=True)
        path = tmp
    with wave.open(path, "rb") as w:
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        sr = w.getframerate()
    x = x.astype(np.float32) / 32768.0
    if sr != SR:
        idx = np.linspace(0, len(x) - 1, int(len(x) * SR / sr))
        x = np.interp(idx, np.arange(len(x)), x).astype(np.float32)
    return x


def f0_track(x: np.ndarray):
    """YIN. Returns (times, f0 with NaN where unvoiced, rms in dB)."""
    n = int(FRAME * SR)
    hop = int(HOP * SR)
    tau_min, tau_max = int(SR / F0_MAX), int(SR / F0_MIN)
    starts = np.arange(0, max(0, len(x) - n), hop)
    f0 = np.full(len(starts), np.nan, dtype=np.float32)
    db = np.full(len(starts), -120.0, dtype=np.float32)
    ap = np.full(len(starts), 1.0, dtype=np.float32)   # aperiodicity: YIN's CMND at the chosen lag

    nfft = 1 << int(np.ceil(np.log2(2 * n)))
    for i, s in enumerate(starts):
        fr = x[s:s + n]
        power = float(np.dot(fr, fr))
        db[i] = 10 * np.log10(max(power / n, 1e-12))
        if power < 1e-6:
            continue
        # difference function via FFT autocorrelation
        sp = np.fft.rfft(fr, nfft)
        acf = np.fft.irfft(sp * np.conj(sp), nfft)[:tau_max + 1]
        cs = np.concatenate(([0.0], np.cumsum(fr * fr)))
        taus = np.arange(tau_max + 1)
        pow_head = cs[n - taus]                      # sum of x[0:n-tau]^2
        pow_tail = cs[n] - cs[taus]                  # sum of x[tau:n]^2
        d = pow_head + pow_tail - 2 * acf
        d[0] = 0.0
        cum = np.cumsum(d[1:])
        cmnd = np.ones_like(d)
        cmnd[1:] = d[1:] * taus[1:] / np.maximum(cum, 1e-12)
        # YIN: take the first tau under threshold, then walk down to the local
        # minimum. Stopping at the first sub-threshold value alone biases the
        # period long — measured 2.5% flat on synthetic tones.
        cand = np.where(cmnd[tau_min:tau_max] < YIN_THRESH)[0]
        if len(cand):
            tau = int(cand[0] + tau_min)
            while tau + 1 < tau_max and cmnd[tau + 1] < cmnd[tau]:
                tau += 1
        else:
            tau = int(np.argmin(cmnd[tau_min:tau_max]) + tau_min)
        ap[i] = float(cmnd[int(round(tau))]) if int(round(tau)) < len(cmnd) else 1.0
        if cmnd[tau] > 0.6:
            continue
        # parabolic refinement
        if 0 < tau < tau_max - 1:
            a, b, c = cmnd[tau - 1], cmnd[tau], cmnd[tau + 1]
            denom = a - 2 * b + c
            if abs(denom) > 1e-9:
                tau = tau + 0.5 * (a - c) / denom
        f0[i] = SR / tau
    return starts / SR, f0, db, ap


def semitones(f: np.ndarray) -> np.ndarray:
    return 12.0 * np.log2(np.clip(f, 1e-6, None) / 100.0)


def tail_is_reliable(t, f0, ap, start, end, tail=0.45,
                     max_ap=0.32, max_jitter=0.75) -> bool:
    """Is the pitch measured at this phrase's tail trustworthy?

    Phrase-final creak is normal in English and it wrecks F0 estimation: the
    signal keeps its energy but loses periodicity, and the tracker starts
    producing octave jumps. Across these videos tails are only mildly noisier
    than phrase bodies on average (0.29 vs 0.20 semitones of frame-to-frame
    jitter), so the uptalk finding survives — but individual phrases vary a
    lot, and correcting a tail whose contour is measurement noise means bending
    the voice to fix something that was never there.
    """
    m = (t >= start) & (t <= end) & ~np.isnan(f0)
    if m.sum() < 8:
        return False
    tv = t[m]
    sel = tv >= tv[-1] - tail
    if sel.sum() < 4:
        return False
    st = semitones(f0[m])[sel]
    return (float(ap[m][sel].mean()) <= max_ap
            and float(np.median(np.abs(np.diff(st)))) <= max_jitter)


def terminal_step(t, f0, start, end, tail=0.22, ref=0.45):
    """Robust terminal measure: how far the LAST `tail` seconds sit above the
    `ref` seconds before them, in semitones. Positive means the phrase ends
    higher than it was — uptalk.

    A least-squares slope over the final 400ms sounded like the natural
    measure and is not usable as a control signal: phrase-final frames jitter
    by several semitones (octave errors around creak), so the fitted slope
    swings wildly between neighbouring phrases and between takes of the same
    audio. Comparing two medians is insensitive to those outliers.
    """
    m = (t >= start) & (t <= end) & ~np.isnan(f0)
    if m.sum() < 8:
        return float("nan")
    tv, st = t[m], semitones(f0[m])
    t_end = tv[-1]
    last = tv >= t_end - tail
    prev = (tv >= t_end - tail - ref) & (tv < t_end - tail)
    if last.sum() < 3 or prev.sum() < 3:
        return float("nan")
    return float(np.median(st[last]) - np.median(st[prev]))


def phrase_stats(t, f0, db, start, end, tail=0.40):
    """Prosody of one transcript segment."""
    m = (t >= start) & (t <= end)
    if m.sum() < 6:
        return None
    ft, fv, dv = t[m], f0[m], db[m]
    voiced = ~np.isnan(fv)
    if voiced.sum() < 5:
        return None
    st = semitones(fv[voiced])
    tv = ft[voiced]

    # terminal pitch slope: last `tail` seconds of voiced frames
    tm = tv >= (tv[-1] - tail)
    slope = np.nan
    if tm.sum() >= 3:
        slope = float(np.polyfit(tv[tm], st[tm], 1)[0])     # semitones / second

    # energy sustain over the final third
    third = ft >= (start + (end - start) * 2 / 3)
    sustain = np.nan
    if third.sum() >= 3:
        sustain = float(np.polyfit(ft[third], dv[third], 1)[0])   # dB / second

    return {
        "terminal_step": terminal_step(t, f0, start, end),
        "median_st": float(np.median(st)),
        "range_st": float(np.percentile(st, 75) - np.percentile(st, 25)),
        "terminal_slope": slope,
        "sustain": sustain,
        "voiced_frac": float(voiced.mean()),
        "dur": end - start,
    }


def measure(analysis_path: str):
    d = json.load(open(analysis_path))
    src = d["source"]["path"]
    x = load(src)
    t, f0, db, ap = f0_track(x)
    rows = []
    for s in d["transcript"]["segments"]:
        r = phrase_stats(t, f0, db, s["start"], s["end"])
        if r:
            r["text"] = s["text"]
            r["start"] = s["start"]
            rows.append(r)
    return d, rows


def is_statement_end(text: str) -> bool:
    """A rising terminal only means uptalk at the end of a STATEMENT.

    Whisper breaks segments mid-sentence constantly, and a rise there is a
    normal continuation cue, not hesitancy. Questions legitimately rise too.
    Counting either as uptalk inflates the number and would have us "fixing"
    correct prosody.
    """
    t = text.rstrip()
    return t.endswith((".", "!")) and not t.endswith("?")


def summarise(name, rows):
    import statistics as st
    ends = [r for r in rows if is_statement_end(r.get("text", ""))
            and r["terminal_slope"] == r["terminal_slope"]]
    mids = [r for r in rows if not is_statement_end(r.get("text", ""))
            and r["terminal_slope"] == r["terminal_slope"]]
    sust = [r["sustain"] for r in ends if r["sustain"] == r["sustain"]]
    rng = [r["range_st"] for r in rows]
    rising = sum(1 for r in ends if r["terminal_slope"] > 1.0)
    return {
        "name": name,
        "phrases": len(rows),
        "statements": len(ends),
        "median_range_st": st.median(rng) if rng else 0,
        "terminal_statement": st.median([r["terminal_slope"] for r in ends]) if ends else float("nan"),
        "terminal_midphrase": st.median([r["terminal_slope"] for r in mids]) if mids else float("nan"),
        "uptalk_pct": 100.0 * rising / max(1, len(ends)),
        "median_sustain": st.median(sust) if sust else 0,
    }


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "measure":
        out = []
        for p in sys.argv[2:]:
            d, rows = measure(p)
            import os
            out.append(summarise(os.path.basename(d["source"]["path"]), rows))
            json.dump(rows, open(p.replace(".analysis.json", ".prosody.json"), "w"))
        print(f"  {'video':<20}{'phr':>5}{'stmt':>6}{'range st':>10}"
              f"{'end-of-stmt':>13}{'mid-phrase':>12}{'uptalk%':>9}{'sustain':>9}")
        for r in out:
            print(f"  {r['name']:<20}{r['phrases']:5d}{r['statements']:6d}"
                  f"{r['median_range_st']:10.2f}{r['terminal_statement']:13.2f}"
                  f"{r['terminal_midphrase']:12.2f}{r['uptalk_pct']:9.0f}{r['median_sustain']:9.1f}")
