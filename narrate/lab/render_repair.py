"""Audio helpers shared by the lab scripts: wav in and out, quiet-run
detection, and two ways of changing a take's length — slowing it toward a
rate, and fitting it to a duration by growing or shrinking its pauses.

Once the batch renderer for the first engine; that renderer is gone and only
these helpers are still used.
"""
from __future__ import annotations

import math
import os
import subprocess
import tempfile
import wave

import numpy as np

SR = 48000


def sh(cmd):
    subprocess.run(cmd, check=True, capture_output=True)


def read_wav(path):
    with wave.open(path, "rb") as w:
        n, ch, sw = w.getnframes(), w.getnchannels(), w.getsampwidth()
        x = np.frombuffer(w.readframes(n), dtype=np.int16).astype(np.float32) / 32768.0
        if ch > 1:
            x = x.reshape(-1, ch).mean(axis=1)
        return x, w.getframerate()


# Leave a little headroom on everything written. Matching, tone and tempo all
# add a fraction of a decibel here and there, and a file that arrives at
# exactly full scale has nowhere to put the next one.
HEADROOM = 0.97


def write_wav(path, x, sr=SR, headroom=HEADROOM):
    """Write audio, turning it DOWN rather than squaring it off.

    `np.clip` at the point of writing is silent damage: a take that came out of
    the model a little hot, then gained 6 dB from the channel match and a shelf
    from the warmth control, lands above full scale, and every sample past the
    limit is flattened into the one before it. That is not loudness, it is
    distortion — 21 milliseconds of it on one line here, heard as a spike in
    the waveform and a rasp in the voice, with nothing anywhere reporting a
    problem.

    Scaling the whole buffer costs a fraction of a decibel nobody can hear and
    keeps the waveform intact. Clipping is only ever a last resort against
    arithmetic error, a sample or two past the edge.
    """
    x = np.asarray(x, dtype=np.float64)
    peak = float(np.abs(x).max()) if x.size else 0.0
    if peak > headroom:
        x = x * (headroom / peak)
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes(np.clip(x * 32768, -32768, 32767).astype(np.int16).tobytes())


def rms(x):
    return float(np.sqrt(np.mean(x ** 2))) if len(x) else 0.0


def db(x):
    return 20 * math.log10(max(x, 1e-9))


def slow_to_rate(y, sr, n_words, target_wpm=155.0, floor_tempo=0.90,
                 ceil_tempo=1.12):
    """Bring a generation down to a natural speaking rate, by one uniform
    time-stretch and nothing else.

    Chatterbox delivers around 222 wpm whatever `cfg_weight` says (measured
    201-249 across 0.2-0.7, no trend), where comfortable narration is 150-160.

    An earlier version tried to be clever: put the extra time into the model's
    own internal pauses, on the theory that people slow down by pausing rather
    than drawling. The theory is right and the implementation was not — it
    filled each pause with `np.resize` of the quiet fragment beside it, which
    TILES a 45ms chunk ten times and produces a ~22 Hz flutter. No sharp edges,
    so no click by any discontinuity measure, but heard as the voice stuttering:
    "Voice,,,,,,,, pack,,,,,". It also made the rate uneven, because sentences
    with few internal pauses fell back on a deep stretch while others did not.

    Measured, a uniform stretch costs nothing here: aperiodicity against the
    source moves -0.015 at 0.90x and -0.036 at 0.75x — it gets *cleaner*, not
    worse. So do the simple thing.

    BUT KEEP THE STRETCH SMALL. The bounds were 0.72-1.35, wide enough to force
    every line onto the same words-per-minute — and that is the wrong target.
    The model does not generate at a constant rate, so hitting one number means
    a DIFFERENT stretch on every line: the ones it produced quickly get drawled
    and the ones it produced slowly get rushed. Uniform wpm, wildly uneven
    delivery, heard as "too fast and then too slow" inside a single render.
    A narrow band keeps every line near its own natural articulation and lets
    the wpm land where it falls. Timing does not need to be exact here — the
    gaps are glided away downstream regardless.
    """
    dur = len(y) / sr
    if dur <= 0.2 or n_words < 2:
        return y, 1.0, 0
    want = n_words / target_wpm * 60.0
    # It must also be able to SPEED UP. This speaker reads at ~240 wpm and the
    # model came back at 174 on a long line, so "slow to a natural rate" left
    # every line 1.4x too long — heard as mistimed against his own read.
    if want <= dur * 1.02 and want >= dur * 0.98:
        return y, 1.0, 0
    if want < dur * 0.98:
        tempo = min(ceil_tempo, dur / max(want, 1e-6))
    else:
        tempo = max(floor_tempo, dur / want)
    a_, b_ = tempfile.mktemp(suffix=".wav"), tempfile.mktemp(suffix=".wav")
    write_wav(a_, y, sr)
    sh(["ffmpeg", "-v", "error", "-i", a_, "-af", f"atempo={tempo:.6f}",
        "-c:a", "pcm_s16le", b_, "-y"])
    y2, _ = read_wav(b_)
    os.unlink(a_); os.unlink(b_)
    return y2, tempo, 0


# ---------------------------------------------------------------------------
# Pacing by pauses
# ---------------------------------------------------------------------------


def _quiet_runs(y, sr, min_s=0.09, win_s=0.01):
    """Internal silent runs, as (start, end) sample indices.

    Head and tail are excluded: the caller has already trimmed them, and a run
    touching either end is lead-in, not a pause between phrases.

    A FRACTION OF PEAK IS NOT A SILENCE DETECTOR. The quiet parts of a phrase
    sit 25-40 dB under its loudest moment, so "below 5.5% of peak" catches the
    soft opening of a sentence as readily as an actual pause — and a pause this
    function reports is a pause the caller is free to SHORTEN. That is how
    "Today we are going to be talking about MCP connections" came back as
    "going to be talking about MCP connections": the opening three words were
    classified as silence and cut. The same mistake, in the same file, that the
    head-trim comment in synth_one.py already records.

    Measure against the FLOOR instead. The quietest tenth of the line is its
    noise floor, and a real pause sits within a few dB of it; speech, however
    soft, sits well above. The peak only contributes a ceiling so that a line
    with no silence at all cannot have its floor estimated up into the words.
    """
    import numpy as np
    w = max(1, int(sr * win_s))
    n = len(y) // w
    if n < 3:
        return []
    env = np.sqrt((y[:n * w].reshape(n, w).astype(np.float64) ** 2).mean(axis=1))
    peak = env.max() or 1.0
    floor = float(np.percentile(env, 10)) or 1e-6
    thr = min(floor * 3.0, peak * 0.02)          # ~+9.5 dB over the floor
    quiet = env < thr
    runs, i = [], 0
    while i < n:
        if not quiet[i]:
            i += 1
            continue
        j = i
        while j < n and quiet[j]:
            j += 1
        if i > 0 and j < n and (j - i) * win_s >= min_s:   # internal only
            runs.append((i * w, j * w))
        i = j
    return runs


def _filler(pool, n, sr, seed=0):
    """`n` samples of the line's own quiet material, with no loop period.

    Consecutive chunks come from DIFFERENT random offsets and every other one
    is reversed, joined by constant-power crossfades. The naive version of this
    — resizing one chunk to length — tiles it, and a tiled 45ms fragment is
    heard as a ~22 Hz flutter: "voice,,,,,, pack,,,,,". Same trap as the room
    tone bed in narrate/cut.py, same answer.
    """
    import numpy as np
    if n <= 0:
        return np.zeros(0, dtype=np.float32)
    if len(pool) < int(sr * 0.03):
        return np.zeros(n, dtype=np.float32)
    rng = np.random.default_rng(seed)
    xf = max(8, int(sr * 0.012))
    chunk = max(xf * 3, int(sr * 0.09))
    out = np.zeros(0, dtype=np.float32)
    flip = False
    while len(out) < n + xf:
        hi = max(1, len(pool) - chunk)
        o = int(rng.integers(0, hi))
        c = np.array(pool[o:o + chunk], dtype=np.float32)
        if len(c) < xf * 2:
            c = np.pad(c, (0, xf * 2 - len(c)))
        if flip:
            c = c[::-1]
        flip = not flip
        if len(out) == 0:
            out = c
            continue
        f = np.sqrt(np.linspace(0, 1, xf, dtype=np.float32))
        out = np.concatenate([out[:-xf], out[-xf:] * f[::-1] + c[:xf] * f, c[xf:]])
    return out[:n]


def fit_by_pauses(y, sr, target_s, min_pause=0.055, max_pause=0.85, seed=0):
    """Hit a duration by editing the SILENCES and leaving the words alone.

    `atempo` was doing this job, and it stretches everything at once: run a
    line 12% faster to match the operator's read and its commas and full stops
    close up by 12% too. Heard exactly as reported — "it doesn't space the
    punctuation well anymore" — and, because the correction differs line to
    line, as some lines rushed and others sluggish inside one render.

    A person does not pace a sentence by talking faster. They hold the beat
    after a clause a little longer, or cut it short. So do that: articulation
    is left bit-for-bit alone, and only the gaps between phrases move.

    Returns (audio, seconds_moved). A line with no internal pause — most short
    ones — has nowhere to put the time and comes back untouched, which is the
    honest answer rather than a drawl.
    """
    import numpy as np
    dur = len(y) / sr
    if dur <= 0.2 or target_s <= 0.2:
        return y, 0.0
    delta = target_s - dur
    if abs(delta) < 0.035:
        return y, 0.0
    runs = _quiet_runs(y, sr)
    if not runs:
        return y, 0.0

    lens = np.array([(b - a) / sr for a, b in runs], dtype=np.float64)
    if delta < 0:
        room = np.maximum(0.0, lens - min_pause)          # what may be removed
    else:
        room = np.maximum(0.0, max_pause - lens)          # what may be added
    if room.sum() <= 1e-6:
        return y, 0.0
    share = room / room.sum() * min(abs(delta), room.sum())
    want = lens - share if delta < 0 else lens + share

    pool = np.concatenate([y[a:b] for a, b in runs]) if delta > 0 else None
    out, prev, moved = [], 0, 0.0
    for k, (a, b) in enumerate(runs):
        out.append(y[prev:a])
        old = b - a
        # Never remove more than two thirds of a run, whatever the target asks
        # for. Detection can be wrong; deleting a phrase because of it cannot
        # be allowed to be the consequence.
        n_new = max(int(sr * min_pause), int(old * 0.34), int(round(want[k] * sr)))
        if n_new < old:
            # Cut from the MIDDLE and crossfade, so both phrase edges keep the
            # decay and onset they were generated with.
            xf = min(max(8, int(sr * 0.010)), n_new // 2)
            half = n_new // 2
            left, right = y[a:a + half], y[b - (n_new - half):b]
            f = np.sqrt(np.linspace(0, 1, xf, dtype=np.float32))
            seg = np.concatenate([left[:-xf], left[-xf:] * f[::-1] + right[:xf] * f, right[xf:]])
        elif n_new > old:
            seg = np.concatenate([y[a:b], _filler(pool, n_new - old, sr, seed + k)])
        else:
            seg = y[a:b]
        moved += (len(seg) - old) / sr
        out.append(seg)
        prev = b
    out.append(y[prev:])
    return np.concatenate(out).astype(np.float32), moved
