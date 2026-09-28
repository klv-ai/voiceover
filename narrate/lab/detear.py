"""Repair screen tearing in a recording.

A screen recorder that grabs the framebuffer without waiting for the display
refresh catches it mid-update: the top of the frame is the new picture and the
bottom is still the old one, with a hard horizontal boundary between them. The
capture is already ruined by the time anything downstream sees it, and every
render reproduces it faithfully — re-rendering lands the tear in exactly the
same place, because it is in the source frames.

WHAT IT IS NOT. A tear is not a bright horizontal edge: a screencast is full of
those — toolbars, table rows, window borders — and a detector that looks for
row contrast flags 42% of a perfectly good video. What identifies a tear is a
STEP in how much each row changed since the previous frame: everything above
the boundary is new, everything below is identical to before. Motion spreads
change across many rows; a static UI edge produces no change at all.

The repair is to drop the torn frame. At 60fps its neighbour is 17ms away and
the substitution is invisible, which is the whole reason tearing is tolerable
to record in the first place.
"""
import argparse
import json
import os
import subprocess
import sys

W, H = 320, 180          # detection runs on a thumbnail; the seam is a whole-frame event
MIN_MOTION = 2.0         # below this nothing moved, so there is nothing to tear
STEP = 1.45              # how step-like the row-change profile has to be


def sh(cmd, **kw):
    return subprocess.run(cmd, check=True, **kw)


def detect(src, fps):
    """Frame numbers that are torn."""
    import numpy as np
    p = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-i", src, "-vf", f"fps={fps},scale={W}:{H}",
         "-pix_fmt", "gray", "-f", "rawvideo", "-"],
        stdout=subprocess.PIPE, bufsize=W * H * 8)
    prev = None
    torn = []
    n = 0
    while True:
        b = p.stdout.read(W * H)
        if len(b) < W * H:
            break
        cur = np.frombuffer(b, dtype=np.uint8).reshape(H, W).astype(np.float32)
        if prev is not None:
            d = np.abs(cur - prev).mean(axis=1)
            if d.max() >= MIN_MOTION:
                cs = np.cumsum(d)
                tot = cs[-1]
                ys = np.arange(8, H - 8)
                top = cs[ys - 1] / ys
                bot = (tot - cs[ys - 1]) / (H - ys)
                gap = float(np.abs(top - bot).max())
                if gap / max(float(d.mean()), 0.5) > STEP:
                    torn.append(n)
        prev = cur
        n += 1
    p.stdout.close()
    p.wait()
    return torn, n


def ranges(nums):
    """Consecutive runs, so the filter expression stays short."""
    out = []
    for k in nums:
        if out and k == out[-1][1] + 1:
            out[-1][1] = k
        else:
            out.append([k, k])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("-o", "--output", required=True)
    ap.add_argument("--fps", type=float, default=0)
    ap.add_argument("--report")
    a = ap.parse_args()

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=r_frame_rate", "-of", "csv=p=0", a.src],
        capture_output=True, text=True).stdout.strip()
    num, den = (probe.split("/") + ["1"])[:2]
    fps = a.fps or (float(num) / float(den or 1))

    torn, total = detect(a.src, fps)
    rep = {"frames": total, "torn": len(torn),
           "percent": round(100.0 * len(torn) / max(1, total), 2)}

    if not torn:
        # Nothing to do, and re-encoding a clean video for nothing would only
        # cost a generation of quality.
        rep["action"] = "left alone"
        if a.report:
            json.dump(rep, open(a.report, "w"), indent=2)
        print(json.dumps(rep))
        return 0

    # Drop the torn frames and let `fps` fill each hole by holding the frame
    # before it. The timestamps of the surviving frames are left ALONE: a
    # `setpts` here renumbers them and closes the gaps instead of filling them,
    # which shortens the video — measured 25.05s down to 23.87s on a test
    # slice — and every frame of that is a frame the audio no longer lines up
    # with. The point is to replace frames, not remove time.
    expr = "+".join(f"between(n,{x},{y})" for x, y in ranges(torn))
    graph = f"select='not({expr})',fps={fps:g}"
    sh(["ffmpeg", "-v", "error", "-i", a.src, "-vf", graph,
        "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p",
        "-c:a", "copy", "-movflags", "+faststart", a.output, "-y"])
    rep["action"] = f"replaced {len(torn)} frame(s)"
    if a.report:
        json.dump(rep, open(a.report, "w"), indent=2)
    print(json.dumps(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
