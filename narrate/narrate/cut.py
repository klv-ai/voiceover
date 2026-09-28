"""Tier 1 — ripple-delete dead air from audio AND video.

The one thing that must not go wrong here is drift: if the audio is trimmed on
a different timeline from the video, your voice slides off the cursor. Both
streams therefore run off the same keep-list, and the result is verified
against the arithmetic before it is handed back.

LANDMINE — ffmpeg's `aselect` is a no-op in 8.x. Even `aselect=0` passes the
full stream through, so a select/aselect filtergraph silently produces correct
video against uncut audio: perfect lip-sync drift, no error message. Video
`select` is fine, so the picture goes through the filtergraph and the audio is
cut sample-exactly here in Python instead. That also buys us a short crossfade
at every join, which a hard `select` cut cannot do — it clicks.

Joins are crossfaded through the discarded audio, so the transition is
continuous without changing the output length. See `cut_audio`.
"""
from __future__ import annotations

import array
import math
import os
import wave

from .util import die, info, ok, probe, run, scratch, step, warn

XFADE_MS = 24   # ms of handle taken from the discarded audio on each side


# ------------------------------------------------------------- audio path

def _read_wav(path: str):
    with wave.open(path, "rb") as w:
        if w.getsampwidth() != 2:
            die(f"expected 16-bit wav: {path}")
        a = array.array("h")
        a.frombytes(w.readframes(w.getnframes()))
        return a, w.getframerate(), w.getnchannels()


def _write_wav(path: str, samples: array.array, rate: int, channels: int) -> None:
    with wave.open(path, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(samples.tobytes())


def _frame_db(samples, ch: int, frame: int, half: int, stride: int) -> float:
    lo, hi = (frame - half) * ch, (frame + half) * ch
    e, n = 0, 0
    for k in range(max(0, lo), min(len(samples), hi), stride):
        v = samples[k]
        e += v * v
        n += 1
    if not n:
        return -120.0
    return 10 * math.log10(max(e / n, 1e-9) / (32768.0 ** 2))


def refine_boundaries(samples, rate: int, ch: int, keep, window: float = 0.06,
                      move_penalty: float = 0.02, seam_floor: float = -34.0,
                      max_mismatch: float = 9.0, report=None):
    """Place each junction's two boundaries TOGETHER, and abandon the ones that
    cannot be hidden.

    Two findings drove this. First, choosing each boundary independently at its
    own local energy minimum produced joins where the outgoing side ended in
    near-silence and the incoming side began on a loud onset — a 30 dB step
    that reads as the audio jumping. What matters at a splice is not that each
    side is quiet but that the two MATCH.

    Second, and larger: Whisper's word boundaries sit inside continuous speech,
    so most junctions had no silence to cut on at all. Measured on
    opportunities.mov, 70 of 79 landed in voiced audio at a median of −22 dBFS
    — mid-vowel. No crossfade rescues that; you hear the vowel change colour.

    So a junction now has to earn its cut. If the best pair of boundaries
    available is still in voiced speech, or the two sides cannot be matched,
    the junction is abandoned and the disfluency stays in. A stumble left in is
    a smaller cost than a seam the listener notices.
    """
    nframes = len(samples) // ch
    span = int(window * rate)
    hop = max(1, rate // 500)
    half = max(1, int(0.004 * rate))
    stride = max(1, ch * 4)

    def candidates(t, lo_limit, hi_limit):
        centre = int(t * rate)
        lo = max(half, centre - span, lo_limit)
        hi = min(nframes - half - 1, centre + span, hi_limit)
        if hi <= lo:
            f = min(max(centre, half), nframes - half - 1)
            return [(f, _frame_db(samples, ch, f, half, stride), 0.0)]
        return [(f, _frame_db(samples, ch, f, half, stride), abs(f - centre) / rate * 1000.0)
                for f in range(lo, hi, hop)]

    out = [list(r) for r in keep]
    merged = []
    for i in range(len(out) - 1):
        # never let a boundary eat more than 40% of its own segment
        left_room = int((out[i][1] - out[i][0]) * 0.4 * rate)
        right_room = int((out[i + 1][1] - out[i + 1][0]) * 0.4 * rate)
        outs = candidates(out[i][1], int(out[i][1] * rate) - left_room, nframes)
        ins = candidates(out[i + 1][0], 0, int(out[i + 1][0] * rate) + right_room)

        best, best_cost, best_lvl, best_mis = None, None, None, None
        for fo, lo_db, do in outs:
            for fi, li_db, di in ins:
                if fi <= fo:
                    continue
                mismatch = abs(lo_db - li_db)
                loudest = max(lo_db, li_db)
                cost = mismatch + 0.5 * (lo_db + li_db) / 2.0 + move_penalty * (do + di)
                if best_cost is None or cost < best_cost:
                    best, best_cost = (fo, fi), cost
                    best_lvl, best_mis = loudest, mismatch
        if not best:
            merged.append(i)
            continue
        if best_lvl > seam_floor or best_mis > max_mismatch:
            merged.append(i)          # cannot hide this cut — leave it uncut
            continue
        out[i][1] = best[0] / rate
        out[i + 1][0] = best[1] / rate

    # Abandoning a junction means the two ranges rejoin and whatever sat
    # between them comes back.
    joined, skip = [], set(merged)
    i = 0
    while i < len(out):
        a, b = out[i]
        while i in skip and i + 1 < len(out):
            b = out[i + 1][1]
            i += 1
        joined.append([a, b])
        i += 1

    for i in range(1, len(joined)):
        if joined[i][0] < joined[i - 1][1]:
            joined[i][0] = joined[i - 1][1]
    result = [(a, b) for a, b in joined if b - a > 0.02]
    if report is not None:
        report["abandoned"] = len(merged)
        report["kept"] = len(result) - 1
    return result


def _rms(seq) -> float:
    n = len(seq)
    if not n:
        return 0.0
    step = max(1, n // 40_000)
    tot = cnt = 0
    for i in range(0, n, step):
        v = seq[i]
        tot += v * v
        cnt += 1
    return math.sqrt(tot / cnt) if cnt else 0.0


def find_room_tone(samples, rate: int, ch: int, win: float = 0.40,
                   pct: float = 0.10):
    """A representative stretch of the room, with nobody talking.

    Two traps, and this function fell into both.

    Taking the quietest window finds DIGITAL SILENCE: every one of these
    thirteen recordings contains a passage of exact zeros, so the search
    returned nothing at all and the "room tone" bed was a hole. That is what
    "the floor drops" was.

    Taking a plain percentile instead lands on windows that still contain
    speech. So: discard the silent candidates, then take a low percentile of
    what remains — the quietest REAL room, not the quietest sample.
    """
    n = int(win * rate)
    frames = len(samples) // ch
    if frames < n * 2:
        return array.array("h")
    hop = max(1, rate // 3)
    overall = _rms(samples)
    floor = max(overall * 0.0008, 2.0)          # anything under this is silence
    cands = []
    for start in range(0, frames - n, hop):
        r = _rms(samples[start * ch:(start + n) * ch])
        if r > floor:
            cands.append((r, start))
    if not cands:
        return array.array("h")
    cands.sort()
    _, start = cands[min(len(cands) - 1, int(len(cands) * pct))]
    return samples[start * ch:(start + n) * ch]


def _tile(tone, ch: int, n_frames: int, offset: int = 0):
    """n_frames of room tone, starting at a rotating offset so it does not loop
    audibly when several spans are filled from the same donor."""
    if not len(tone):
        return array.array("h", [0] * (n_frames * ch))
    tf = len(tone) // ch
    out = array.array("h")
    k = offset % max(1, tf)
    while len(out) // ch < n_frames:
        take = min(tf - k, n_frames - len(out) // ch)
        out.extend(tone[k * ch:(k + take) * ch])
        k = 0
    return out[:n_frames * ch]


def _steady(win, ch: int, rate: int, swing_max: float, crest_max: float):
    """Is this window stationary ambience, or an event?

    Room tone does not change over 0.4s. Speech, breaths, lip smacks and word
    tails all do. Measured on mcp.mov: genuine room tone swings 1.3-2.2x across
    20ms sub-windows, while every window holding a word or a breath swings
    4-37x.

    LEVEL ALONE CANNOT SEPARATE THEM, and that is the whole trap - a breath
    sits in the same -45..-28 dBFS band as a quiet window, so selecting "the
    quietest 10%" gave a bank of 24 donors of which 22 were not room tone at
    all (16 overlapped transcribed WORDS). Cycled through a multi-second fill,
    that is heard as breathing on a loop.
    """
    sub = int(0.020 * rate)
    frames = len(win) // ch
    n = frames // sub if sub else 0
    if n < 4:
        return False
    lo = hi = None
    tot = 0.0
    for k in range(n):
        r = _rms(win[k * sub * ch:(k + 1) * sub * ch])
        tot += r * r
        lo = r if lo is None or r < lo else lo
        hi = r if hi is None or r > hi else hi
    if not lo or lo <= 0:
        return False
    rms = math.sqrt(tot / n)
    if rms <= 0:
        return False
    peak = max(abs(v) for v in win)
    return (hi / lo) <= swing_max and (peak / rms) <= crest_max


def soften(buf, ch: int, rate: int, cut_db: float = 9.0, corner: float = 2200.0,
           rumble_db: float = 8.0, rumble_corner: float = 180.0):
    """Take the HISS and the RUMBLE out of a room-tone bed, keep the room.

    The bed is real ambience, so it carries whatever the recording carries, at
    both ends of the spectrum.

    At the top it is broadband noise — measured -28.7 dB at 4-8 kHz on one
    recording. Injected into every gap it becomes a CONSTANT stationary hiss,
    and the ear locks onto stationary noise in a way it never does onto the
    varying original.

    At the bottom it is low-frequency rumble, and that varies far more between
    recordings than the hiss does: measured across two videos of the same
    speaker in the same room, the gaps of one carried 11 dB more 80-300 Hz
    energy than the other. Broadband, not a single tone, which is exactly what
    is heard as a hum — and the original version of this function passed it
    through untouched, on the grounds that low energy is what makes a bed read
    as a room rather than as digital silence.

    That grounds is right, which is why this attenuates rather than removes:
    eight decibels takes the rumble out of the foreground and leaves the room
    audibly present. The band between the two corners — where the room actually
    lives — is untouched.
    """
    frames = len(buf) // ch
    if frames < 4 or (cut_db <= 0 and rumble_db <= 0):
        return buf
    a_hi = 1.0 - math.exp(-2.0 * math.pi * corner / rate)
    a_lo = 1.0 - math.exp(-2.0 * math.pi * rumble_corner / rate)
    g_hi = 10.0 ** (-abs(cut_db) / 20.0)
    g_lo = 10.0 ** (-abs(rumble_db) / 20.0)
    out = array.array("h", buf)
    for c in range(ch):
        lo = float(buf[c])
        hi = float(buf[c])
        for f in range(frames):
            i = f * ch + c
            x = float(buf[i])
            lo += a_lo * (x - lo)              # the rumble band
            y = lo * g_lo + (x - lo)           # ...turned down, rest intact
            hi += a_hi * (y - hi)              # the room band
            out[i] = max(-32768, min(32767, int(hi + (y - hi) * g_hi)))
    return out


def _scaled(buf, g: float):
    """A copy of `buf` at gain `g`, clipped."""
    if abs(g - 1.0) < 0.02:
        return buf
    out = array.array("h", buf)
    for i in range(len(out)):
        out[i] = max(-32768, min(32767, int(out[i] * g)))
    return out


def room_tone_bank(samples, rate: int, ch: int, win: float = 0.40,
                   pct: float = 0.10, count: int = 24):
    """SEVERAL distinct stretches of the room, not one.

    Tiling a SINGLE donor is the trap. A bed built by looping one 0.40s window
    across a multi-second span is periodic, and periodic repetition is heard as
    texture — a chirp every 0.40s — even though every join is between similar
    values, so no click or step metric will ever flag it. Measured directly:
    autocorrelation r=0.92 at exactly the donor length across a 5.2s fill.

    A short span shorter than the donor never loops, which is why ducking an
    "um" got away with it and filling a whole sentence did not.
    """
    n = int(win * rate)
    frames = len(samples) // ch
    if frames < n * 2:
        return []
    hop = max(1, rate // 3)
    overall = _rms(samples)
    floor = max(overall * 0.0008, 2.0)      # anything under this is silence
    cands = []
    for start in range(0, frames - n, hop):
        r = _rms(samples[start * ch:(start + n) * ch])
        if r > floor:
            cands.append((r, start))
    if not cands:
        return []
    cands.sort()
    # If even the QUIETEST candidate sits near the overall level, this
    # recording has no quiet passage and anything banked would be signal.
    # (0.6 is tuned: an earlier 12 dB version discarded every real donor,
    # because overall is dragged down by silence.)
    if overall > 0 and cands[0][0] > overall * 0.6:
        return []

    # Walk from the quietest upward and keep only windows that are STEADY.
    # Ordering by level but filtering by SHAPE is the point: the quietest
    # windows are not automatically ambience, and the version that trusted
    # level alone banked breaths and word tails.
    def gather(swing_max, crest_max):
        bank, taken, ref = [], [], None
        for r, start in cands:
            # Stay at the ROOM's level. Walking the candidate list up to a
            # fixed count without this kept accepting progressively louder
            # windows once the genuinely quiet ones ran out, and the
            # median-levelling below then normalised the whole bed to them:
            # a -26 dB bed on a recording whose room sits at -60 dB.
            if ref is not None and r > ref * 2.0:
                break
            if any(abs(start - t) < n for t in taken):
                continue                    # overlapping windows are near-copies
            w = samples[start * ch:(start + n) * ch]
            if not _steady(w, ch, rate, swing_max, crest_max):
                continue
            if ref is None:
                ref = r
            taken.append(start)
            bank.append(w)
            if len(bank) >= count:
                break
        return bank

    # Relax rather than come back empty: no bed at all means the span keeps the
    # original speech underneath it, which is a worse fault than a slightly
    # lively bed. Only a recording with no quiet passage reaches the last pass.
    bank = gather(2.5, 6.0)
    if len(bank) < 4:
        bank = gather(4.0, 8.0) or bank
    if len(bank) < 2:
        bank = gather(1e9, 1e9) or bank

    # Level what survives to its own median so the bed does not surge between
    # chunks. Applied AFTER the shape filter: doing it on the raw quiet band
    # pulled every genuine -60 dB donor up to the -39 dB of the contaminated
    # ones, which is how the breaths got louder as well as looped.
    if len(bank) > 2:
        lv = sorted(_rms(d) for d in bank)
        med = lv[len(lv) // 2]
        if med > 0:
            near = [d for d in bank if 0.5 * med <= _rms(d) <= 2.0 * med]
            bank = [_scaled(d, med / (_rms(d) or med)) for d in (near or bank)]
    return [soften(d, ch, rate) for d in bank]


def _rev(buf, ch: int):
    """`buf` backwards: identical spectrum and level, decorrelated from the
    forward version. Doubles a small bank's variety for nothing, which matters
    because a recording may only yield a handful of genuinely quiet windows."""
    frames = len(buf) // ch
    out = array.array("h", buf)
    for f in range(frames):
        g = frames - 1 - f
        for c in range(ch):
            out[f * ch + c] = buf[g * ch + c]
    return out


def _bed(bank, ch: int, rate: int, n_frames: int, seed: int = 0):
    """`n_frames` of room tone that never repeats.

    Consecutive chunks are drawn from DIFFERENT donors and crossfaded, so the
    bed has no loop period at all. The crossfade is constant-power (sqrt), not
    linear: these chunks are uncorrelated noise, and a linear fade dips about
    3 dB through every join, which would swap one periodic artefact for
    another. Deterministic for a given seed so a rebuild sounds identical.
    """
    if n_frames <= 0:
        return array.array("h")
    if not bank:
        return array.array("h", [0] * (n_frames * ch))
    xf = max(1, int(0.030 * rate))
    out = array.array("h", [0] * (n_frames * ch))
    state = (seed * 1103515245 + 12345) & 0x7FFFFFFF
    order: list[int] = []
    oi = 0

    def pick():
        """Walk a shuffled cycle, not independent draws.

        Drawing at random let the same donor come back two chunks later purely
        by chance — measured r=0.25 at 0.74s, exactly two chunk-steps. A fresh
        shuffle per pass means no donor repeats until every other one has been
        used, so the nearest possible repeat is a whole bank apart.
        """
        nonlocal order, oi, state
        if oi >= len(order):
            idx = list(range(len(bank)))
            for i in range(len(idx) - 1, 0, -1):
                state = (state * 1103515245 + 12345) & 0x7FFFFFFF
                j = (state >> 8) % (i + 1)
                idx[i], idx[j] = idx[j], idx[i]
            if order and len(idx) > 1 and idx[0] == order[-1]:
                idx[0], idx[-1] = idx[-1], idx[0]
            order, oi = idx, 0
        k = order[oi]
        oi += 1
        return k

    # Take a chunk SHORTER than the donor, from a random offset inside it.
    # A recording may only contain three or four genuinely clean stretches of
    # room; reusing each one whole leaves a cycle you can measure (r=0.14 at
    # 2.22s with four donors). Sliding the read position turns a handful of
    # donors into an effectively unlimited supply of distinct chunks.
    chunk = max(xf * 2 + 1, int(0.25 * rate))
    pos = 0
    while pos < n_frames:
        d = bank[pick()]
        state = (state * 1103515245 + 12345) & 0x7FFFFFFF
        if (state >> 16) & 1:
            d = _rev(d, ch)
        dl = len(d) // ch
        take = min(chunk, dl)
        if take <= xf:
            break
        state = (state * 1103515245 + 12345) & 0x7FFFFFFF
        off = 0 if dl <= take else (state >> 8) % (dl - take + 1)
        for f in range(take):
            gi = pos + f
            if gi >= n_frames:
                break
            if f < xf and pos > 0:
                t = f / xf
                wn, wo = math.sqrt(t), math.sqrt(1.0 - t)
            else:
                wn, wo = 1.0, 0.0
            for c in range(ch):
                idx = gi * ch + c
                out[idx] = max(-32768, min(32767,
                               int(out[idx] * wo + d[(off + f) * ch + c] * wn)))
        step_f = take - xf
        if step_f <= 0:
            break
        pos += step_f
    return out


def speech_level(samples, rate: int, ch: int, i0: int, i1: int):
    """How loud the SPEAKING is over [i0, i1), ignoring the gaps.

    A plain RMS over a span is dominated by how much silence it happens to
    contain, so two takes of the same words at the same loudness measure
    differently purely because one pauses more. Take a high percentile of the
    short-window levels instead: that lands on the voice itself.
    """
    win = int(0.06 * rate)
    frames = len(samples) // ch
    i0, i1 = max(0, i0), min(frames, i1)
    if win <= 0 or i1 - i0 < win:
        return 0.0
    lv = []
    for f in range(i0, i1 - win, win):
        lv.append(_rms(samples[f * ch:(f + win) * ch]))
    if not lv:
        return 0.0
    lv.sort()
    return lv[min(len(lv) - 1, int(len(lv) * 0.75))]


def local_floor(samples, rate: int, ch: int, i0: int, i1: int, look: float = 0.5):
    """The level the room holds AROUND a span, not the quietest instant in it.

    Using the minimum filled 10.8 dB under the surrounding ambience on Ask.mov,
    which is what makes a filled span sound like the floor dropping away rather
    than like a pause. Ambience is separated from speech before the percentile:
    if the half-second either side happens to be solid speech, every window is
    loud and a percentile of them would fill the span at speaking level.
    """
    nframes = len(samples) // ch
    win = int(0.06 * rate)
    if win <= 0:
        return 0.0
    local = []
    for lo, hi in ((max(0, i0 - int(look * rate)), i0),
                   (i1, min(nframes, i1 + int(look * rate)))):
        for f in range(lo, max(lo, hi - win), win):
            local.append(_rms(samples[f * ch:(f + win) * ch]))
    if not local:
        return 0.0
    local.sort()
    loud = local[-1]
    quiet = [v for v in local if v <= loud * 0.5] or local[:1]
    return quiet[int(len(quiet) * 0.30)]


def tame_floor(buf, rate: int, ch: int, target: float,
               win: float = 0.06, smooth: float = 0.15):
    """Pull a take's own noise floor down to the room's, leaving speech alone.

    A generated take carries its own hiss, and it is far louder than this
    recording's room: measured -25 to -50 dBFS inside the takes against a -58
    dBFS room, a step of 7 to 32 dB. Laid onto a room-tone bed the background
    JUMPS the instant a line starts, which is heard as a spike of noise at the
    top of every line.

    This is a downward expander, not a denoiser. Frames sitting at the take's
    own floor are attenuated toward `target`; frames carrying speech are left
    at unity; everything between is interpolated in the log domain. The gain
    curve is then smoothed over ~150ms so it cannot pump, and applied with
    linear interpolation between window centres so there are no steps in it.
    """
    frames = len(buf) // ch
    w = max(1, int(win * rate))
    n = frames // w
    if n < 4 or target <= 0:
        return buf
    lv = [_rms(buf[k * w * ch:(k + 1) * w * ch]) for k in range(n)]
    ranked = sorted(v for v in lv if v > 0)
    if not ranked:
        return buf
    floor = ranked[max(0, int(len(ranked) * 0.05))]
    speech = ranked[min(len(ranked) - 1, int(len(ranked) * 0.85))]
    if floor <= target or speech <= floor:
        return buf                      # already at or below the room
    # ONLY WHEN THERE IS A REAL FLOOR TO FIND.
    #
    # This reads the quietest few percent of the take as its noise floor, which
    # is true of a take with pauses in it and false of one that is speech from
    # end to end: there the quietest windows ARE speech, just the soft parts.
    # Measured on a 3.48s line with no internal silence, the "floor" came out at
    # -36 dBFS, the untouched threshold at -24, and the closing words of the
    # sentence — sitting at -25 to -38 — were pulled down 20-odd dB. The line
    # was heard as truncated, and effectively it was.
    #
    # A genuine noise floor sits far below the speech. If it does not, there is
    # no hiss here to tame and nothing to do.
    if speech < floor * 8.0:            # less than ~18 dB of range
        return buf
    lo_g = target / floor               # what the quietest frames need
    # WHERE "SPEECH" STARTS IS MEASURED FROM THE FLOOR, NOT FROM THE PEAK.
    #
    # Anchoring unity at half the 85th percentile means a frame is only safe if
    # it is within 6 dB of the loudest thing in the take. Speech does not work
    # like that: a line spans 30 to 40 dB from its stressed vowels to the
    # release of its last word, so the whole quiet end of a sentence fell into
    # the ramp and was pulled toward the room. Measured on one line: the final
    # 0.74s, sitting at -17 to -42 dBFS in the take, arrived 21 to 30 dB down.
    # Heard as the line being truncated — which it effectively was.
    #
    # Hiss is what sits AT the floor. Twelve decibels above it is already
    # content, and content is not this function's business.
    # Nine decibels above the floor is already content, not hiss.
    safe = min(floor * 2.8, speech * 0.5)
    gains = []
    for v in lv:
        if v >= safe:
            gains.append(1.0)
        elif v <= floor * 1.5:
            gains.append(lo_g)
        else:
            # log-domain ramp between the two anchors
            t = math.log(v / (floor * 1.5)) / math.log(safe / (floor * 1.5))
            gains.append(lo_g * ((1.0 / lo_g) ** t))
    r = max(1, int(smooth / win))
    sm = [sum(gains[max(0, i - r):min(len(gains), i + r + 1)])
          / len(gains[max(0, i - r):min(len(gains), i + r + 1)]) for i in range(len(gains))]

    out = array.array("h", buf)
    for f in range(frames):
        k = f / w - 0.5
        i0 = max(0, min(n - 1, int(math.floor(k))))
        i1 = max(0, min(n - 1, i0 + 1))
        t = max(0.0, min(1.0, k - i0))
        g = sm[i0] * (1 - t) + sm[i1] * t
        for c in range(ch):
            j = f * ch + c
            out[j] = max(-32768, min(32767, int(out[j] * g)))
    return out


def _fill_gain(bed_r: float, target: float, span_s: float):
    """How hard to drive the room-tone bed.

    The bed is already REAL room tone lifted from this recording, so its own
    level is the truth and a local measurement is only a correction to it.

    HOW FAR TO TRUST THAT CORRECTION DEPENDS ON THE SPAN. Around a 0.3s filler
    the half-second either side genuinely is the same acoustic moment, and
    matching it is what stopped ducked ums sounding like holes. Across a
    multi-second sentence those same windows are mostly speech, and following
    them put one fill 8 dB above the room and another 8 dB below it - heard as
    a rush of noise under one line and a hole under the next. So the tolerance
    narrows as the span grows: local for short, the room's own level for long.
    """
    if bed_r <= 0:
        return 0.0
    if target <= 0:
        return 1.0
    t = min(1.0, max(0.0, (span_s - 0.5) / 1.5))      # 0 at 0.5s, 1 at 2s+
    hi = 10.0 ** ((6.0 - 4.5 * t) / 20.0)             # +/-6 dB -> +/-1.5 dB
    return min(max(target / bed_r, 1.0 / hi), hi)


def fill_with_room(samples, rate: int, ch: int, i0: int, i1: int, bank):
    """Overwrite [i0, i1) with room tone matched to the local floor."""
    if i1 <= i0:
        return
    if not bank:
        # No usable room tone in this recording, so the room here IS silence.
        # Zeroing is the honest answer; returning early would leave the
        # original words in place underneath the take.
        for k in range(i0 * ch, i1 * ch):
            samples[k] = 0
        return
    target = local_floor(samples, rate, ch, i0, i1)
    bed = _bed(bank, ch, rate, i1 - i0, seed=i0)
    bed_r = _rms(bed) or 1.0
    g = _fill_gain(bed_r, target, (i1 - i0) / float(rate))
    for f in range(i0, i1):
        k = f - i0
        for c in range(ch):
            samples[f * ch + c] = max(-32768, min(32767, int(bed[k * ch + c] * g)))


def duck_spans(samples, rate: int, ch: int, spans, keep, *,
               ramp_ms: float = 45.0, min_dur: float = 0.06,
               min_level: float = -45.0, room_tone: bool = True):
    """Replace a filler with the room it was spoken in, rather than silence.

    Attenuating the span to a gain floor takes the room tone down with the
    voice, so an "um" becomes a hole: the noise floor drops out and comes back,
    which is audible precisely because the rest of the track has a floor. The
    span is now crossfaded to a bed of room tone lifted from the quietest
    stretch of the same recording and matched to the LOCAL floor either side,
    so the level of the background never moves.

    Only spans still inside a keep-range are touched — anything already cut out
    is gone.
    """
    nframes = len(samples) // ch
    ramp = int(ramp_ms * rate / 1000)
    bank = room_tone_bank(samples, rate, ch) if room_tone else []
    done = 0

    for a, b in spans:
        mid = (a + b) / 2.0
        if not any(x <= mid <= y for x, y in keep):
            continue
        i0, i1 = int(a * rate), int(b * rate)
        if i1 - i0 < 4 or i0 < 0 or i1 > nframes or b - a < min_dur:
            continue
        half = max(1, int(0.008 * rate))
        n = max(2, int((b - a) / 0.02))
        peak = max(_frame_db(samples, ch, int((a + (b - a) * k / n) * rate), half,
                             max(1, ch * 4)) for k in range(n + 1))
        if peak < min_level:
            continue

        target = local_floor(samples, rate, ch, i0, i1)

        bed = _bed(bank, ch, rate, i1 - i0, seed=i0)
        bed_r = _rms(bed) or 1.0
        g_bed = _fill_gain(bed_r, target, b - a)

        r = min(ramp, (i1 - i0) // 2)
        for f in range(i0, i1):
            k = f - i0
            if k < r:
                w = k / r
            elif (i1 - f) < r:
                w = (i1 - f) / r
            else:
                w = 1.0
            for c in range(ch):
                idx = f * ch + c
                src = samples[idx] * (1.0 - w)
                fill = bed[k * ch + c] * g_bed * w
                samples[idx] = max(-32768, min(32767, int(src + fill)))
        done += 1
    return done


def cut_audio(samples, rate: int, ch: int, keep, out_wav: str, *,
              tempo: float = 1.0, xfade_ms: int = XFADE_MS) -> str:
    """Concatenate the keep-ranges, crossfading each join through the material
    we are throwing away.

    Fading each edge down to silence and back up is what made cuts audible: at
    a join where one side is quiet and the other loud, the listener hears a
    hole followed by a step. A crossfade has no hole — but an overlapping one
    shortens the audio by a fade per join, and that shortfall accumulates as
    the sound creeping ahead of the picture.

    The way out is that a cut always has discarded audio either side of it. We
    borrow `xfade_ms` of it as crossfade handles: the outgoing side is taken
    from just PAST the cut point, the incoming side from just BEFORE the resume
    point. The join becomes continuous, and because the handles come from
    material that was leaving anyway, the output length is unchanged.
    """
    nframes = len(samples) // ch
    L = int(xfade_ms * rate / 1000)

    bounds, out = [], array.array("h")
    for a, b in keep:
        i0 = max(0, min(nframes, int(a * rate)))
        i1 = max(i0, min(nframes, int(b * rate)))
        if i1 <= i0:
            continue
        bounds.append((len(out) // ch, i0, i1))
        out.extend(samples[i0 * ch:i1 * ch])

    # crossfade each join in place
    for k in range(len(bounds) - 1):
        _, _, e_src = bounds[k]                 # source frame where the cut is made
        join, s_src, _ = bounds[k + 1]          # output frame, source frame where it resumes
        seg_left = join - bounds[k][0]
        seg_right = (bounds[k + 2][0] if k + 2 < len(bounds) else len(out) // ch) - join
        n = min(L, seg_left, seg_right,
                nframes - e_src, s_src,         # handles must exist in the source
                max(0, (s_src - e_src) // 2))   # and must come from discarded audio
        if n < 8:
            continue
        for f in range(2 * n):
            t = (f + 0.5) / (2 * n)
            g_out, g_in = math.cos(t * math.pi / 2), math.sin(t * math.pi / 2)
            src_o = (e_src - n + f) * ch
            src_i = (s_src - n + f) * ch
            dst = (join - n + f) * ch
            for c in range(ch):
                v = samples[src_o + c] * g_out + samples[src_i + c] * g_in
                out[dst + c] = max(-32768, min(32767, int(v)))

    # short fades at the very ends so the file itself does not click
    edge = min(int(0.010 * rate), len(out) // ch // 2)
    for f in range(edge):
        g = f / edge
        for c in range(ch):
            out[f * ch + c] = int(out[f * ch + c] * g)
            j = (len(out) // ch - 1 - f) * ch + c
            out[j] = int(out[j] * g)

    tmp = scratch(".wav")
    _write_wav(out_wav if tempo == 1.0 else tmp, out, rate, ch)
    if tempo != 1.0:
        run(["ffmpeg", "-v", "error", "-i", tmp, "-af", _atempo(tempo),
             "-c:a", "pcm_s16le", out_wav, "-y"])
    os.path.exists(tmp) and os.unlink(tmp)
    return out_wav


def _atempo(tempo: float) -> str:
    """atempo is well-conditioned in 0.5–2.0; chain outside that."""
    t, chain = tempo, []
    while t > 2.0:
        chain.append(2.0)
        t /= 2.0
    while t < 0.5:
        chain.append(0.5)
        t /= 0.5
    chain.append(t)
    return ",".join(f"atempo={c:.6f}" for c in chain)


# ------------------------------------------------------------- video path

def video_filtergraph(keep, tempo: float = 1.0, fps: float = 60.0) -> str:
    """select + renumber.

    LANDMINE — `setpts=N/FRAME_RATE/TB` assumes constant frame rate, and these
    screen recordings are VFR: Ask.mov averages 26 fps against a nominal 60,
    because a static screen emits fewer frames. Renumbering N frames as if they
    were evenly spaced at 60 then compresses the timeline by whatever the real
    average was — a 60-second selection came out at 20.5 seconds, playing 3x
    fast against perfectly correct audio, with no error anywhere.

    So normalise to CFR with `fps=` BEFORE select, and divide by that same rate.
    Duplicated frames on static screen content cost almost nothing to encode.
    """
    # Guard bands on both ends of each range, and they are not symmetric.
    #
    # End: half a frame early, because `between` is inclusive and a boundary
    # sitting exactly on the grid would otherwise pull in one extra frame.
    #
    # Start: a QUARTER frame early, because the frame at exactly t = a is
    # sometimes computed a hair below a and then silently dropped. That cost
    # about a third of a frame per segment — 7 frames over 23 segments on
    # opportunities.mov — and since only the picture lost them, the sound crept
    # ahead by up to 283ms by the end of a long video. A quarter frame cannot
    # admit the previous frame, which is a whole frame away.
    head, tail = 0.25 / fps, 0.5 / fps
    sel = "+".join(f"between(t,{a - head:.4f},{b - tail:.4f})" for a, b in keep)
    g = f"[0:v]fps={fps:g},select='{sel}',setpts=N/{fps:g}/TB"
    if tempo and abs(tempo - 1.0) > 1e-6:
        g += f"/{tempo:.6f}"
    return g + "[v]"


# ------------------------------------------------------------------- main

def cut(src: str, edl: dict, out: str, *, audio: str | None = None,
        encoder: str = "libx264", crf: int = 18, preset: str = "medium",
        abitrate: str = "320k", fps: float | None = None, refine: bool = True,
        seam_floor: float = -34.0, duck: bool = True,
        dry_run: bool = False) -> str | None:
    keep = [tuple(r) for r in edl.get("keep", [])]
    if not keep:
        die("EDL has no keep-ranges — nothing to cut.")

    mi = probe(src)
    tempo = float(edl.get("tempo", 1.0) or 1.0)

    rate = fps or (mi.fps if 0 < mi.fps <= 120 else 60.0)

    a_src = audio or src
    base = scratch(".wav")
    run(["ffmpeg", "-v", "error", "-i", a_src, "-map", "0:a:0", "-vn",
         "-c:a", "pcm_s16le", "-ar", "48000", base, "-y"])
    samples, srate, ch = _read_wav(base)
    os.unlink(base)

    if refine:
        before_n = len(keep)
        rep = {}
        keep = refine_boundaries(samples, srate, ch, keep,
                                 seam_floor=seam_floor, report=rep)
        info(f"seams: {rep.get('kept', 0)} cuts placed in quiet matched audio, "
             f"{rep.get('abandoned', 0)} abandoned as unhideable "
             f"({before_n} → {len(keep)} ranges)")

    if duck:
        n = duck_spans(samples, srate, ch, edl.get("filler_spans", []), keep)
        if n:
            info(f"ducked {n} filler(s) in place — silenced without a splice")

    # Snap every boundary to the frame grid, then cut BOTH streams on the
    # snapped list. Video can only cut on whole frames; audio can cut anywhere.
    # Left unsnapped, each segment rounds the picture up by up to one frame
    # while the sound is sample-exact, and those fractions accumulate — 47
    # segments drifted 0.27s apart by the end of a 6-minute video.
    keep = [(round(a * rate) / rate, round(b * rate) / rate) for a, b in keep]
    keep = [(a, b) for a, b in keep if b - a > 1.5 / rate]

    # Measure AFTER refining and snapping — those move boundaries, and the
    # drift check below is only meaningful against the ranges actually cut.
    kept = sum(b - a for a, b in keep)
    expect = kept / tempo

    step(f"cut — {len(keep)} keep-ranges, tempo {tempo:g}x")
    info(f"source   {mi.duration:8.2f}s   nominal {mi.fps:g} fps → normalising to {rate:g} fps CFR")
    info(f"kept     {kept:8.2f}s  ({len(keep)} ranges, {edl.get('gap_fill', 0):.2f}s pause between)")
    info(f"expected {expect:8.2f}s  (saves {mi.duration - expect:.1f}s)")
    if mi.nb_video_streams > 1:
        info(f"source carries {mi.nb_video_streams} video streams (thumbnail tracks) — mapping v:0 only")

    graph = video_filtergraph(keep, tempo, rate)
    if dry_run:
        print(graph)
        return None

    if audio:
        a_info = probe(audio)
        if abs(a_info.duration - mi.duration) > 0.25:
            warn(f"replacement audio is {a_info.duration:.2f}s but source is "
                 f"{mi.duration:.2f}s — the EDL is measured on the source timeline")
        info(f"audio    from {os.path.basename(audio)}")

    cut_wav = scratch(".wav")
    cut_audio(samples, srate, ch, keep, cut_wav, tempo=tempo)
    got_a = probe(cut_wav).duration
    if abs(got_a - expect) > 0.15:
        warn(f"cut audio is {got_a:.2f}s, expected {expect:.2f}s")

    script = scratch(".filter")
    with open(script, "w") as f:
        f.write(graph)

    cmd = ["ffmpeg", "-v", "error", "-stats", "-i", src, "-i", cut_wav,
           "-filter_complex_script", script, "-map", "[v]", "-map", "1:a:0"]
    if encoder == "videotoolbox":
        cmd += ["-c:v", "h264_videotoolbox", "-b:v", "40M", "-profile:v", "high"]
    else:
        cmd += ["-c:v", encoder, "-preset", preset, "-crf", str(crf), "-profile:v", "high"]
    cmd += ["-pix_fmt", "yuv420p", "-fps_mode", "cfr", "-r", f"{rate:g}",
            "-c:a", "aac", "-b:a", abitrate, "-ar", "48000",
            "-movflags", "+faststart", out, "-y"]
    run(cmd, capture=False)
    os.unlink(script)
    os.unlink(cut_wav)

    res = probe(out)
    drift = res.duration - expect
    ok(f"{out}  ({res.duration:.2f}s)")
    if abs(drift) > max(0.15, 2.0 / rate):
        warn(f"output is {drift:+.2f}s off the arithmetic — check A/V sync before publishing")
    else:
        info(f"A/V within {abs(drift):.2f}s of plan")
    return out
