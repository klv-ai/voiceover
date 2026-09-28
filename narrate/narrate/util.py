"""Shared helpers: subprocess wrapping, ffprobe/ffmpeg introspection, logging.

Everything in narrate/ is stdlib-only. Heavy work is shelled out to ffmpeg
and whisper, so the CLI itself installs nowhere and runs on any python3 on
the box.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass

# ---------------------------------------------------------------- logging

_COLOR = sys.stderr.isatty() and os.environ.get("NO_COLOR") is None


def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if _COLOR else s


def info(msg: str) -> None:
    print(_c("36", "  ›"), msg, file=sys.stderr, flush=True)


def step(msg: str) -> None:
    print(_c("1;35", f"\n▸ {msg}"), file=sys.stderr, flush=True)


def ok(msg: str) -> None:
    print(_c("32", "  ✓"), msg, file=sys.stderr, flush=True)


def warn(msg: str) -> None:
    print(_c("33", "  !"), msg, file=sys.stderr, flush=True)


def die(msg: str, code: int = 1):
    print(_c("1;31", "✗ " + msg), file=sys.stderr, flush=True)
    raise SystemExit(code)


def hms(sec: float) -> str:
    sec = max(0.0, float(sec))
    m, s = divmod(sec, 60)
    h, m = divmod(int(m), 60)
    return f"{h:d}:{m:02d}:{s:05.2f}" if h else f"{m:d}:{s:05.2f}"


# ------------------------------------------------------------ subprocess

def run(cmd: list[str], *, capture: bool = True, check: bool = True) -> subprocess.CompletedProcess:
    """Run a command. stderr is always captured (ffmpeg reports on stderr)."""
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE,
        text=True,
    )
    if check and proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-15:])
        die(f"command failed ({proc.returncode}): {' '.join(cmd[:6])} …\n{tail}")
    return proc


def require(tool: str, hint: str = "") -> str:
    path = shutil.which(tool)
    if not path:
        die(f"'{tool}' not found on PATH. {hint}")
    return path


def have(tool: str) -> bool:
    return shutil.which(tool) is not None


# ---------------------------------------------------------------- ffprobe

@dataclass
class MediaInfo:
    path: str
    duration: float
    has_video: bool
    has_audio: bool
    width: int = 0
    height: int = 0
    fps: float = 0.0
    vcodec: str = ""
    acodec: str = ""
    sample_rate: int = 0
    channels: int = 0
    nb_video_streams: int = 0

    @property
    def is_video(self) -> bool:
        return self.has_video


def probe(path: str) -> MediaInfo:
    if not os.path.exists(path):
        die(f"no such file: {path}")
    p = run(["ffprobe", "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", path])
    d = json.loads(p.stdout)
    streams = d.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fps = 0.0
    if v and v.get("r_frame_rate", "0/0") != "0/0":
        num, _, den = v["r_frame_rate"].partition("/")
        try:
            fps = float(num) / float(den or 1)
        except (ValueError, ZeroDivisionError):
            fps = 0.0
    return MediaInfo(
        path=path,
        duration=float(d.get("format", {}).get("duration", 0.0)),
        has_video=v is not None,
        has_audio=a is not None,
        width=int(v.get("width", 0)) if v else 0,
        height=int(v.get("height", 0)) if v else 0,
        fps=fps,
        vcodec=v.get("codec_name", "") if v else "",
        acodec=a.get("codec_name", "") if a else "",
        sample_rate=int(a.get("sample_rate", 0)) if a else 0,
        channels=int(a.get("channels", 0)) if a else 0,
        nb_video_streams=sum(1 for s in streams if s.get("codec_type") == "video"),
    )


# ------------------------------------------------------- audio measurement

def loudness(path: str) -> dict:
    """EBU R128 integrated loudness, LRA and true peak."""
    # -vn / -map 0:a:0 are load-bearing, not tidiness: without them ffmpeg maps
    # the video stream to the null muxer too and re-encodes it, which on a 4K60
    # screen recording costs minutes per measurement instead of seconds.
    p = run(["ffmpeg", "-hide_banner", "-i", path, "-map", "0:a:0", "-vn",
             "-af", "ebur128=peak=true", "-f", "null", "-"], check=False)
    out, res, section = p.stderr or "", {}, None
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("Integrated loudness"):
            section = "i"
        elif s.startswith("Loudness range"):
            section = "lra"
        elif s.startswith("True peak"):
            section = "tp"
        elif s.startswith("I:") and section == "i":
            res["lufs"] = _f(s.split()[1])
        elif s.startswith("LRA:") and section == "lra":
            res["lra"] = _f(s.split()[1])
        elif s.startswith("Peak:") and section == "tp":
            res["true_peak"] = _f(s.split()[1])
    return res


def astats(path: str) -> dict:
    """Overall RMS / peak / noise floor, in dBFS."""
    # NB: astats reports at loglevel info — "-v error" silences it completely.
    p = run(["ffmpeg", "-hide_banner", "-i", path, "-map", "0:a:0", "-vn",
             "-af", "astats", "-f", "null", "-"], check=False)
    keys = {
        "RMS level dB": "rms",
        "Peak level dB": "peak",
        "Noise floor dB": "noise_floor",
        "Flat factor": "flat_factor",
    }
    res: dict = {}
    for line in (p.stderr or "").splitlines():
        if "]" not in line:
            continue
        body = line.split("]", 1)[1].strip()
        for k, name in keys.items():
            if body.startswith(k + ":"):
                res[name] = _f(body.split(":", 1)[1])  # last stream wins == overall
    return res


def audio_report(path: str) -> dict:
    r = loudness(path)
    r.update(astats(path))
    if "rms" in r and "peak" in r:
        r["crest"] = round(r["peak"] - r["rms"], 2)
    return r


def _f(tok: str):
    try:
        return round(float(tok.strip().split()[0]), 2)
    except (ValueError, IndexError):
        return None


# ------------------------------------------------------------------ misc

def extract_audio(src: str, dst: str, *, rate: int = 48000, mono: bool = True) -> str:
    """Decode the first audio stream to PCM wav (explicitly a:0 — some of these
    recordings carry mjpeg thumbnail streams that confuse bare stream mapping)."""
    cmd = ["ffmpeg", "-v", "error", "-i", src, "-map", "0:a:0", "-vn",
           "-ar", str(rate), "-c:a", "pcm_s16le"]
    if mono:
        cmd += ["-ac", "1"]
    cmd += [dst, "-y"]
    run(cmd)
    return dst


def scratch(suffix: str = "") -> str:
    fd, p = tempfile.mkstemp(suffix=suffix, prefix="narrate_")
    os.close(fd)
    return p


def write_json(path: str, obj) -> None:
    with open(path, "w") as f:
        json.dump(obj, f, indent=2)


def read_json(path: str):
    if not os.path.exists(path):
        die(f"no such analysis file: {path}")
    with open(path) as f:
        return json.load(f)
