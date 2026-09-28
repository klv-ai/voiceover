"""Whisper transcription with word-level timestamps.

Two engines, auto-detected:
  * mlx_whisper  — 4-5x faster on Apple Silicon; used via its Python API, which
                   mirrors openai-whisper's signature exactly.
  * whisper      — the openai-whisper CLI, the universal fallback.

Both are fed a lexicon as `initial_prompt`. That matters more than it looks:
without it Whisper renders a product called "Fablie" as "Fably" and one
called "Tabl" as "table",
which then poisons every downstream confidence score on your own brand terms.
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import sys

_PKG = os.path.dirname(os.path.abspath(__file__))
_VENV = os.path.join(os.path.dirname(_PKG), ".venv", "bin", "python3")

from .util import die, info, ok, run, scratch, warn, have

# Whisper truncates initial_prompt at 224 tokens; stay well inside that.
_PROMPT_CHARS = 700

# mlx-community's repo names are not derivable from the openai model name —
# some carry an "-mlx" suffix and turbo does not. Map them explicitly.
MLX_MODELS = {
    "tiny": "mlx-community/whisper-tiny-mlx",
    "base": "mlx-community/whisper-base-mlx",
    "small": "mlx-community/whisper-small-mlx",
    "medium": "mlx-community/whisper-medium-mlx",
    "large": "mlx-community/whisper-large-v3-mlx",
    "large-v3": "mlx-community/whisper-large-v3-mlx",
    "turbo": "mlx-community/whisper-large-v3-turbo",
    "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
}

# The project-local venv first: `pip install mlx-whisper` there keeps the fast
# path available without touching the system interpreter.
_INTERPRETERS = [
    _VENV, sys.executable, "python3", "python3.13", "python3.12", "python3.11",
    "/opt/homebrew/bin/python3", "/opt/homebrew/opt/python@3.11/bin/python3.11",
]


# Whisper is trained to produce clean, readable text, so by default it drops
# most disfluency: on opportunities.mov the standard prompt reported 32 hard
# fillers where a verbatim-primed prompt found 80, and the 50 it had dropped
# measured at a median of -17.7 dBFS, 0.26s long, at 0.96 confidence — real
# audible "um"s, silently deleted from the transcript. Since finding disfluency
# is the whole point here, prime for it.
VERBATIM_PREAMBLE = ("Verbatim transcript with every filler kept: um, uh, er, hmm. ")


def load_lexicon(path: str | None, verbatim: bool = True) -> str:
    """Build the initial_prompt: disfluency priming plus the brand lexicon."""
    prompt = VERBATIM_PREAMBLE if verbatim else ""
    terms = []
    if path and os.path.exists(path):
        terms = [ln.strip() for ln in open(path)
                 if ln.strip() and not ln.lstrip().startswith("#")]
    if terms:
        prompt += "Terminology: " + ", ".join(terms) + "."
    return prompt[:_PROMPT_CHARS]


def _mlx_interpreter() -> str | None:
    for exe in _INTERPRETERS:
        if not exe:
            continue
        path = shutil.which(exe) if not os.path.isabs(exe) else (exe if os.path.exists(exe) else None)
        if not path:
            continue
        p = run([path, "-c", "import mlx_whisper"], check=False)
        if p.returncode == 0:
            return path
    return None


def _via_mlx(exe: str, wav: str, model: str, language: str, prompt: str) -> dict:
    out = scratch(".json")
    script = (
        "import json,sys,mlx_whisper\n"
        "r = mlx_whisper.transcribe(sys.argv[1], path_or_hf_repo=sys.argv[2],\n"
        "    language=(sys.argv[3] or None), word_timestamps=True,\n"
        "    initial_prompt=(sys.argv[4] or None), verbose=False)\n"
        "json.dump(r, open(sys.argv[5], 'w'))\n"
    )
    run([exe, "-c", script, wav, model, language, prompt, out], capture=False)
    with open(out) as f:
        data = json.load(f)
    os.unlink(out)
    return data


def _via_cli(wav: str, model: str, language: str, prompt: str) -> dict:
    outdir = os.path.dirname(wav) or "."
    cmd = ["whisper", wav, "--model", model, "--word_timestamps", "True",
           "--output_format", "json", "--output_dir", outdir, "--verbose", "False"]
    if language:
        cmd += ["--language", language]
    if prompt:
        cmd += ["--initial_prompt", prompt]
    run(cmd, capture=False)
    stem = os.path.splitext(os.path.basename(wav))[0]
    hits = glob.glob(os.path.join(outdir, stem + ".json"))
    if not hits:
        die("whisper produced no JSON output")
    with open(hits[0]) as f:
        data = json.load(f)
    os.unlink(hits[0])
    return data


def transcribe(wav: str, *, model: str = "small", language: str = "en",
               lexicon: str | None = None, engine: str = "auto",
               verbatim: bool = True) -> dict:
    """Return a normalised transcript: flat word list + segments."""
    prompt = load_lexicon(lexicon, verbatim=verbatim)
    if prompt:
        info(f"prompt: {'verbatim priming + ' if verbatim else ''}"
             f"{len(prompt.split(','))} terms primed into the decoder")

    exe = _mlx_interpreter() if engine in ("auto", "mlx") else None
    if engine == "mlx" and not exe:
        die("engine 'mlx' requested but mlx_whisper is not importable by any python3 found.\n"
            "  install with:  python3 -m pip install mlx-whisper")

    if exe:
        mdl = model if "/" in model else MLX_MODELS.get(model)
        if not mdl:
            die(f"no mlx model mapped for {model!r}. Known: "
                + ", ".join(sorted(MLX_MODELS)) + " (or pass a full HF repo id)")
        info(f"engine: mlx_whisper ({mdl}) via {exe}")
        raw = _via_mlx(exe, wav, mdl, language, prompt)
    else:
        if not have("whisper"):
            die("no whisper found. install one of:\n"
                "  python3 -m pip install mlx-whisper      (fast, Apple Silicon)\n"
                "  python3 -m pip install openai-whisper    (portable)")
        if engine == "auto":
            warn("mlx_whisper not installed — falling back to CPU whisper (much slower).")
            warn("for 48 min of video, consider: python3 -m pip install mlx-whisper")
        info(f"engine: openai-whisper CLI ({model})")
        raw = _via_cli(wav, model, language, prompt)

    words, segments = [], []
    for seg in raw.get("segments", []):
        segments.append({
            "start": round(float(seg.get("start", 0.0)), 3),
            "end": round(float(seg.get("end", 0.0)), 3),
            "text": (seg.get("text") or "").strip(),
        })
        for w in seg.get("words", []) or []:
            token = (w.get("word") or w.get("text") or "").strip()
            if not token:
                continue
            words.append({
                "w": token,
                "start": round(float(w.get("start", 0.0)), 3),
                "end": round(float(w.get("end", 0.0)), 3),
                "p": round(float(w.get("probability", w.get("confidence", 1.0))), 3),
            })

    if not words:
        die("transcript has no word-level timestamps — the whisper build may be too old.")

    ok(f"{len(words)} words across {len(segments)} segments")
    return {
        "engine": "mlx_whisper" if exe else "openai-whisper",
        "model": model,
        "language": raw.get("language", language),
        "lexicon_prompt": prompt,
        "words": words,
        "segments": segments,
    }
