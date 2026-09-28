"""narrate — command-line front end."""
from __future__ import annotations

import argparse
import os
import sys

from . import analyze as A
from . import master as M
from . import transcribe as T
from .util import (audio_report, die, extract_audio, have, hms, info, ok, probe,
                   read_json, scratch, step, warn, write_json)

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LEXICON = os.path.join(os.path.dirname(HERE), "lexicon.txt")


def _stem(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0]


def _out(path: str, suffix: str, ext: str | None = None, outdir: str | None = None) -> str:
    d = outdir or os.path.dirname(os.path.abspath(path))
    e = ext or os.path.splitext(path)[1] or ".mp4"
    return os.path.join(d, f"{_stem(path)}.{suffix}{e}")


# --------------------------------------------------------------- analyze

def cmd_analyze(args) -> int:
    src = args.media
    mi = probe(src)
    out = args.output or _out(src, "analysis", ".json", args.outdir)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)

    step(f"analyze — {os.path.basename(src)}")
    info(f"{hms(mi.duration)}  {mi.width}x{mi.height}@{mi.fps:g}  "
         f"{mi.vcodec}/{mi.acodec} {mi.sample_rate}Hz x{mi.channels}")

    wav = scratch(".wav")
    extract_audio(src, wav)
    # Measure the SOURCE, not the mono 16k extraction: ebur128 on a downmix
    # reads ~3 LU quieter than the stereo master YouTube will actually receive.
    stats = audio_report(src)
    info(f"loudness {stats.get('lufs')} LUFS   true peak {stats.get('true_peak')} dBFS   "
         f"crest {stats.get('crest')} dB   floor {stats.get('noise_floor')} dBFS")

    lex_path = args.lexicon if args.lexicon else (
        DEFAULT_LEXICON if os.path.exists(DEFAULT_LEXICON) else None)
    tr = T.transcribe(wav, model=args.model, language=args.language,
                      lexicon=lex_path, engine=args.engine,
                      verbatim=not args.no_verbatim)

    lex_terms = []
    if lex_path and os.path.exists(lex_path):
        lex_terms = [ln.strip() for ln in open(lex_path)
                     if ln.strip() and not ln.lstrip().startswith("#")]

    flags = A.detect(tr["words"], tr["segments"], gap_min=args.gap_min,
                     low_conf=args.low_conf, lexicon_terms=lex_terms)
    met = A.metrics(tr["words"], flags, mi.duration)
    edl = A.build_edl(tr["words"], mi.duration, gap_max=args.gap_max,
                      gap_keep=args.gap_keep, cut_fillers=args.cut_fillers,
                      tempo=args.tempo)
    picks = A.build_pickups(tr["words"], tr["segments"], flags)

    doc = {
        "version": 1,
        "source": {"path": os.path.abspath(src), "duration": mi.duration,
                   "width": mi.width, "height": mi.height, "fps": mi.fps,
                   "video_streams": mi.nb_video_streams},
        "audio_stats": stats,
        "transcript": tr,
        "metrics": met,
        "flags": flags,
        "edl": edl,
        "pickups": picks,
    }
    write_json(out, doc)
    os.unlink(wav)
    ok(out)
    render_report(doc)
    return 0


# ---------------------------------------------------------------- report

def render_report(doc: dict) -> None:
    m, edl, stats = doc["metrics"], doc["edl"], doc["audio_stats"]
    src = doc["source"]
    flags = doc["flags"]

    print()
    print(f"  ── {os.path.basename(src['path'])} " + "─" * max(0, 52 - len(os.path.basename(src['path']))))
    print(f"  {'runtime':<22}{hms(src['duration'])}")
    print(f"  {'words':<22}{m['words']}")
    print(f"  {'pace':<22}{m['wpm']} wpm" + ("   ← draggy, 140-160 is the band" if m['wpm'] < 135 else ""))
    print(f"  {'dead air':<22}{m['gap_seconds']:.1f}s in {m['gap_count']} gaps "
          f"({m['gap_pct']}% of speech)" + ("   ← worth cutting" if m['gap_pct'] > 6 else ""))
    print(f"  {'longest gap':<22}{m['longest_gap']:.1f}s")
    print(f"  {'hard fillers':<22}{m['hard_fillers']}  ({m['fillers_per_min']}/min)")
    print(f"  {'stutters':<22}{m['stutters']}")
    print(f"  {'low confidence':<22}{m['low_conf']}  ({m['low_conf_per_min']}/min)")
    print(f"  {'brand misses':<22}{m['brand_misses']}"
          + ("   ← mangled product names" if m['brand_misses'] else ""))
    print(f"  {'loudness':<22}{stats.get('lufs')} LUFS  (target -14)")
    print(f"  {'true peak':<22}{stats.get('true_peak')} dBFS")
    print(f"  {'crest':<22}{stats.get('crest')} dB" +
          ("   ← uncontrolled dynamics" if (stats.get("crest") or 0) > 15 else ""))

    brands = [f for f in flags if f["kind"] == "brand"][:6]
    if brands:
        print("\n  brand misses")
        for f in brands:
            print(f"    {hms(f['start']):>9}  {f['detail']}")

    lows = sorted([f for f in flags if f["kind"] == "low_conf"], key=lambda f: f["p"])[:6]
    if lows:
        print("\n  least confident words")
        for f in lows:
            print(f"    {hms(f['start']):>9}  p={f['p']:.2f}  {f['text']!r}")

    print(f"\n  plan: {len(edl['keep'])} keep-ranges → {hms(edl['estimated_out'])} "
          f"(saves {edl['saved_seconds']:.0f}s at tempo {edl['tempo']:g}x)")
    if edl.get("post_cut_wpm"):
        st = edl["suggested_tempo"]
        line = f"  pace after cut: {edl['post_cut_wpm']} wpm"
        if st > 1.005:
            line += f"   →  --tempo {st:g} brings it to {edl['suggested_wpm']} wpm"
        else:
            line += "   (already in the band)"
        print(line)
    print(f"  pickups: {len(doc['pickups'])} sentences worth re-reading")
    print()


def cmd_redetect(args) -> int:
    """Re-run the detectors over a stored transcript.

    Transcription is the expensive step and the thresholds are the part you
    actually want to tune, so keep them separable.
    """
    doc = read_json(args.analysis)
    tr = doc["transcript"]
    lex_path = args.lexicon or (DEFAULT_LEXICON if os.path.exists(DEFAULT_LEXICON) else None)
    lex_terms = []
    if lex_path and os.path.exists(lex_path):
        lex_terms = [ln.strip() for ln in open(lex_path)
                     if ln.strip() and not ln.lstrip().startswith("#")]

    step(f"redetect — {len(tr['words'])} words, {len(lex_terms)} lexicon terms")
    src_path = doc["source"]["path"]
    if os.path.exists(src_path):
        doc["audio_stats"] = audio_report(src_path)
        info("refreshed audio measurements from source")
    doc["flags"] = A.detect(tr["words"], tr["segments"], gap_min=args.gap_min,
                            low_conf=args.low_conf, lexicon_terms=lex_terms)
    doc["metrics"] = A.metrics(tr["words"], doc["flags"], doc["source"]["duration"])
    doc["edl"] = A.build_edl(tr["words"], doc["source"]["duration"],
                             gap_max=args.gap_max, gap_keep=args.gap_keep,
                             cut_fillers=args.cut_fillers,
                             cut_stutters=args.cut_stutters, tempo=args.tempo)
    doc["pickups"] = A.build_pickups(tr["words"], tr["segments"], doc["flags"])
    out = args.output or args.analysis
    write_json(out, doc)
    ok(out)
    render_report(doc)
    return 0


def cmd_report(args) -> int:
    render_report(read_json(args.analysis))
    return 0


def cmd_master(args) -> int:
    src = args.media
    ext = ".mp4" if probe(src).has_video else ".wav"
    out = args.output or _out(src, "master", ext, args.outdir)
    M.master(src, out, lufs=args.lufs, tp=args.true_peak, lra=args.lra,
             denoise=args.denoise, denoise_model=args.denoise_model,
             denoise_mix=args.denoise_mix, presence=args.presence,
             compress=args.compress,
             two_pass=not args.single_pass, show_chain=args.show_chain)
    return 0


# ---------------------------------------------------------------- doctor

def cmd_doctor(args) -> int:
    step("environment")
    rows = [
        ("ffmpeg", have("ffmpeg"), "brew install ffmpeg"),
        ("ffprobe", have("ffprobe"), "brew install ffmpeg"),
        ("whisper (CPU)", have("whisper"), "python3 -m pip install openai-whisper"),
    ]
    mlx = T._mlx_interpreter()
    rows.append(("mlx_whisper (fast)", bool(mlx), "python3 -m pip install mlx-whisper"))
    for name, present, hint in rows:
        mark = "\033[32m✓\033[0m" if present else "\033[33m·\033[0m"
        print(f"  {mark} {name:<22}{'' if present else hint}")

    print()
    from . import master as MM
    for name in ("mp", "sh"):
        have_m = MM.rnn_model(name)
        mark = "\033[32m✓\033[0m" if have_m else "\033[33m·\033[0m"
        print(f"  {mark} RNNoise {name:<14}"
              + ("" if have_m else f"missing from {MM._MODELS}"))

    if not have("ffmpeg"):
        return 1
    if not (mlx or have("whisper")):
        warn("no whisper engine — `analyze` will not run")
        return 1
    print()
    ok("ready: analyze and master"
       + ("" if MM.rnn_model("mp") else "  (no RNNoise model — master's denoise will be skipped)"))
    return 0


# ------------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="narrate",
        description="The analysis and mastering steps behind the Voiceover studio.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""commands
  narrate doctor
  narrate analyze  demo.mov                      → demo.analysis.json + report
  narrate master   demo.mp4 -o demo.final.mp4    → video copied, audio mastered
""")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common_out(sp):
        sp.add_argument("-o", "--output")
        sp.add_argument("--outdir")

    def analysis_opts(sp):
        sp.add_argument("--model", default="small",
                        help="tiny|base|small|medium|large-v3|turbo (default small; "
                             "turbo is the best accuracy/speed trade on Apple Silicon)")
        sp.add_argument("--language", default="en")
        sp.add_argument("--engine", default="auto", choices=["auto", "mlx", "whisper"])
        sp.add_argument("--lexicon", help="brand/product terms, one per line")
        sp.add_argument("--no-verbatim", action="store_true",
                        help="do not prime the decoder for fillers. Whisper then "
                             "silently drops most of them (32 found instead of 80)")
        sp.add_argument("--gap-min", type=float, default=0.9,
                        help="report gaps longer than this (default 0.9s)")
        sp.add_argument("--low-conf", type=float, default=0.45)
        sp.add_argument("--gap-max", type=float, default=0.60,
                        help="collapse gaps longer than this (default 0.60s)")
        sp.add_argument("--gap-keep", type=float, default=0.30,
                        help="pause left behind after collapsing (default 0.30s)")
        sp.add_argument("--cut-fillers", action="store_true",
                        help="also excise um/uh (off by default — jump-cuts mid-sentence)")
        sp.add_argument("--cut-stutters", action="store_true",
                        help='also excise immediate repeats ("we, we click")')
        sp.add_argument("--tempo", type=float, default=1.0,
                        help="global speed-up applied to picture and sound together")

    def master_opts(sp):
        sp.add_argument("--lufs", type=float, default=-14.0, help="target loudness (YouTube: -14)")
        sp.add_argument("--true-peak", type=float, default=-1.5)
        # Wide on purpose. A narrow loudness range is a request to COMPRESS:
        # measured on a finished narration mix, LRA 11 cost 5.4 dB of
        # speech-to-floor ratio while LRA 30 cost 1.0, and both hit the
        # loudness target. The takes are levelled upstream already.
        sp.add_argument("--lra", type=float, default=30.0)
        sp.add_argument("--denoise", default="rnn", choices=["rnn", "fft", "none"],
                        help="rnn = arnndn/RNNoise (default, +16 dB SNR here); "
                             "fft = afftdn (near-useless on this material)")
        sp.add_argument("--denoise-model", default="mp", help="RNNoise weights: mp or sh")
        sp.add_argument("--denoise-mix", type=float, default=0.9,
                        help="1.0 removes all room; 0.9 leaves a trace so pauses "
                             "do not read as dropouts")
        sp.add_argument("--presence", type=float, default=1.5, help="3.2 kHz lift in dB")
        sp.add_argument("--compress", default="none", choices=["none", "gentle", "firm"],
                        help="dynamics. none keeps the delivery (default); "
                             "firm is punchier but reads as processed")
        sp.add_argument("--show-chain", action="store_true")

    sp = sub.add_parser("analyze", help="transcribe, measure, propose a cut")
    sp.add_argument("media"); common_out(sp); analysis_opts(sp); sp.set_defaults(fn=cmd_analyze)

    sp = sub.add_parser("redetect", help="re-run detectors on a stored transcript")
    sp.add_argument("analysis"); sp.add_argument("-o", "--output")
    sp.add_argument("--lexicon")
    sp.add_argument("--gap-min", type=float, default=0.9)
    sp.add_argument("--low-conf", type=float, default=0.45)
    sp.add_argument("--gap-max", type=float, default=0.60)
    sp.add_argument("--gap-keep", type=float, default=0.30)
    sp.add_argument("--cut-fillers", action="store_true")
    sp.add_argument("--cut-stutters", action="store_true")
    sp.add_argument("--tempo", type=float, default=1.0)
    sp.set_defaults(fn=cmd_redetect)

    sp = sub.add_parser("report", help="re-print the report for an analysis")
    sp.add_argument("analysis"); sp.set_defaults(fn=cmd_report)

    sp = sub.add_parser("master", help="spoken-word chain + R128 normalisation")
    sp.add_argument("media"); sp.add_argument("--single-pass", action="store_true")
    common_out(sp); master_opts(sp); sp.set_defaults(fn=cmd_master)

    sub.add_parser("doctor", help="check the environment").set_defaults(fn=cmd_doctor)
    return p


def main(argv=None) -> int:
    # stdout carries the reports, stderr the progress. Keep them interleaved in
    # the order they were written even when stdout is a pipe.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:
        pass
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
