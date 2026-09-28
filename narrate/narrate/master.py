"""Tier 0 — the mastering chain.

Nothing here is clever; it is the standard spoken-word chain, in the standard
order, with a two-pass loudnorm so the final number is the number we asked for
rather than the number a single-pass dynamic normaliser drifted to.

Video is stream-copied. Only the audio is re-encoded, so running this after a
cut costs seconds and loses no picture quality.
"""
from __future__ import annotations

import json
import math
import os
import re

from .util import info, ok, probe, run, step, warn, audio_report, loudness


COMPRESSION = {
    # ratio, threshold dB, attack ms, release ms, makeup dB
    "none":   None,
    "gentle": (1.6, -22.0, 15, 250, 1.0),
    "firm":   (3.0, -20.0, 5, 140, 3.0),
}

_MODELS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models")


def rnn_model(name: str = "mp") -> str | None:
    path = name if os.path.isabs(name) else os.path.join(_MODELS, f"{name}.rnnn")
    return path if os.path.exists(path) else None


def chain(*, denoise: str = "rnn", denoise_model: str = "mp", denoise_mix: float = 0.9,
          fft_strength: float = 5.0, mud: float = -1.5, presence: float = 1.5,
          deess: float = 0.25, compress: str = "none",
          noise_floor: float = -50.0, highpass: int = 75) -> list[str]:
    """The spoken-word chain.

    Denoising defaults to ffmpeg's `arnndn` (RNNoise). Measured on this footage
    it lifts the speech-to-pause ratio by 16 dB while leaving speech level
    within 0.2 dB. The `afftdn` spectral denoiser this replaced moved the same
    number by 0.2 dB — it was audibly colouring the sound while removing
    essentially no noise.

    `denoise_mix` keeps a little of the original room under the result. Fully
    denoised pauses read as dropouts, because a room that goes to digital
    silence between phrases is not a room anyone has been in.

    Compression defaults to OFF. Any compressor — even 1.6:1 — pulls the
    loudness range from this source's 6.4 LU down to about 4.3, and that
    flattening is what reads as "processed". Peak control comes from the
    limiter after loudnorm, which only touches transients that would clip.
    """
    links = [f"highpass=f={highpass}"]

    if denoise == "rnn":
        model = rnn_model(denoise_model)
        if model:
            links.append(f"arnndn=m={model}:mix={denoise_mix:g}")
        else:
            # DO NOT fall back to afftdn. This file's own note on it: it moved
            # the speech-to-pause ratio by 0.2 dB while audibly colouring the
            # sound. Falling back to it means every master on a box without an
            # RNNoise model quietly runs the denoiser that was replaced for
            # being harmful - which is what was happening here, and is a good
            # part of "there is distortion added to the output".
            warn(f"no RNNoise model {denoise_model!r} in {_MODELS} — "
                 f"denoising OFF rather than falling back to afftdn")
            denoise = "none"
    if denoise == "fft" and fft_strength > 0:
        links.append(f"afftdn=nr={fft_strength:g}:nf={noise_floor:g}:tn=1")

    links.append("adeclick")
    if mud:
        links.append(f"equalizer=f=250:t=q:w=1.2:g={mud:g}")
    if presence:
        links.append(f"equalizer=f=3200:t=q:w=1.8:g={presence:g}")
    if deess:
        links.append(f"deesser=i={deess:g}")
    spec = COMPRESSION.get(compress)
    if spec:
        ratio, thresh, atk, rel, makeup = spec
        links.append(f"acompressor=threshold={thresh:g}dB:ratio={ratio:g}"
                     f":attack={atk:g}:release={rel:g}:makeup={makeup:g}")
    return links


def _measure(src: str, pre: str, lufs: float, tp: float, lra: float) -> dict | None:
    """Pass 1: run the chain and let loudnorm report what it saw."""
    af = f"{pre},loudnorm=I={lufs}:TP={tp}:LRA={lra}:print_format=json"
    p = run(["ffmpeg", "-hide_banner", "-i", src, "-map", "0:a:0", "-vn",
             "-af", af, "-f", "null", "-"], check=False)
    m = re.findall(r"\{[^{}]*\"input_i\"[^{}]*\}", p.stderr or "", re.S)
    if not m:
        return None
    try:
        return json.loads(m[-1])
    except json.JSONDecodeError:
        return None


def master(src: str, out: str, *, lufs: float = -14.0, tp: float = -1.5,
           lra: float = 30.0, denoise: str = "rnn", denoise_model: str = "mp",
           denoise_mix: float = 0.9, presence: float = 1.5,
           compress: str = "none", two_pass: bool = True,
           show_chain: bool = False) -> str:
    # LRA IS A TARGET, AND ASKING FOR A NARROW ONE IS COMPRESSION.
    #
    # 14 (and the 11 broadcast usually wants) is narrower than a clean
    # narration mix, so loudnorm squeezes the range to fit - lifting the quiet
    # passages toward the loud ones, which is heard as the level floating and
    # as hiss appearing between phrases. Measured on this video:
    #
    #     as-is          speech -17.5  floor -56.8   ratio 39.3 dB
    #     LRA 11         speech -14.1  floor -48.0   ratio 33.9 dB
    #     LRA 30         speech -14.1  floor -52.4   ratio 38.3 dB
    #
    # All three hit the loudness target. Only the wide one leaves the mix
    # alone getting there, which is the entire job here: the takes were
    # already levelled upstream and do not want levelling again.
    src_info = probe(src)
    before = audio_report(src)

    # Anchor the denoiser to the material's own noise floor rather than a
    # guess: 3 dB above measured is aggressive enough to matter and gentle
    # enough not to chew the tails off words.
    # A recording that contains digital silence measures a noise floor of -inf,
    # which would render as "nf=-inf" in the filter string and fail the run.
    nf = before.get("noise_floor")
    floor = round(nf + 3, 1) if isinstance(nf, (int, float)) and math.isfinite(nf) else -50.0

    pre = ",".join(chain(denoise=denoise, denoise_model=denoise_model,
                         denoise_mix=denoise_mix, presence=presence,
                         compress=compress, noise_floor=floor))

    step(f"master — denoise: {denoise}, compression: {compress}, target {lufs:g} LUFS")
    info(f"measured noise floor {nf} dBFS")
    if show_chain:
        for link in pre.split(","):
            info(f"    {link}")

    # ONE GAIN FOR THE WHOLE FILE, then a limiter for the peaks.
    #
    # This used to be loudnorm with linear=true, and the log said "linear
    # correction". It was not. loudnorm only honours linear when the gain it
    # needs keeps the peaks under the ceiling; otherwise it reverts to DYNAMIC
    # mode and rides the level on a ~3 s window without a word of warning.
    # Speech this dynamic never qualifies: measured on a finished narration,
    # -20.8 LUFS with peaks at -1.2 dBTP needs +6.8 dB, which is a peak at
    # +5.6, so ffmpeg reported "normalization_type": "dynamic" and the master
    # swung 4.7 dB on a slow curve - heard as the sound drifting louder and
    # quieter in a wave. A fixed gain with a fast limiter swung 0.7.
    #
    # The limiter sits under the true-peak target because it watches samples
    # and the peaks BETWEEN samples land higher; a -2 dB ceiling measured -1.3.
    ceiling = tp - 1.0
    gain = 0.0
    if two_pass:
        m = _measure(src, pre, lufs, tp, lra)
        if m:
            gain = lufs - float(m["input_i"])
            info(f"pass 1: input {m['input_i']} LUFS, TP {m['input_tp']} dBTP "
                 f"→ fixed gain {gain:+.2f} dB, peaks limited at {ceiling:g} dB")
        else:
            warn("pass 1 measurement unavailable — no gain applied, peaks limited only")

    tmp = out + ".master.wav"

    def render(g: float) -> dict:
        af = (f"{pre},volume={g:.2f}dB,"
              # latency=1: the limiter looks 5 ms ahead and would otherwise
              # delay the sound against the picture by that much.
              f"alimiter=limit={ceiling:g}dB:attack=5:release=50:level=disabled:latency=1")
        run(["ffmpeg", "-v", "error", "-i", src, "-map", "0:a:0", "-vn", "-af", af,
             "-c:a", "pcm_f32le", "-ar", "48000", tmp, "-y"])
        return loudness(tmp)

    got = render(gain)
    # The limiter takes some energy off the peaks, so the result lands a little
    # short. Make that up ONCE, still as a single fixed gain.
    short = (lufs - got["lufs"]) if isinstance(got.get("lufs"), (int, float)) else 0.0
    if two_pass and short > 0.3:
        info(f"limiter cost {short:.1f} LU; adding it back as fixed gain")
        gain += short
        got = render(gain)

    cmd = ["ffmpeg", "-v", "error", "-stats", "-i", src, "-i", tmp]
    if src_info.has_video:
        # v:0 only — thumbnail/mjpeg tracks must not ride along.
        cmd += ["-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
                "-c:a", "aac", "-b:a", "320k", "-ar", "48000",
                "-movflags", "+faststart"]
    else:
        cmd += ["-map", "1:a:0", "-c:a", "pcm_s16le", "-ar", "48000"]
    cmd += [out, "-y"]
    try:
        run(cmd, capture=False)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)

    after = audio_report(out)
    ok(f"{out}")
    _delta(before, after)
    return out


def _delta(before: dict, after: dict) -> None:
    rows = [("integrated", "lufs", "LUFS"), ("true peak", "true_peak", "dBFS"),
            ("loudness range", "lra", "LU"), ("crest", "crest", "dB"),
            ("noise floor", "noise_floor", "dBFS")]
    print()
    print(f"    {'':<16}{'before':>10}{'after':>10}")
    for label, key, unit in rows:
        b, a = before.get(key), after.get(key)
        if b is None and a is None:
            continue
        bs = f"{b:.1f}" if isinstance(b, (int, float)) else "—"
        as_ = f"{a:.1f}" if isinstance(a, (int, float)) else "—"
        print(f"    {label:<16}{bs:>10}{as_:>10}  {unit}")
    print()
