#!/usr/bin/env python3
"""Join several recordings into one video, in the order given.

A video is often recorded in sittings — stop, fix something, start again — and
the pieces used to go through Final Cut only to be glued end to end before they
came here. This does the gluing, so a project can start from all of them.

Every piece is fitted to the first one's frame (scaled, letterboxed if it
differs), at its nominal frame rate, and the whole is RE-ENCODED — constant
frame rate, clean timestamps, the same as the Final Cut export it replaces.

Why not copy the pieces end to end, losslessly? Tried, measured, rejected. The
screen recorder stores decode timestamps far behind presentation time — 1.7 s
in one recording, 8.8 s in another — and not by a constant amount (some frames
sit at zero). Placed end to end, the second piece's decode times start before
the first piece's end; ffmpeg forces them into order one tick apart, and the
MOV index built from them no longer seeks: every seek into that piece returned
no frames while straight playback looked perfect. Matroska seeks correctly but
reports the recording's AVERAGE rate (49.8 fps) as nominal, and remuxed back to
MOV its millisecond timestamps read as 120 fps; the pipeline would render at
either. Shifting each piece's decode times as a block fails on the frames that
have no slack. Re-encoding is the one join that is right every time.

The sound is resampled with `async`, which pads the start of each piece with
silence: these recorders start the audio 0.2-0.35 s after the picture, and
without the pad every seam would slide the words early by that much.

    python3 lab/join.py OUT.mov part1.mov part2.mov …

Prints JSON: {"path", "duration", "parts": [{"name", "start", "duration"}]}
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from narrate.util import run  # noqa: E402


def probe(path: str) -> dict:
    p = run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format",
             "-show_streams", path], check=False)
    if p.returncode != 0:
        raise SystemExit(f"cannot read {os.path.basename(path)}: {(p.stderr or '').strip()[-200:]}")
    d = json.loads(p.stdout or "{}")
    ss = d.get("streams", [])
    # The first REAL picture: screencasts carry mjpeg thumbnails as extra
    # video streams, which are not the recording.
    v = next((s for s in ss if s.get("codec_type") == "video"
              and not (s.get("disposition") or {}).get("attached_pic")
              and s.get("codec_name") != "mjpeg"), None)
    a = next((s for s in ss if s.get("codec_type") == "audio"), None)
    if not v:
        raise SystemExit(f"{os.path.basename(path)} has no picture")
    return {"path": path, "v": v, "a": a, "duration": float(d.get("format", {}).get("duration") or 0)}


def fps_of(v: dict) -> str:
    """The nominal rate — r_frame_rate, which is 60/1 for these recordings
    even though they average less. Never avg_frame_rate."""
    r = v.get("r_frame_rate") or ""
    num, _, den = r.partition("/")
    try:
        f = float(num) / float(den or 1)
        if 1 <= f <= 120:
            return r
    except (ValueError, ZeroDivisionError):
        pass
    return "60/1"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("parts", nargs="+")
    a = ap.parse_args()

    parts = [probe(p) for p in a.parts]
    first = parts[0]["v"]
    w, h, fps = int(first["width"]), int(first["height"]), fps_of(first)
    rate = Fraction(fps)
    args, graph, legs = [], [], []
    for i, m in enumerate(parts):
        # Every piece is given an exact length BEFORE it is joined: all of it —
        # picture or sound, whichever runs on longer — rounded up to a whole
        # frame. The concat filter otherwise runs each piece for its longer
        # stream, and which one that is varies: one recording's sound ran
        # 0.87 s past its picture, and the seams stopped being where the
        # lengths said (1188.75 s joined against 1187.91 s expected). Now the
        # picture holds its last frame and the sound is padded with silence to
        # the same frame, so the seams are exactly where they are recorded and
        # no sound is cut.
        vd = float(m["v"].get("duration") or 0)
        ae = (float(m["a"].get("start_time") or 0) + float(m["a"].get("duration") or 0)) if m["a"] else 0
        end = max(m["duration"], vd, ae)
        m["frames"] = max(1, math.ceil(round(end * rate, 6)))
        m["length"] = float(m["frames"] / rate)
        samples = round(m["length"] * 48000)
        args += ["-i", m["path"]]
        graph.append(f"[{i}:{m['v']['index']}]fps={fps},"
                     f"scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos,"
                     f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,format=yuv420p,"
                     f"setpts=PTS-STARTPTS,tpad=stop_mode=clone:stop_duration={end - vd + 1:.3f},"
                     f"trim=end_frame={m['frames']},setpts=PTS-STARTPTS[v{i}]")
        if m["a"]:
            graph.append(f"[{i}:{m['a']['index']}]aresample=48000:async=1:first_pts=0,"
                         f"aformat=sample_fmts=fltp:channel_layouts=stereo,"
                         f"apad,atrim=end_sample={samples},asetpts=PTS-STARTPTS[a{i}]")
        else:
            graph.append(f"anullsrc=r=48000:cl=stereo,atrim=end_sample={samples}[a{i}]")
        legs.append(f"[v{i}][a{i}]")
    graph.append("".join(legs) + f"concat=n={len(parts)}:v=1:a=1[v][a]")

    tmp = a.out + ".joining" + os.path.splitext(a.out)[1]
    r = run(["ffmpeg", "-v", "error", "-y", *args, "-filter_complex", ";".join(graph),
             "-map", "[v]", "-map", "[a]",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "256k", tmp], check=False)
    if r.returncode != 0:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise SystemExit(f"could not join the recordings: {(r.stderr or '').strip()[-300:]}")

    # Trust nothing: the length must add up and the sound must run as long as
    # the picture, or the transcript will be timed against the wrong frames.
    m = probe(tmp)
    want = sum(p["length"] for p in parts)
    vd = float(m["v"].get("duration") or m["duration"])
    ad = float((m["a"] or {}).get("duration") or 0)
    bad = (f"length {m['duration']:.2f}s, expected {want:.2f}s"
           if abs(m["duration"] - want) > 0.1 + 0.02 * len(parts)
           else f"sound {ad:.2f}s against picture {vd:.2f}s" if abs(vd - ad) > 0.3 else "")
    if bad:
        os.remove(tmp)
        raise SystemExit(f"the joined video is wrong: {bad}")
    os.replace(tmp, a.out)

    # Where each piece begins in the joined video.
    starts, t = [], 0.0
    for p in parts:
        starts.append({"name": os.path.basename(p["path"]), "start": round(t, 3),
                       "duration": round(p["length"], 3)})
        t += p["length"]
    result = {"path": a.out, "duration": round(m["duration"], 3), "parts": starts}
    # Beside the video, so a build that fails AFTER the join can be retried
    # without doing the join again.
    with open(a.out + ".join.json", "w") as f:
        json.dump(result, f)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
