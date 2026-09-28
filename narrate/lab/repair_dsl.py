"""A repair request: the DSL that tells a voice model what hole it is filling.

The point is that a synthesis request carries far more than words. Handing a
TTS engine a sentence and dropping the result into a gap is what makes injected
audio obvious: it arrives at the wrong length, the wrong loudness, its own idea
of melody, and against a silent background while everything around it has a
room. A request therefore describes the SPAN, not just the text —

    say        the words, with the stumble removed
    duration   how long the hole is, to the millisecond
    f0         the pitch contour actually measured either side and across it
    energy     the loudness envelope it has to match
    terminal   whether the phrase falls, holds or rises out of the span
    room       the noise floor it has to sit on, and where to borrow it from
    seam       where to cut in and out, chosen for level match, not word edges

Anything that can be conditioned on these fulfils the request directly;
anything that cannot gets post-fitted to them. Either way the injected span is
answerable to its neighbours rather than to a generic voice.

For a repeated phrase the rewrite is mechanical — drop the first copy — so no
language model is needed for the common case. It is there for false starts and
garbled runs, which are the minority.

    ../.venv/bin/python3 lab/repair_dsl.py <analysis.json> -o requests.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import prosody                      # noqa: E402
from narrate import analyze as A    # noqa: E402
from narrate import cut as C        # noqa: E402

CONTOUR_POINTS = 24


def _seam_point(samples, rate, ch, t, window=0.06):
    """Quietest instant near t — where a splice can hide."""
    half = max(1, int(0.004 * rate))
    stride = max(1, ch * 4)
    span = int(window * rate)
    centre = int(t * rate)
    lo = max(half, centre - span)
    hi = min(len(samples) // ch - half - 1, centre + span)
    if hi <= lo:
        return t, -120.0
    best, best_db = centre, None
    for f in range(lo, hi, max(1, rate // 500)):
        v = C._frame_db(samples, ch, f, half, stride)
        if best_db is None or v < best_db:
            best, best_db = f, v
    return best / rate, best_db


def _resample_curve(ts, vals, n=CONTOUR_POINTS):
    if len(ts) < 2:
        return []
    grid = np.linspace(ts[0], ts[-1], n)
    out = np.interp(grid, ts, vals)
    return [[round(float(a - ts[0]), 4), round(float(b), 2)] for a, b in zip(grid, out)]


def build_all_segments(analysis_path: str, voice_ref: str | None = None):
    """One request per transcript segment: replace the whole narration.

    Span-level injection was rejected on listening — a clone spliced beside the
    speaker's own voice in the same phrase is compared directly by the ear and
    loses, even with the channel matched, because it lacks the micro-timing and
    imperfection of a real take ("like two different sessions"). Replacing
    everything removes the comparison: there is no real voice adjacent to
    measure it against. That is a different proposition, not a better version
    of the same one, and it has its own failure mode — consistency across a
    whole video, and fatigue.
    """
    d = json.load(open(analysis_path))
    src = d["source"]["path"]
    ws = d["transcript"]["words"]
    drop = A.disfluency_drops(ws, fillers=True, repeats=True)

    # Sentence-level chunks, not Whisper segments. Segments here run 14-22
    # seconds, which is both a long single generation and a large time-fit to
    # the original hole. A sentence is the natural unit: it anchors to its own
    # start time, so drift cannot accumulate across the video, and the video
    # keeps describing what the narration is describing.
    bounds, cur = [], []
    for i, w in enumerate(ws):
        if i in drop:
            continue
        cur.append(i)
        if w["w"].strip().endswith((".", "!", "?")) and len(cur) >= 3:
            bounds.append(cur)
            cur = []
    if len(cur) >= 3:
        bounds.append(cur)

    requests = []
    for n, idx in enumerate(bounds, 1):
        say = " ".join(ws[i]["w"].strip() for i in idx)
        say = " ".join(say.split()).replace(" ,", ",").replace(" .", ".")
        if len(say.split()) < 3:
            continue
        s0, s1 = ws[idx[0]]["start"], ws[idx[-1]]["end"]
        if s1 - s0 < 0.5:
            continue
        requests.append({
            "id": f"s{n:03d}",
            "span": [round(s0, 3), round(s1, 3)],
            "seam": [round(s0, 3), round(s1, 3)],
            "heard": "",
            "say": say,
            "why": ["full replacement"],
            "target": {"duration": round(s1 - s0, 3)},
            "fit": {"max_stretch": 0.35, "xfade_ms": 16,
                    "match_level": True, "room_tone": True},
        })
    # Pad tightly. A breath usually begins within a fraction of a second of the
    # previous word ending, so generous padding round every word blocks the very
    # gaps the breaths live in — it cut the hit rate from 8 to 2.
    spoken = [[round(w["start"] - 0.02, 3), round(w["end"] + 0.02, 3)] for w in ws]
    return {"version": 1, "source": src, "voice_ref": voice_ref,
            "engine": "chatterbox", "mode": "replace_all",
            "spoken_spans": spoken, "requests": requests}


def build_requests(analysis_path: str, voice_ref: str | None = None,
                   pad: float = 0.06):
    d = json.load(open(analysis_path))
    src = d["source"]["path"]
    ws = d["transcript"]["words"]
    edl = d["edl"]

    base = os.path.dirname(analysis_path)
    stem = os.path.splitext(os.path.basename(src))[0]
    raw = f"{base}/{stem}_src.wav"
    if not os.path.exists(raw):
        from narrate.util import run
        run(["ffmpeg", "-v", "error", "-i", src, "-map", "0:a:0", "-vn",
             "-c:a", "pcm_s16le", "-ar", "48000", raw, "-y"])
    sm, rate, ch = C._read_wav(raw)
    keep = [tuple(r) for r in edl["keep"]]
    ref_ranges = C.refine_boundaries(sm, rate, ch, keep, seam_floor=-34.0)

    x16 = prosody.load(raw)
    t16, f0, db, ap = prosody.f0_track(x16)

    # Surviving repeats, grouped by the PHRASE they sit in — not by the words
    # themselves. A request covering only "it's, it's" would have the model
    # synthesise a single word and splice it mid-phrase, with both seams hard
    # against speech and no context for coarticulation: the hardest possible
    # case. Replacing the whole segment puts both seams at the pauses either
    # side of a clause, where they can hide, and gives the model enough context
    # to produce natural joins into its own neighbours.
    reps = set(A.repeat_drops(ws))
    alive = {i for i in reps
             if any(a <= (ws[i]["start"] + ws[i]["end"]) / 2 <= b for a, b in ref_ranges)}

    # Expand from each repair to the nearest real PAUSE either side, capped.
    # Whisper segments are the wrong unit: some run 30 seconds, and replacing
    # 30 seconds of real audio with synthesis abandons the whole premise of
    # keeping most of the recording real. A pause is where a seam can hide, so
    # grow to the closest one and stop.
    def gap_before(i):
        return ws[i]["start"] - ws[i - 1]["end"] if i > 0 else 99.0

    def gap_after(i):
        return ws[i + 1]["start"] - ws[i]["end"] if i + 1 < len(ws) else 99.0

    MIN_PAUSE, MAX_SPAN = 0.18, 4.0
    groups, used = [], set()
    for h in sorted(alive):
        if h in used:
            continue
        lo = hi = h
        # expand to the nearest pause, but never into a span already claimed —
        # without that guard consecutive repairs each drag in their neighbour's
        # territory and the "islands" merge into most of the recording
        while lo > 0 and (lo - 1) not in used and gap_before(lo) < MIN_PAUSE \
                and ws[hi]["end"] - ws[lo - 1]["start"] < MAX_SPAN:
            lo -= 1
        while hi + 1 < len(ws) and (hi + 1) not in used and gap_after(hi) < MIN_PAUSE \
                and ws[hi + 1]["end"] - ws[lo]["start"] < MAX_SPAN:
            hi += 1
        idx = list(range(lo, hi + 1))
        hits = [i for i in idx if i in alive]
        used.update(idx)
        groups.append((idx, hits, None))

    tone_donor = None
    tone = C.find_room_tone(sm, rate, ch)
    if len(tone):
        tone_donor = "quietest 0.40s of the source"

    requests = []
    for n, (idx, hit, seg) in enumerate(groups, 1):
        i0, i1 = idx[0], idx[-1]
        s0, s1 = ws[i0]["start"], ws[i1]["end"]
        if s1 - s0 < 0.30:
            continue

        seam_in, lvl_in = _seam_point(sm, rate, ch, s0 - pad)
        seam_out, lvl_out = _seam_point(sm, rate, ch, s1 + pad)
        if seam_out - seam_in < 0.3:
            continue

        heard = " ".join(ws[i]["w"].strip() for i in idx)
        # we are re-saying this anyway, so drop the fillers too
        drop = set(hit) | {i for i in idx if A.norm(ws[i]["w"]) in A.HARD_FILLERS}
        say = " ".join(ws[i]["w"].strip() for i in idx if i not in drop)
        say = " ".join(say.split()).replace(" ,", ",").replace(" .", ".")
        if len(say.split()) < 2:
            continue

        m = (t16 >= s0) & (t16 <= s1) & ~np.isnan(f0)
        if m.sum() < 4:
            continue
        tv = t16[m]
        hz = f0[m]
        e = db[(t16 >= s0) & (t16 <= s1)]
        et = t16[(t16 >= s0) & (t16 <= s1)]
        st = prosody.semitones(hz)
        step = prosody.terminal_step(t16, f0, s0, s1)

        # local room floor either side of the span
        win = int(0.06 * rate)
        nfr = len(sm) // ch
        floors = []
        for lo, hi in ((max(0, int((s0 - 0.5) * rate)), int(s0 * rate)),
                       (int(s1 * rate), min(nfr, int((s1 + 0.5) * rate)))):
            for f in range(lo, max(lo, hi - win), win):
                floors.append(C._rms(sm[f * ch:(f + win) * ch]))
        floor_db = 20 * np.log10(max(min(floors) if floors else 1.0, 1.0) / 32768.0)

        requests.append({
            "id": f"r{n:03d}",
            "span": [round(s0, 3), round(s1, 3)],
            "seam": [round(seam_in, 3), round(seam_out, 3)],
            "seam_level_dbfs": [round(lvl_in, 1), round(lvl_out, 1)],
            "heard": heard,
            "say": say,
            "why": [f"{len(hit)} repeated word(s) removed",
                    f"{len(drop) - len(hit)} filler(s) removed"],
            "target": {
                "duration": round(s1 - s0, 3),
                "f0_median_hz": round(float(np.median(hz)), 1),
                "f0_range_st": round(float(np.percentile(st, 75) - np.percentile(st, 25)), 2),
                "f0_contour_hz": _resample_curve(tv, hz),
                "energy_env_dbfs": _resample_curve(et, e),
                "terminal": ("fall" if step == step and step < -0.4
                             else "rise" if step == step and step > 0.4 else "level"),
                "room_floor_dbfs": round(float(floor_db), 1),
                "room_donor": tone_donor,
            },
            "fit": {"max_stretch": 0.15, "xfade_ms": 24,
                    "match_level": True, "room_tone": True},
        })

    return {
        "version": 1,
        "source": src,
        "voice_ref": voice_ref,
        "engine": "chatterbox",
        "requests": requests,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("analysis")
    ap.add_argument("-o", "--output", required=True)
    ap.add_argument("--voice-ref")
    ap.add_argument("--all", action="store_true",
                    help="one request per segment — replace the whole narration")
    a = ap.parse_args()
    doc = (build_all_segments(a.analysis, a.voice_ref) if a.all
           else build_requests(a.analysis, a.voice_ref))
    json.dump(doc, open(a.output, "w"), indent=2)
    print(f"  {len(doc['requests'])} repair requests -> {a.output}")
    for r in doc["requests"][:5]:
        print(f"    {r['id']} {r['span'][0]:7.2f}s {r['target']['duration']:4.2f}s "
              f"{r['target'].get('terminal','-'):<5} heard={r['heard'][:38]!r}")
        print(f"           say={r['say'][:60]!r}")


if __name__ == "__main__":
    main()
