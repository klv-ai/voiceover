"""Assemble the current mix: your voice, with the rendered lines swapped in.

The studio needs one continuous audio track to play against the video, so the
operator can hear the thing as a whole rather than auditioning fragments. The
base is the ORIGINAL recording, on the original timeline — every line that has
not been rendered, or that is marked keep-real, is simply your own audio,
untouched. Rendered lines are laid over the top at their own start times.

Nothing is cut and nothing moves: this is a preview of the substitution, not of
the final retimed edit. Keeping the timeline fixed is what lets the video play
against it without re-encoding anything.

    python3 lab/assemble.py plan.json -o preview.wav
"""
from __future__ import annotations

import argparse
import array
import contextlib
import json
import os
import sys
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from narrate import cut as C          # noqa: E402
from narrate.util import run          # noqa: E402


def _retime(y, rate, ch, factor):
    """Stretch or squeeze a take by `factor` without moving its pitch."""
    import subprocess
    import tempfile
    a, b = tempfile.mktemp(suffix=".wav"), tempfile.mktemp(suffix=".wav")
    try:
        C._write_wav(a, y, rate, ch)
        tempo = 1.0 / factor          # >1 factor means LONGER, so slower tempo
        chain = []
        while tempo < 0.5:
            chain.append("atempo=0.5")
            tempo /= 0.5
        while tempo > 2.0:
            chain.append("atempo=2.0")
            tempo /= 2.0
        chain.append(f"atempo={tempo:.6f}")
        r = subprocess.run(["ffmpeg", "-v", "error", "-i", a, "-af", ",".join(chain),
                            "-c:a", "pcm_s16le", b, "-y"], capture_output=True)
        if r.returncode != 0 or not os.path.exists(b):
            return y
        out, _, _ = C._read_wav(b)
        return out
    finally:
        for f in (a, b):
            try:
                os.unlink(f)
            except OSError:
                pass


def prev_placed_end(placed, rate):
    """Frame index just after the FURTHEST thing already laid down.

    Not the last entry's end. Entries go down in script order, but a line does
    not have to end where the script says: a take nudged later runs past the
    span of the line after it, and a line left out records an entry ending at
    its own original start. Reading only the last entry therefore lets the
    watermark move BACKWARDS, and the next line then clears from a point that
    is still inside its neighbour's audio.

    Measured: line 51 played 350.07-354.22, line 52 was dropped and recorded an
    end of 349.30, so line 53 began clearing at 351.20 and filled the rest of
    51 with room tone. 4.15s of take, 1.14s of it audible — heard as 51 being
    truncated to the size of the line that had been removed.
    """
    return max((int(x["end"] * rate) for x in placed), default=0)


def _loud_bounds(base, rate, ch, frm, to, thresh, step=0.03):
    """First and last windows in [frm, to) that are above `thresh`.

    Scanning the WHOLE range is the point. Walking forward until the first
    quiet window stops at any brief pause and leaves the speech after it in
    place, which is why a fragment still survived on some lines after the
    first attempt at this.

    Needed because a transcript boundary is where a WORD is deemed to end, not
    where the sound does: on mcp s001 the segment ends at 3.54s while "Klavi"
    runs to about 3.8s, so clearing only the span left the tail of the real
    word playing right after the take had said the same word - heard as
    "Hello, this is ... from Klavi, vi".
    """
    w = max(1, int(step * rate))
    first = last = None
    f = frm
    while f + w <= to:
        if C._rms(base[f * ch:(f + w) * ch]) > thresh:
            if first is None:
                first = f
            last = f + w
        f += w
    return first, last


def lay_bed(base, rate, ch, level_db):
    """One continuous bed of SYNTHESISED room, under everything.

    Every take carries the floor the model generated in it — about -54 dB here
    — and between takes a synthetic mix is digital silence. So the room
    switches on at each block and off again after it, a hundred and thirty
    times in one video, heard as a click or a small breath at every boundary.

    A bed fixes that, and it cannot be built by tiling donors: a bank is a few
    seconds, a video is minutes, and the repetition is audible as a pulse at
    exactly the donor length (measured r=0.92 before, r=-0.004 for this).
    Noise shaped to the same spectrum has nothing to repeat.

    The level is MEASURED and scaled, not computed from the filter's gain — an
    earlier version reasoned about what the one-pole would cost and landed 4.7
    dB low.

    It sits WELL BELOW the takes' own floor, not level with it. Matching that
    floor put -54 dB of noise across the whole video, including the seven
    minutes that had been silent, and a continuous broadband -54 dB is heard as
    static. The bed only has to stop the floor appearing and vanishing at a
    block edge; a step of ten decibels is not noticed, where a step of sixty
    is. Note also that the floor it was matched to is not the model's: the
    takes leave the pipeline about 7.5 dB noisier than they were generated.
    """
    import array as _array
    import numpy as np
    n = len(base) // ch
    if n <= 0:
        return
    rng = np.random.default_rng(12345)      # the same mix twice
    w = rng.uniform(-1.0, 1.0, size=n).astype(np.float32)
    # A one-pole low-pass. The takes' floor measured within 4 dB across five
    # bands, so the bed wants to be nearly flat with a little taken off the top.
    # DARK, not flat.
    #
    # The first bed was shaped to a measurement of the takes' floor that came
    # out nearly flat across five bands, so it was nearly white — and white
    # noise at an audible level is not room tone, it is television static,
    # which is precisely what it sounded like. Real air rolls off hard above a
    # couple of kilohertz. A long, heavily damped kernel does that.
    #
    # Convolved in one call: ten minutes is forty-seven million samples, which
    # a per-sample Python loop does not finish in a useful time.
    a = 0.06
    k = np.array([a * (1.0 - a) ** i for i in range(256)], dtype=np.float32)
    k /= np.sqrt((k ** 2).sum())
    b = np.convolve(w, k, mode="same").astype(np.float32)
    rms = float(np.sqrt((b ** 2).mean())) or 1e-9
    b *= (10.0 ** (level_db / 20.0)) / rms
    cur = np.frombuffer(base, dtype=np.int16).astype(np.float32)
    if ch > 1:
        b = np.repeat(b, ch)
    out = np.clip(cur + b * 32767.0, -32768, 32767).astype(np.int16)
    base[:] = _array.array("h", out.tobytes())


def assemble(plan, out):
    src_wav = plan["source_wav"]
    if not os.path.exists(src_wav):
        run(["ffmpeg", "-v", "error", "-i", plan["source"], "-map", "0:a:0", "-vn",
             "-ac", "1", "-ar", "48000", "-c:a", "pcm_s16le", src_wav, "-y"])
    base, rate, ch = C._read_wav(src_wav)
    nframes = len(base) // ch
    fade = int(0.018 * rate)
    placed = []
    # Every stretch of the original that a take has replaced, so the slivers
    # BETWEEN them can be found afterwards.
    cleared = []

    # A FULLY SYNTHETIC track, when asked for.
    #
    # Normally the mix begins as the original recording and every take is
    # patched over its own span. That is right while some of the original is
    # being kept, and wrong once every line has been re-read: a replacement is
    # never frame-perfect, so each seam leaks the screencast underneath — its
    # room, its breaths, the tail of the word being replaced. Heard as the
    # speaker being in a tunnel, and as stray breaths belonging to nobody, on
    # takes that are clean in isolation.
    #
    # So start from silence and stay there: each take brings its own room
    # across its own span, and between them there is nothing to reproduce.
    synthetic = bool(plan.get("synthetic"))
    if synthetic:
        # NO room tone at all. `fill_with_room` zeroes a span when the bank is
        # empty, and that is exactly what this mix wants everywhere the takes
        # do not reach.
        #
        # The first attempt banked tone from the takes and laid it across the
        # whole timeline. A bank is a few seconds of donor; a video is minutes.
        # room_tone_bank's own docstring says what tiling one does — periodic
        # repetition heard as texture, r=0.92 autocorrelation at the donor
        # length — and over ten minutes that is sixty repeats of the same
        # breath of air, with no voice anywhere near it.
        #
        # Each take already carries the room it was generated in, across its
        # own span. Between them, silence is the honest answer and the quiet
        # one.
        # SILENCE, not a bed.
        #
        # Filling the whole timeline with room tone tiles a few seconds of
        # donor across minutes, and room_tone_bank says what that sounds like
        # in its own docstring: periodic repetition heard as texture, r=0.92
        # autocorrelation at exactly the donor length. Over a ten-minute video
        # it is sixty repeats of the same breath of air — reported, accurately,
        # as constant echoes repeating over and over with no voice present.
        #
        # The takes carry their own room within their own spans, which is all
        # the room this mix needs. `tone` is kept for the short seam fills,
        # where a span shorter than the donor never loops.
        tone = []
        src = base
        base = array.array("h", bytes(nframes * ch * 2))
        # KEEP MY VOICE. A line kept as the real recording is the one place a
        # synthetic mix still wants the original, and starting from silence
        # left it silent: the rest of the mix never lays the recording down,
        # it only protects what is already there. Put the recording back across
        # exactly those spans, faded in and out so neither end clicks.
        for seg in plan["segments"]:
            if not seg.get("locked") or seg.get("drop"):
                continue
            i0 = max(0, int(seg["start"] * rate))
            i1 = min(nframes, int(seg["end"] * rate))
            n = i1 - i0
            for i in range(n):
                g = min(1.0, (i + 1) / fade, (n - i) / fade)
                for c in range(ch):
                    k = (i0 + i) * ch + c
                    base[k] = int(src[k] * g)
    else:
        tone = C.room_tone_bank(base, rate, ch)

    segs = plan["segments"]
    # Make room for a last line that outlasts the picture.
    #
    # Every take is clamped to the length of the source recording, which is
    # right for a line in the middle and wrong for the final one: the closing
    # sentence re-read a little slower runs past the end of the video and its
    # tail is simply dropped — "the last track is truncated", followed by the
    # click of an abrupt cut. The mix is allowed to outlast the source; the
    # renderer holds the final frame over the difference.
    need = 0
    for seg in segs:
        if seg.get("drop") or seg.get("locked") or not seg.get("audio"):
            continue
        wav = seg["audio"]
        if not os.path.exists(wav):
            continue
        with contextlib.closing(wave.open(wav)) as w:
            take = w.getnframes() * (rate / float(w.getframerate()))
        take *= float(seg.get("stretch") or 1.0)
        at = seg["start"] + float(seg.get("offset") or 0.0)
        need = max(need, int(at * rate) + int(take))
    tail_pad = int(0.35 * rate)          # somewhere for the fade to live
    if need + tail_pad > nframes:
        grow = need + tail_pad - nframes
        base.extend(array.array("h", bytes(grow * ch * 2)))
        C.fill_with_room(base, rate, ch, nframes, nframes + grow, tone)
        nframes += grow

    # Hard fillers that sit in the GAPS BETWEEN sentences are not covered by
    # any segment span, so replacing the sentences leaves them untouched and an
    # "um" survives into the mix even though no take contains one. Duck them
    # first, on the original audio, with duck_spans' ramps and room-tone bed.
    # Hard fillers only: a soft filler is a real word, and a stutter needs the
    # sentence re-read - a half-second hole mid-phrase is worse than the stumble.
    # Pad each filler slightly. duck_spans crossfades INTO the bed across its
    # 45ms ramps, so on a 0.26s "um" the first and last 45ms are still the
    # original - the onset survives and you hear the start of the um. Padding
    # puts those ramps in the quiet either side instead of over the filler.
    pad = 0.04
    gap_fillers = []
    if synthetic:
        plan = dict(plan, fillers=[])     # there is no original to duck
    for a, b in plan.get("fillers", []):
        if any(s["start"] <= a and b <= s["end"] for s in segs):
            continue                     # inside a sentence; its span handles it
        lo = max(0.0, a - pad)
        hi = b + pad
        for s in segs:                   # never ramp over a neighbour's speech
            if s["end"] <= a:
                lo = max(lo, s["end"])
            if s["start"] >= b:
                hi = min(hi, s["start"])
        if hi > lo:
            gap_fillers.append((lo, hi))
    ducked = C.duck_spans(base, rate, ch, gap_fillers,
                          [(0.0, nframes / float(rate))]) if gap_fillers else 0

    # The recording's own speaking level, as the anchor for every take. Matching
    # a take purely to the span it replaces goes wrong when that span is quiet
    # for some other reason - s037's original measured -54 dB, so per-span
    # matching drove its take 12 dB down and it became the loudest mismatch in
    # the mix rather than the best fit.
    overall_voice = C.speech_level(base, rate, ch, 0, nframes)
    # The room's own level, as the yardstick for "this has gone quiet".
    room_r = C._rms(tone[0]) if tone else 0.0
    edge_thresh = room_r * 4.0          # +12 dB over the room

    for si, seg in enumerate(segs):
        # A line the operator kept real, or has not rendered yet, still OCCUPIES
        # its original span - it is their own voice, playing untouched.
        spoken_end, rendered = seg["end"], False
        # A DROPPED line contributes nothing: no take, and its own recording is
        # replaced by room tone. The span then reads as reclaimable air to the
        # renderer, which glides through it like any other silence.
        if seg.get("drop"):
            i0 = max(prev_placed_end(placed, rate), int(seg["start"] * rate))
            i1 = min(nframes, int(seg["end"] * rate))
            C.fill_with_room(base, rate, ch, i0, i1, tone)
            placed.append({"id": seg["id"], "start": round(seg["start"], 3),
                           "end": round(seg["start"], 3), "rendered": False,
                           "dropped": True,
                           "original": [round(seg["start"], 3), round(seg["end"], 3)]})
            continue
        usable = (not seg.get("locked") and seg.get("audio")
                  and os.path.exists(seg["audio"]))
        if usable:
            y, r2, c2 = C._read_wav(seg["audio"])
            # Give a line more room. A short take against a long pause reads as
            # clipped; stretching it into the silence that follows is free,
            # because that silence was going to be glided away regardless.
            st = float(seg.get("stretch") or 1.0)
            if abs(st - 1.0) > 0.02:
                st = max(0.7, min(1.6, st))
                y = _retime(y, r2, c2, st)
            # A nudge moves the take inside its own gap so the operator can
            # lengthen a pause or take up dead space. Bounded by the take
            # already placed before it and by the next sentence, so a nudge can
            # never make two lines overlap.
            floor_t = placed[-1]["end"] + 0.02 if placed else 0.0
            # The next line's PLACED start, not its position in the original
            # recording.
            #
            # Using the original start ignores every nudge the operator has
            # made and every offset the timing fit computed, so a take that
            # would run past where its neighbour USED to be gets pulled
            # earlier — silently, and by a different amount each time. The
            # timeline draws takes where they were asked to go, the mix put
            # them somewhere else, and the two drifted apart down the video:
            # measured at -1.4s by the middle, a median half-second late on,
            # which is exactly "the waveform doesn't match the voice blocks".
            if si + 1 < len(segs):
                nxt_seg = segs[si + 1]
                ceil_t = nxt_seg["start"] + float(nxt_seg.get("offset") or 0.0) - 0.02
            else:
                ceil_t = nframes / float(rate)
            at = seg["start"] + float(seg.get("offset") or 0.0)
            at = max(floor_t, min(at, ceil_t))
            start = int(at * rate)
            n = min(len(y) // c2, nframes - start)
            if r2 == rate and n > 2 * fade:
                # REPLACE the span this line stands in for — do not merely duck
                # it. Attenuating to -28 dB leaves the original words audible
                # underneath the take, saying the same thing at a different
                # rate, which is heard as a ghost or a faint double. Overwrite
                # with room tone at the local floor instead, so the background
                # never moves and nothing survives to compete with the take.
                span_end = min(nframes, int(seg["end"] * rate))
                # Match the take to the loudness of the voice it stands in for,
                # measured on speaking frames only. Without this, takes arrive
                # at whatever level the model produced and the mix steps in
                # volume from line to line.
                local = C.speech_level(base, rate, ch, int(seg["start"] * rate), span_end)
                # ANCHOR ON THE WHOLE RECORDING, not on the span.
                #
                # Matching each take to the span it replaces copies that span's
                # level exactly — including drift the speaker never intended. A
                # seven-minute read gets quieter as it goes: measured 1.7 dB
                # across this one, which the mix then turned into 3.5 dB,
                # because a quiet span pulls its take down and the next quiet
                # span pulls the next one further. The voice recedes while the
                # room tone stays where it is, and the back half sounds like it
                # is being spoken from further away.
                #
                # So follow the span only PARTLY. A line the operator genuinely
                # dropped his voice for still sits lower than its neighbours;
                # a line that is quieter only because it came later does not.
                if overall_voice > 0 and local > 0:
                    # Gently, and within a decibel. The unify pass has already
                    # put every take on one speaking level; this only has to
                    # keep a deliberately quiet line quiet relative to its
                    # neighbours, and a wider allowance simply readmits the
                    # session drift it is here to reject — at +/-2 dB the late
                    # takes all sat pinned to the floor of the clamp.
                    follow = (local / overall_voice) ** 0.25
                    want = overall_voice * min(max(follow, 0.89), 1.12)   # +/-1 dB
                else:
                    want = local
                have = C.speech_level(y, rate, c2, 0, n)
                gain = min(max((want / have) if have > 0 else 1.0, 0.25), 4.0)
                # Bring the take's own hiss down to the room it is being laid
                # into, or the background steps up the moment the line starts.
                # A HUMAN take was recorded in this room, so its floor already
                # IS the room — expanding it would gate the speaker's own
                # breath and ambience. Only synthesis carries a foreign floor.
                if room_r > 0 and not seg.get("human"):
                    y = C.tame_floor(y, r2, c2, room_r / max(gain, 1e-6))
                # If the take runs past its hole, clear the overrun as well —
                # but never past the next line, whose audio may be real.
                nxt = int(segs[si + 1]["start"] * rate) if si + 1 < len(segs) else nframes
                # Follow the real audio past the transcript boundary to the
                # END of the last speech before the next sentence, so no
                # fragment of the word survives the take that replaced it.
                tail = span_end
                if edge_thresh > 0:
                    _, last = _loud_bounds(base, rate, ch, span_end,
                                           min(nxt, nframes), edge_thresh)
                    tail = last or span_end
                # Never clip the clear below the take's OWN extent. A take can
                # be LONGER than the hole it fills (s003: a 2.70s take in a
                # 1.94s span, ending 0.21s past the next sentence's original
                # start). Capping at `nxt` then left a strip of the recording
                # playing underneath the take's tail — heard as a burst of
                # background at the END of the line. Extending past `nxt` is
                # safe because the next take is placed after this one ends.
                cap = max(nxt, start + n)
                clear_to = min(max(span_end, start + n, tail), cap, nframes)
                # The hole stays where the recording put it even when the take
                # is nudged away from it. Clearing only from the take's new
                # position left the original's onset playing in front of a
                # take nudged right (s002 at +0.92s), and its tail exposed
                # behind one nudged left.
                # The FURTHEST point already laid down, via the helper — not
                # the last entry. A line left out records an entry ending at
                # its own original start, which is earlier than the take before
                # it, and clearing from there wipes that take: line 51 played
                # 350.07-354.22, line 52 was dropped at 349.30, and line 53
                # then cleared from 351.20 — leaving 1.14s of a 4.15s take.
                prev_end = prev_placed_end(placed, rate)
                clear_from = max(prev_end, min(start, int(seg["start"] * rate)))
                C.fill_with_room(base, rate, ch, clear_from, clear_to, tone)
                cleared.append((clear_from, clear_to))
                # Fade in across the take's OWN lead-in, not a fixed 18ms.
                # A generated take often opens with 100-160ms of model noise
                # before the first word - measured -22 to -35 dBFS against a
                # -58 dBFS bed, which is the spike heard at the top of a line.
                # The ramp stops short of the speech onset so a quiet initial
                # consonant still arrives at full level (a tight trim here is
                # what once turned "Today" into "oday").
                head = tail = fade
                wq = max(1, int(0.02 * rate))
                nw = n // wq
                if nw > 4:
                    lv = [C._rms(y[k * wq * c2:(k + 1) * wq * c2]) for k in range(nw)]
                    ref = sorted(lv)[int(len(lv) * 0.85)]
                    # A LOW threshold on purpose: it must catch the quiet
                    # consonant that opens a word, so the ramp finishes before
                    # any real sound rather than 60ms short of the vowel. A
                    # fixed guard left the burst audible on takes whose noise
                    # runs right up to the speech (+17.9 dB on s005).
                    onset = next((k for k, v in enumerate(lv) if v >= ref * 0.10), 0)
                    head = min(int(onset * wq) - int(0.02 * rate), n // 3)
                    head = max(fade, head)
                    # The same at the end. Every take trails model noise after
                    # its last word — measured -42..-44 dBFS for up to 160ms,
                    # some 15 dB above the bed, which is the spike heard at the
                    # END of a line. Start the ramp 40ms AFTER the last word so
                    # the word's own release is untouched: trimming that is
                    # what once made a line "hold zero emotion and just end".
                    last = max((k for k, v in enumerate(lv) if v >= ref * 0.10),
                               default=nw - 1)
                    quiet = n - (int((last + 1) * wq) + int(0.04 * rate))
                    # CAP IT. A fade exists to stop a click at the join and to
                    # take off the 160ms of model noise that trails a take; a
                    # quarter of a second does both. Allowing a third of the
                    # take turns a fade into an edit: when the speech detector
                    # puts the last word early — it uses a fraction of the 85th
                    # percentile, and a quiet closing syllable sits under that
                    # — the ramp starts over words still being spoken. Measured
                    # on a 3.48s line: the last 0.74s, running -17 to -42 dBFS
                    # in the take, arrived in the mix 21 to 30 dB down. Heard,
                    # correctly, as the line being truncated.
                    tail = max(fade, min(quiet, int(0.25 * rate)))
                for k in range(n):
                    g = 1.0
                    if k < head:
                        g = k / head
                    elif n - k < tail:
                        g = (n - k) / tail
                    for c in range(ch):
                        i = (start + k) * ch + c
                        base[i] = max(-32768, min(32767,
                                     int(base[i] + y[k * c2 + min(c, c2 - 1)] * g * gain)))
                spoken_end, rendered = at + n / rate, True
        # Report EVERY line, not just the rendered ones. The retimer works out
        # which gaps are dead air from the sentence layout as a whole; hand it
        # only the rendered lines and it reads a kept-real neighbour as silence.
        placed.append({"id": seg["id"],
                       "start": round(at if rendered else seg["start"], 3),
                       "end": round(spoken_end, 3), "rendered": rendered,
                       "original": [round(seg["start"], 3), round(seg["end"], 3)]})

    # NOTHING BUT TAKES AND ROOM SURVIVES.
    #
    # Every line clears the stretch it replaces, and the bounds of those
    # clears have to agree with each other for the whole timeline to come out
    # covered. They repeatedly have not: a line nudged later, a line left out,
    # a neighbour that moved — each one shifts a boundary, and what falls
    # through is the ORIGINAL RECORDING at full level. One such gap here held
    # 0.2s at -1 dBFS, which is the spike seen in the waveform above a stretch
    # with no block under it at all.
    #
    # So stop relying on the boundaries agreeing. Take the union of what is
    # actually in the mix — every placed take, and every line kept as the real
    # recording — and fill everything else with room tone. A gap between
    # sentences is room either way; the difference is that this cannot leave a
    # word behind.
    keep = []
    for x in placed:
        if x.get("dropped"):
            continue
        if x.get("rendered"):
            keep.append((int(x["start"] * rate), int(x["end"] * rate)))
        else:
            # NOT RENDERED YET is not the same as gone. The loop above leaves
            # such a line playing as the operator's own voice, on purpose, and
            # this pass used to wipe it anyway: with no line rendered the whole
            # timeline became a few seconds of room tone, which the level-up
            # below then lifted 45 dB into white noise across the entire track.
            a, b = x["original"]
            keep.append((int(a * rate), int(b * rate)))
    for seg in segs:
        if seg.get("locked"):
            keep.append((int(seg["start"] * rate), int(seg["end"] * rate)))
    keep.sort()
    merged = []
    for a, b in keep:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    at = 0
    for a, b in merged:
        if a > at:
            C.fill_with_room(base, rate, ch, at, min(a, nframes), tone)
        at = max(at, b)
    if at < nframes:
        C.fill_with_room(base, rate, ch, at, nframes, tone)

    # Close the slivers between one replaced span and the next.
    #
    # Each line clears the original audio it stands in for, and two adjacent
    # lines can leave a few milliseconds uncleared between them — 20ms here,
    # between a line ending at 322.76 and the next beginning at 322.78. That
    # sliver is the ORIGINAL RECORDING at full level: measured -5 dBFS against
    # a -54 dBFS bed, 20 milliseconds long. Too short to be a word, far too
    # loud to be room — heard and SEEN as a spike, in this case on the last
    # line of the video.
    #
    # Only true slivers are closed. A real gap between sentences is the room
    # the operator recorded in and belongs in the mix.
    SLIVER = int(0.25 * rate)
    cleared.sort()
    for (a0, a1), (b0, b1) in zip(cleared, cleared[1:]):
        if 0 < b0 - a1 <= SLIVER:
            C.fill_with_room(base, rate, ch, a1, b0, tone)

    # Fade the last quarter-second to nothing. A mix that simply stops is a
    # step from room tone to digital silence, and a step is a click — heard as
    # a spike on the final line whatever that line contains.
    tail_n = min(int(0.25 * rate), nframes)
    for i in range(tail_n):
        g = (tail_n - i) / float(tail_n)
        k = (nframes - tail_n + i) * ch
        for c in range(ch):
            base[k + c] = int(base[k + c] * g)

    # The bed goes on LAST, after every fill and duck has had its say - those
    # zero the spans they touch, so anything laid down earlier would be punched
    # full of holes exactly at the boundaries it exists to smooth.
    if synthetic:
        lay_bed(base, rate, ch, float(plan.get("bed_db", -68.0)))

    # Bring the whole mix up to where the master would put it.
    #
    # Everything auditioned in the studio is THIS file, not the mastered one,
    # and it was landing thirteen decibels down — quiet enough that it could
    # not be judged against anything else playing. The master targets -14 LUFS
    # and a -1.5 dBFS true peak; a plain peak normalise to the same ceiling
    # gets the preview close enough to hear honestly, and costs nothing at the
    # master, which measures what it is given and normalises again.
    #
    # One gain over the finished mix, so every relative level set upstream —
    # take against take, bed against take — survives untouched.
    import array as _a
    import numpy as _np
    _cur = _np.frombuffer(base, dtype=_np.int16)
    _peak = int(_np.abs(_cur.astype(_np.int32)).max()) if len(_cur) else 0
    _want = 32767 * (10.0 ** (-1.5 / 20.0))
    # Capped. This lift exists for a mix that is merely quiet — 13 dB down —
    # and without a cap it lifts whatever is there, however little: a track
    # holding nothing but room tone at -62 dB came up 45 dB into hiss. A mix
    # that needs more than this is not quiet, it is empty, and should sound it.
    _gain = min(_want / _peak, 10.0 ** (20.0 / 20.0)) if _peak else 1.0
    if 0 < _peak < _want:
        _out = _np.clip(_cur.astype(_np.float32) * _gain,
                        -32768, 32767).astype(_np.int16)
        base[:] = _a.array("h", _out.tobytes())

    C._write_wav(out, base, rate, ch)
    cleaned = clean_background(out, _np.frombuffer(base, dtype=_np.int16), rate, ch) \
        if plan.get("clean_background", True) else {"cleaned": False, "why": "switched off"}
    return {"path": out, "duration": round(nframes / rate, 3), "placed": placed,
            "ducked": ducked, "background": cleaned}


def clean_background(path, samples, rate, ch):
    """Take the snow out of the background — gently.

    The voice clone reproduces its sample's background about 40 dB under the
    voice, a little brighter than the sample itself: steady hiss, heard as
    "snow" once the master lifts it. A noise model alone barely touched it
    (-2 to -4 dB: it is not noise-like to RNNoise); what worked was RNNoise
    followed by a gate that lowers the pauses.

    Two strengths were compared on a whole video by ear. A deep gate (pauses
    at -82 dB) left the background audibly coming back under every phrase —
    "breathing". A gentle one (ratio 2, range -12 dB: pauses at -73 dB, 56 dB
    under the voice against 37 before) cleared the snow and "preserves
    something in the voice better". This is the gentle one.

    The gate's threshold follows the mix's own speaking level (27 dB under
    it), because the mix is quieter than the mastered video it was tuned on.
    Never fatal: a failure leaves the mix as it was.
    """
    import numpy as _np
    model = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models", "sh.rnnn")
    if not os.path.exists(model):
        return {"cleaned": False, "why": "no RNNoise model"}
    y = _np.asarray(samples, dtype=_np.float32).reshape(-1) / 32768.0
    if ch > 1:
        y = y[: len(y) // ch * ch].reshape(-1, ch).mean(axis=1)
    h = max(1, int(rate * 0.05))
    n = len(y) // h
    if n < 10:
        return {"cleaned": False, "why": "too short"}
    lv = 20 * _np.log10(_np.sqrt((y[: n * h].reshape(n, h) ** 2).mean(axis=1)) + 1e-9)
    speech = float(_np.median(lv[lv > lv.max() - 30]))
    thr = 10 ** ((speech - 27.0) / 20.0)
    af = (f"arnndn=m={model}:mix=0.9,"
          f"agate=threshold={thr:.6f}:ratio=2:attack=10:release=250:range=0.25:knee=4")
    tmp = path + ".clean.wav"
    # check=False: util.run EXITS on failure, and a failed clean must leave
    # the uncleaned mix, not take the whole assembly down with it.
    r = run(["ffmpeg", "-v", "error", "-y", "-i", path, "-af", af, "-ar", str(rate),
             "-ac", str(ch), "-c:a", "pcm_s16le", tmp], check=False)
    if r.returncode != 0 or not os.path.exists(tmp):
        if os.path.exists(tmp):
            os.remove(tmp)
        return {"cleaned": False, "why": (r.stderr or "ffmpeg failed").strip()[-200:]}
    os.replace(tmp, path)
    return {"cleaned": True, "speech_db": round(speech, 1), "gate_db": round(speech - 27.0, 1)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("plan")
    ap.add_argument("-o", "--output", required=True)
    a = ap.parse_args()
    print(json.dumps(assemble(json.load(open(a.plan)), a.output)))


if __name__ == "__main__":
    main()
