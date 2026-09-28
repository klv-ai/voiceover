"""TD-PSOLA: change pitch without changing timing, timbre or words.

Why this and not a voice model: resynthesising speech from a cloned voice
replaces the speaker. Pitch-synchronous overlap-add moves the speaker's own
glottal pulses closer together or further apart, so the formants — the thing
that makes a voice recognisably yours — are carried through untouched. The
output is the same recording with a different melody.

Duration is preserved by construction: synthesis marks are laid down across the
same span as the analysis marks, and each one borrows from whichever analysis
mark is nearest, so frames get repeated or skipped rather than stretched.
"""
from __future__ import annotations

import numpy as np


def _lowpass(x: np.ndarray, sr: int, cutoff: float = 900.0) -> np.ndarray:
    """Zero-phase low-pass, for finding glottal closures."""
    n = len(x)
    F = np.fft.rfft(x)
    f = np.fft.rfftfreq(n, 1.0 / sr)
    F = F * np.exp(-(f / cutoff) ** 4)      # soft brick wall, no ringing
    return np.fft.irfft(F, n)


def pitch_marks(x: np.ndarray, sr: int, t: np.ndarray, f0: np.ndarray,
                tol: float = 0.30):
    """Glottal pulse positions.

    Marks must land on the SAME phase of every period or overlap-add scatters
    the spectrum: measured 6.55 dB log-spectral distortion on a resynthesis
    that changed pitch by 0.03 semitones, which is what was heard as sizzle on
    vocal peaks. The earlier version snapped to the largest |x| in a window of
    the raw 48 kHz waveform, which finds whichever formant peak happens to be
    tallest and jumps between them from period to period — 42% spacing error at
    the 90th percentile against the pitch the tracker reported.

    A glottal closure is a low-frequency event, so the search runs on a 900 Hz
    low-passed copy, steps period by period from the F0 track, and only accepts
    a mark within `tol` of the predicted position.
    """
    voiced = ~np.isnan(f0)
    if voiced.sum() < 2:
        return np.array([], dtype=int)
    lp = _lowpass(x, sr)

    def period_at(sample: float) -> float | None:
        i = int(np.clip(np.searchsorted(t, sample / sr), 0, len(f0) - 1))
        if np.isnan(f0[i]):
            return None
        return sr / float(f0[i])

    marks, n = [], len(x)
    i = 0
    while i < len(t):
        if not voiced[i]:
            i += 1
            continue
        # start a run
        pos = t[i] * sr
        while pos < n - 1:
            p = period_at(pos)
            if p is None:
                break
            w = int(p * tol)
            a, b = int(max(0, pos - w)), int(min(n, pos + w + 1))
            if b - a < 3:
                break
            # glottal closure shows as the strongest excursion in the LF copy
            local = a + int(np.argmax(np.abs(lp[a:b])))
            # Phase-lock to the previous mark. Peak-picking alone jumps between
            # whichever excursion happens to be tallest in this period, so
            # consecutive marks sit at different phases; overlap-adding
            # windows centred on inconsistent phases is what scatters the
            # spectrum. Correlating against the previous period's waveform
            # keeps every mark on the same point of the cycle.
            if marks:
                prev = marks[-1]
                half = int(p * 0.5)
                if prev - half >= 0 and prev + half < n:
                    tpl = x[prev - half:prev + half]
                    tn = np.sqrt(np.dot(tpl, tpl)) or 1.0
                    best, best_r = local, -2.0
                    for cand in range(max(half, local - int(p * 0.25)),
                                      min(n - half, local + int(p * 0.25) + 1), 2):
                        seg = x[cand - half:cand + half]
                        r = float(np.dot(tpl, seg) / (tn * (np.sqrt(np.dot(seg, seg)) or 1.0)))
                        if r > best_r:
                            best, best_r = cand, r
                    local = best
            marks.append(local)
            pos = local + p
        # skip the frames this run consumed
        while i < len(t) and t[i] * sr < pos:
            i += 1
    return np.unique(np.array(marks, dtype=int))


def _runs(marks: np.ndarray, sr: int, max_gap_s: float = 0.05):
    """Split marks into contiguous voiced runs."""
    if len(marks) == 0:
        return []
    out, cur = [], [marks[0]]
    for m in marks[1:]:
        if m - cur[-1] > max_gap_s * sr:
            out.append(np.array(cur))
            cur = [m]
        else:
            cur.append(m)
    out.append(np.array(cur))
    return [r for r in out if len(r) >= 4]


def shift(x: np.ndarray, sr: int, marks: np.ndarray, ratio_at,
          xfade_ms: float = 12.0) -> np.ndarray:
    """Repitch by `ratio_at(sample_index) -> multiplier` (1.0 = unchanged).

    Only voiced runs whose ratio actually departs from unity are resynthesised;
    everything else is the original signal, untouched. Without that, the
    overlap-add would keep laying down marks across unvoiced gaps using the
    last voiced period and smear consonants and silence into a buzz.
    """
    y = x.astype(np.float32).copy()
    xf = int(xfade_ms * sr / 1000)

    for run in _runs(marks, sr):
        lo, hi = int(run[0]), int(run[-1])
        probe = np.linspace(lo, hi, max(3, min(24, (hi - lo) // 200 + 3)))
        if all(abs(ratio_at(p) - 1.0) < 1e-3 for p in probe):
            continue

        periods = np.diff(run)
        periods = np.append(periods, periods[-1])

        pad = 4 * int(periods.max()) + 8      # room for lowered-pitch windows
        acc = np.zeros(hi - lo + 2 * pad + 2, dtype=np.float64)
        wsum = np.zeros_like(acc)
        base = lo - pad

        pos = float(run[0])
        while pos < hi:
            k = int(np.searchsorted(run, pos, side="right") - 1)
            k = min(max(k, 0), len(run) - 1)
            p = int(periods[k])
            if p < 4:
                break
            r = max(float(ratio_at(pos)), 1e-3)
            # Window TWO SYNTHESIS periods, not two analysis periods, so the
            # hop is always exactly half the window and Hann overlap-add sums
            # to a constant. With a fixed 2-analysis-period window the overlap
            # drifts away from 50% as soon as the ratio leaves unity, wsum
            # ripples at the synthesis rate, and dividing by it amplitude-
            # modulates the voice — heard as sizzle on peaks, and worse for
            # downward shifts because those spread the windows further apart.
            # Measured aperiodicity added: +0.046 at -3 st, +0.023 at +3 st.
            ps = max(4, int(round(p / r)))
            a, b = int(run[k]) - ps, int(run[k]) + ps
            if a < 0 or b > len(x):
                pos += ps
                continue
            win = np.hanning(b - a)
            dst = int(pos) - ps - base
            if dst < 0 or dst + (b - a) > len(acc):
                pos += ps
                continue
            acc[dst:dst + (b - a)] += x[a:b] * win
            wsum[dst:dst + (b - a)] += win
            pos += ps

        rec = np.where(wsum > 1e-6, acc / np.maximum(wsum, 1e-6), 0.0)
        s0, s1 = lo, hi
        seg = rec[s0 - base:s1 - base]
        if len(seg) != s1 - s0:
            continue
        # crossfade the corrected run back into the original either side
        n = min(xf, (s1 - s0) // 4)
        if n > 2:
            ramp = np.linspace(0, 1, n)
            seg[:n] = seg[:n] * ramp + y[s0:s0 + n] * (1 - ramp)
            seg[-n:] = seg[-n:] * ramp[::-1] + y[s1 - n:s1] * ramp
        y[s0:s1] = seg.astype(np.float32)
    return y
