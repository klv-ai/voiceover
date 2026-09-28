"""Close dead air by SPEEDING THE PICTURE THROUGH IT, not by cutting it.

A ripple delete removes the gap and the frames with it, which on a screencast
reads as a jump. Once a script is tightened the gaps are everywhere, and a
video full of jumps is worse than one that is slightly slow. Gliding keeps
every frame and simply moves through the silence quickly: the cursor slides,
the page settles, and the narration picks up again.

A gap the operator has HELD is left at full length — a beat they want. So is
anything shorter than `gap_min`, and the head of the video.

Both streams are compressed by the SAME factor over the same span, so nothing
drifts; the audio there is room tone, and speeding room tone is inaudible.

    ../.venv/bin/python3 lab/glide.py plan.json -o out.mp4
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from narrate.util import probe, run  # noqa: E402


def atempo_chain(speed: float) -> str:
    """atempo only accepts 0.5-2.0 per instance, so chain them."""
    parts, s = [], float(speed)
    while s > 2.0:
        parts.append("atempo=2.0")
        s /= 2.0
    while s < 0.5:
        parts.append("atempo=0.5")
        s /= 0.5
    if abs(s - 1.0) > 1e-3:
        parts.append(f"atempo={s:.6f}")
    return ",".join(parts)


def build_segments(placed, dur, holds, gap_min, speed, residual, fps=None):
    """The whole timeline as (start, end, speed) spans, in order.

    Boundaries are snapped to the FRAME GRID when `fps` is given. `trim` cuts
    the picture on whole frames while `atrim` is sample-exact, so an unsnapped
    boundary leaves the two a fraction of a frame apart — harmless once, and
    across sixty-odd spans it accumulated into 0.17s of measured lag.
    """
    q = (lambda t: round(t * fps) / fps) if fps else (lambda t: t)
    segs = []
    prev_end, prev_id = 0.0, None
    for p in placed:
        a, b = prev_end, p["start"]
        if b - a > 0.01:
            # The head of the video is never compressed: whatever is there is
            # the opening, not dead air after a line.
            held = prev_id is None or holds.get(prev_id, False)
            if not held and (b - a) >= gap_min:
                keep = min(b - a, residual)
                if b - keep > a + 0.01:
                    segs.append((q(a), q(b - keep), speed))
                segs.append((q(max(a, b - keep)), q(b), 1.0))
            else:
                segs.append((q(a), q(b), 1.0))
        # SPANS MUST TILE, never overlap.
        #
        # Each span is trimmed out of the mix independently and the results
        # concatenated, so two spans covering the same stretch put that audio
        # into the output TWICE - heard as a phrase repeating, and only in the
        # final, because the preview never goes through here. It happens when
        # a take runs past the start of the line after it: the gap branch above
        # correctly adds nothing, and then the line's own span was appended
        # starting before the previous one had ended.
        #
        # Measured on this video: two overlapping pairs, 1.29s and 0.57s, and
        # the operator heard exactly two repeats.
        #
        # Clamping is the whole fix. The audio is not lost - it belongs to the
        # take that got there first, which is the one still playing.
        s0 = max(q(p["start"]), q(prev_end))
        s1 = max(q(p["end"]), s0)
        if s1 - s0 > 0.0:
            segs.append((s0, s1, 1.0))
        prev_end, prev_id = max(prev_end, p["end"]), p["id"]
    if dur - prev_end > 0.01:
        segs.append((q(prev_end), q(dur), 1.0))

    # Merge neighbours that run at the same rate; every extra span is another
    # trim/concat pair in the graph.
    out = []
    for s in segs:
        if out and abs(out[-1][2] - s[2]) < 1e-6 and abs(out[-1][1] - s[0]) < 1e-6:
            out[-1] = (out[-1][0], s[1], s[2])
        else:
            out.append(tuple(s))
    return [s for s in out if s[1] - s[0] > 0.02]


def apply_cuts(segs, cuts, source_dur, fps=None):
    """Take deleted sections out of the spans — picture and sound together.

    The spans tile the timeline, so removing the cut ranges from them leaves
    exactly what is kept, each piece at the rate it already had. Edges snap to
    the frame grid like every other boundary here, so a cut cannot open the
    fraction-of-a-frame gap between picture and sound that unsnapped spans
    once accumulated into 0.17 s of lag.

    A cut that runs to the end of the recording runs to the end of the
    VIDEO, so it also takes any frame held after the last line.
    """
    if not cuts:
        return segs
    q = (lambda t: round(t * fps) / fps) if fps else (lambda t: t)
    ranges = []
    for a, b in cuts:
        a, b = float(a), float(b)
        if b >= source_dur - 0.01:
            b = float("inf")
        if b > a:
            ranges.append((q(a), b if b == float("inf") else q(b)))
    ranges.sort()
    out = []
    for s0, s1, sp in segs:
        pieces = [(s0, s1)]
        for a, b in ranges:
            nxt = []
            for x, y in pieces:
                if b <= x or a >= y:
                    nxt.append((x, y))
                    continue
                if a > x:
                    nxt.append((x, a))
                if b < y:
                    nxt.append((b, y))
            pieces = nxt
        out.extend((x, y, sp) for x, y in pieces if y - x > 0.02)
    return out


def mask_graph(masks, src, out):
    """Filters that blur each mask's rectangle for its span, from pad `src`
    to pad `out`. Masks are (from, to, x, y, w, h): source seconds, and
    fractions of the frame.

    Applied on the SOURCE timeline, before the picture is split into spans,
    so a mask follows every cut and glided gap by construction. The blur is a
    heavy box blur scaled to the rectangle — enough that no text in it can be
    read — and it is switched off outside the mask's span, so a mask costs
    nothing where it is not showing.
    """
    if not masks:
        return f"[{src}]null[{out}];"
    g, cur = "", src
    for i, (a, b, x, y, w, h) in enumerate(masks):
        a, b = float(a), float(b)
        nxt = out if i == len(masks) - 1 else f"mk{i}o"
        on = f"between(t\\,{a:.3f}\\,{b:.3f})"
        g += (f"[{cur}]split=2[mk{i}a][mk{i}b];"
              f"[mk{i}b]crop=w=iw*{w:.4f}:h=ih*{h:.4f}:x=iw*{x:.4f}:y=ih*{y:.4f},"
              f"boxblur=luma_radius='min(w\\,h)/6':luma_power=3:"
              f"chroma_radius='min(cw\\,ch)/6':chroma_power=3:enable='{on}'[mk{i}c];"
              f"[mk{i}a][mk{i}c]overlay=x=main_w*{x:.4f}:y=main_h*{y:.4f}:enable='{on}'[{nxt}];")
        cur = nxt
    return g


# What a browser will actually decode.
#
# H.264 Level 5.2 is the practical ceiling in browser media stacks and consumer
# hardware decoders: 983,040 macroblocks per second. A Mac screen recording is
# 4096x2178, and at 60fps that is 2,104,320 — more than double. ffmpeg encodes
# it happily, flags it Level 6.0, and the file is perfectly valid; it is just
# asking for more than anything can play. The browser software-decodes, falls
# behind and gives up partway through, at a point that moves with the picture
# complexity, which is why it looked like a render that kept failing in a
# different place each time.
#
# Leave real headroom under the limit rather than sitting on it.
MB_BUDGET = 850_000


def fit_for_playback(w, h, fps):
    """The largest size within the decode budget, keeping the aspect ratio."""
    def mbs(W, H):
        return (W // 16) * ((H + 15) // 16) * fps
    if mbs(w, h) <= MB_BUDGET:
        return w, h
    scale = (MB_BUDGET / mbs(w, h)) ** 0.5
    # Land on a 16-pixel grid. A macroblock is 16x16, and a decoder handed a
    # size that does not divide cleanly pads the edge internally — harmless in
    # principle, and the sort of thing that upsets a hardware path in practice.
    W = max(320, int(w * scale) // 16 * 16)
    H = max(176, int(round(W * h / w)) // 16 * 16)
    while mbs(W, H) > MB_BUDGET and W > 640:
        W -= 16
        H = max(176, int(round(W * h / w)) // 16 * 16)
    return W, H


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("plan")
    ap.add_argument("-o", "--output", required=True)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    d = json.load(open(a.plan))
    src, audio = d["source"], d["audio"]
    holds = {k: bool(v) for k, v in (d.get("holds") or {}).items()}
    info = probe(src)
    fps = d.get("fps") or info.fps or 30.0
    dur = float(d.get("duration") or info.duration)

    # A closing line re-read a little slower runs past the end of the source,
    # and every span here is bounded by the picture — so the sound was trimmed
    # to fit and the last track arrived truncated, ending in the click of an
    # abrupt cut. Hold the final frame instead, and extend the timeline the
    # spans are built over so the extra sound is actually carried. The pad has
    # to happen BEFORE the split: padding the concatenated result is too late,
    # the audio has already been cut by then.
    tail = float(d.get("tail_hold") or 0.0)
    hold = round(tail + 0.35, 3) if tail > 0.04 else 0.0
    if hold:
        dur += hold
        print(f"  holding the last frame {hold:.2f}s so the closing line is not cut")
    segs = build_segments(d["placed"], dur, holds,
                          float(d.get("gap_min", 0.8)),
                          float(d.get("speed", 3.0)),
                          float(d.get("residual", 0.28)), fps=fps)
    cuts = d.get("cuts") or []
    if cuts:
        before = sum((b - a) / sp for a, b, sp in segs)
        segs = apply_cuts(segs, cuts, float(d.get("duration") or info.duration), fps=fps)
        after = sum((b - a) / sp for a, b, sp in segs)
        print(f"  {len(cuts)} section(s) deleted: {before - after:.1f}s of video removed")

    sped = [s for s in segs if s[2] != 1.0]
    out_dur = sum((b - a) / sp for a, b, sp in segs)
    print(f"  {len(segs)} spans, {len(sped)} glided at {d.get('speed', 3.0)}x")
    print(f"  {dur:.1f}s -> {out_dur:.1f}s  (recovers {dur - out_dur:.1f}s of dead air)")
    if holds:
        print(f"  {sum(1 for v in holds.values() if v)} gap(s) held at full length")

    n = len(segs)
    labels = [f"[v{i}][a{i}]" for i in range(n)]
    vsplit = "".join(f"[sv{i}]" for i in range(n))
    asplit = "".join(f"[sa{i}]" for i in range(n))
    vpad = f"tpad=stop_mode=clone:stop_duration={hold:.3f}," if hold else ""
    ow, oh = fit_for_playback(info.width, info.height, fps)
    vscale = ""
    if (ow, oh) != (info.width, info.height):
        vscale = f"scale={ow}:{oh}:flags=lanczos,"
        print(f"  {info.width}x{info.height} -> {ow}x{oh} so it stays inside "
              f"what a browser can decode at {fps:g}fps")
    masks = d.get("masks") or []
    if masks:
        print(f"  {len(masks)} blur mask(s)")
    graph = (f"[0:v]fps={fps:g}[vsrc];"
             + mask_graph(masks, "vsrc", "vmasked")
             + f"[vmasked]{vpad}{vscale}format=yuv420p,split={n}{vsplit};"
             f"[1:a]asplit={n}{asplit};")
    for i, (s0, s1, sp) in enumerate(segs):
        graph += (f"[sv{i}]trim=start={s0:.4f}:end={s1:.4f},"
                  f"setpts=(PTS-STARTPTS)/{sp:g}[v{i}];")
        # A glided span is room tone by construction — it lies BETWEEN takes,
        # never across one. So take exactly the length the picture will occupy
        # rather than tempo-shifting it. atempo does not return precisely
        # n/speed samples, and chained (2.0 then 1.5 for 3x) the deficit
        # compounds: measured +0.08s of lag by 20s growing to +0.48s by 250s
        # across 31 spans. A plain trim is sample-exact and cannot drift, and
        # room tone sounds like room tone at any length.
        # Give the audio exactly as long as the picture will actually be: the
        # output is a whole number of FRAMES, so derive the length from that
        # rather than from the raw ratio.
        out_frames = max(1, round((s1 - s0) / sp * fps))
        a0, a1 = (s0, s1) if sp == 1.0 else (s0, s0 + out_frames / fps)
        graph += (f"[sa{i}]atrim=start={a0:.4f}:end={a1:.4f},"
                  f"asetpts=PTS-STARTPTS[a{i}];")
    graph += "".join(labels) + f"concat=n={n}:v=1:a=1[vo][ao]"

    if a.dry_run:
        print(graph[:1500])
        return 0

    run(["ffmpeg", "-v", "error", "-i", src, "-i", audio,
         "-filter_complex", graph, "-map", "[vo]", "-map", "[ao]",
         "-c:v", "libx264", "-crf", "18", "-preset", "medium",
         "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
         a.output, "-y"])
    got = probe(a.output)
    print(f"  wrote {a.output}  {got.duration:.2f}s")
    print(json.dumps({"duration": round(got.duration, 3),
                      "spans": n, "glided": len(sped),
                      "saved": round(dur - out_dur, 2)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
