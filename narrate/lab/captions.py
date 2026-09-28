"""Subtitles for the finished video.

Every fact needed is already known: the script is the exact words, and the
render knows where each line was placed. The only hard part is that the
finished video is not the source — the glide pass speeds the picture through
the dead air between lines, so a line spoken at 3:42 in the recording arrives
somewhere earlier in the output, and by a different amount for every line.
Subtitles written against the source timeline would drift further out of sync
with every gap that was closed.

So map each line through the same span list the renderer used. That list is
deterministic from the plan the render already wrote, so the captions cannot
disagree with the video unless the video is rebuilt with different settings.

Writes WebVTT or SRT to stdout.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import glide as G                       # noqa: E402

# YouTube shows two lines comfortably; past that it truncates or scrolls.
MAX_CHARS = 42
MAX_LINES = 2
MAX_CUE_S = 6.0


def out_time(segs, t):
    """Where a source time lands in the rendered video."""
    acc = 0.0
    for a, b, sp in segs:
        if t < a:
            return acc
        if t <= b:
            return acc + (t - a) / sp
        acc += (b - a) / sp
    return acc


def wrap(text):
    """Break a cue into at most two readable lines."""
    words, lines, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > MAX_CHARS:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines


def split_cue(text, t0, t1):
    """One line may be too long to sit on screen as a single caption.

    Split it into cues of at most two rows, and give each cue a share of the
    line's time proportional to its share of the words — the reader's pace
    follows the speaker's, and nothing here knows word-level timing.
    """
    rows = wrap(text)
    if len(rows) <= MAX_LINES and (t1 - t0) <= MAX_CUE_S:
        return [(t0, t1, rows)]
    out, span = [], max(0.1, t1 - t0)
    total = sum(len(r.split()) for r in rows) or 1
    at = t0
    for i in range(0, len(rows), MAX_LINES):
        chunk = rows[i:i + MAX_LINES]
        share = sum(len(r.split()) for r in chunk) / total
        end = min(t1, at + span * share)
        out.append((at, end, chunk))
        at = end
    return out


def stamp(t, comma=False):
    if t < 0:
        t = 0.0
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    frac = f"{s:06.3f}"
    return f"{int(h):02d}:{int(m):02d}:" + (frac.replace(".", ",") if comma else frac)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("plan", help="the glide plan the render used")
    ap.add_argument("--text", required=True, help="JSON: {id: say}")
    ap.add_argument("--format", default="vtt", choices=["vtt", "srt"])
    a = ap.parse_args()

    d = json.load(open(a.plan))
    says = json.load(open(a.text))
    info = G.probe(d["source"])
    fps = d.get("fps") or info.fps or 30.0
    dur = float(d.get("duration") or info.duration)
    tail = float(d.get("tail_hold") or 0.0)
    if tail > 0.04:
        dur += round(tail + 0.35, 3)
    segs = G.build_segments(d["placed"], dur, {k: bool(v) for k, v in (d.get("holds") or {}).items()},
                            float(d.get("gap_min", 0.8)), float(d.get("speed", 3.0)),
                            float(d.get("residual", 0.28)), fps=fps)
    # The same deleted sections the render took out, or every caption after a
    # cut would run early by the length of it.
    segs = G.apply_cuts(segs, d.get("cuts") or [], float(d.get("duration") or info.duration), fps=fps)

    cues = []
    for pl in d["placed"]:
        text = (says.get(pl["id"]) or "").strip()
        if not text:
            continue
        t0, t1 = out_time(segs, pl["start"]), out_time(segs, pl["end"])
        if t1 - t0 < 0.25:
            continue
        cues.extend(split_cue(text, t0, t1))
    cues.sort(key=lambda c: c[0])
    # A cue must never outlast the next one's start.
    for i in range(len(cues) - 1):
        if cues[i][1] > cues[i + 1][0]:
            cues[i] = (cues[i][0], cues[i + 1][0], cues[i][2])

    lines = []
    if a.format == "vtt":
        lines.append("WEBVTT\n")
        for t0, t1, rows in cues:
            lines.append(f"{stamp(t0)} --> {stamp(t1)}")
            lines.extend(rows)
            lines.append("")
    else:
        for i, (t0, t1, rows) in enumerate(cues, 1):
            lines.append(str(i))
            lines.append(f"{stamp(t0, True)} --> {stamp(t1, True)}")
            lines.extend(rows)
            lines.append("")
    sys.stdout.write("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
