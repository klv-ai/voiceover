"""Where a generated line comes from: VoxCPM, locally.

Everything the studio does to a line after it exists — trimming the head and
tail, the tone shelf, the terminal fall, the pacing — is independent of how
the audio was made, and lives in synth_one.py. This module only generates.

The speaker comes from two places, either or both:

    a LoRA fine-tune    the voice LEARNED from the speaker's reads, so it is
                        the same person on every line by construction
    a sample + its text "ultimate cloning": a few seconds of the real
                        recording the model continues from, carrying the
                        accent, the microphone distance and the room

A style direction switches to timbre-only cloning (VoxCPM refuses a direction
alongside a reference transcript), and no sample at all narrates from the
fine-tune alone.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

import numpy as np

SR = 48000

# Said once per process: a GPU build of torch that can see no GPU.
_warned_cpu = False


def _device() -> str:
    """Where the model should run — and a loud complaint when that is not
    where it ought to be.

    A GPU build of torch that can see no GPU is a BROKEN machine, not a CPU
    machine, and the difference is ten times the cost of a render. That failed
    silently once: an unattended kernel upgrade left the box without an
    `amdgpu` module for the kernel it booted, `/dev/kfd` never appeared,
    `torch.cuda.is_available()` went False, and every line was generated on the
    processor at thirty-five seconds apiece instead of three. Nothing in the
    pipeline noticed, through a whole render and three separate timing
    measurements — all of which timed the generation and none of which asked
    what was underneath it. The operator caught it by listening for the card's
    fans.

    A fallback that is chosen is fine. A fallback that is suffered should be
    impossible to miss.
    """
    global _warned_cpu
    import torch
    if torch.cuda.is_available():      # ROCm answers to this too
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    hip = getattr(torch.version, "hip", None)
    built_for = hip or torch.version.cuda
    if built_for and not _warned_cpu:
        _warned_cpu = True
        print(f"WARNING: torch is built for the GPU "
              f"({'ROCm' if hip else 'CUDA'} {built_for}) but can see no "
              f"device, so this is generating on the CPU — roughly ten times "
              f"slower. On this box that has meant the amdgpu module missing "
              f"after a kernel upgrade: check `ls /dev/kfd`, `modinfo amdgpu`, "
              f"and `apt install linux-modules-extra-$(uname -r)`.",
              file=sys.stderr, flush=True)
    return "cpu"


_vox = None
_vox_lora = None


def _reference_text(wav: str | None) -> str | None:
    """What the reference clip SAYS.

    VoxCPM refuses a reference without its transcript — it aligns the clone
    against the words, where Chatterbox takes the audio alone. That is a real
    difference and arguably a better design, but it means a reference built by
    concatenating takes arrives without the one thing this model needs.

    So transcribe it once and keep the result next to the wav. The corpus
    knows these words already; deriving them here means any reference works,
    including one the operator drops in by hand.
    """
    if not wav or not os.path.exists(wav):
        return None
    side = os.path.splitext(wav)[0] + ".txt"
    if os.path.exists(side):
        text = open(side).read().strip()
        if text:
            return text
    try:
        import whisper_any
        text = whisper_any.transcribe(wav)["text"].strip()
    except Exception:
        return None
    if text:
        try:
            with open(side, "w") as fh:
                fh.write(text)
        except OSError:
            pass
    return text or None


# The sample's clicks come out before the model hears it; see scrub.py.
if os.path.dirname(os.path.abspath(__file__)) not in sys.path:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scrub import scrubbed as _scrubbed_reference  # noqa: E402


def _denoised_reference(model, wav: str) -> str:
    """The reference with its room taken out — done ONCE, not per line.

    Asking the model to denoise the prompt on every call costs more than the
    generation: 67 seconds a line against 34 with it off, and the prompt is
    the same file every time. Over a hundred-and-forty-three line render that
    is an hour spent cleaning one clip again and again.

    So clean it once, keep the result beside the original, and hand the model
    audio it has no reason to denoise.
    """
    side = os.path.splitext(wav)[0] + ".denoised.wav"
    if os.path.exists(side) and os.path.getmtime(side) >= os.path.getmtime(wav):
        return side
    den = getattr(model, "denoiser", None)
    if den is None:
        return wav
    try:
        den.enhance(wav, output_path=side)
    except Exception:
        return wav
    return side if os.path.exists(side) else wav


def direction_of(o: dict) -> str:
    return (o.get("direction") or "").strip()


def _voxcpm(text: str, voice: str, o: dict):
    """VoxCPM — a second local clone, kept behind the same seam.

    Here to be compared, not assumed better. Every problem still open in this
    pipeline is something the generator does: it collapses to silence, it keeps
    talking after the sentence, it invents a different room on every line. None
    of those are fixable by rearranging how a model is called — measured, and
    twice — so the remaining question is whether a different model has the same
    faults. The harnesses that answer it already exist.

    Takes the same reference wav as the local clone, so a comparison changes
    one thing.
    """
    prompt_text = o.get("prompt_text") or _reference_text(voice)
    # A reference with no transcript is FATAL here, never a degraded mode.
    #
    # VoxCPM aligns the clone against the words of the prompt. Hand it audio
    # and no text and it does not fail — it makes up what the audio must have
    # said and reads that out before the line. A whole render came back with
    # every take opening "the CIEWS" because the transcript step had hit HIP
    # out-of-memory (the synth worker holds 14.6 of this card's 16 GiB, so
    # loading Whisper alongside it is a coin toss) and the exception was
    # swallowed into a None. Stop instead: one named failure is recoverable,
    # a hundred and forty-one quietly wrong takes are not.
    if voice and not prompt_text:
        raise RuntimeError(
            f"no transcript for the reference {voice!r} — VoxCPM needs the "
            f"words of its prompt, and generating without them produces takes "
            f"that read the prompt aloud. Transcribe it once with nothing else "
            f"on the GPU and cache it beside the wav as .txt.")

    # Scrub the sample's clicks before the model hears it. After the transcript
    # lookup, which reads the .txt beside the ORIGINAL; the scrubbed copy says
    # the same words. On by default — `scrub: False` hands over the raw sample.
    if voice and o.get("scrub", True):
        voice = _scrubbed_reference(voice)

    # A fine-tuned adapter, if one is asked for.
    #
    # Held as part of the model's identity rather than applied per call: the
    # weights are loaded at construction, so asking for a different adapter
    # means building a different model. Comparing two checkpoints is the whole
    # point of having them, and a comparison that silently kept the first one
    # loaded would be worse than no comparison at all.
    want_lora = os.path.abspath(o["lora"]) if o.get("lora") else None

    global _vox, _vox_lora
    if _vox is not None and want_lora != _vox_lora:
        # Let go of the old one properly. Dropping the reference is not enough
        # on its own: torch's caching allocator holds the blocks, so swapping
        # checkpoint after checkpoint - which is exactly what scoring does -
        # leaves a 2B model's worth of card reserved each time.
        _vox = None
        try:
            import gc
            import torch
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass
    if _vox is None:
        from voxcpm import VoxCPM
        base = o.get("model") or "openbmb/VoxCPM2"
        if want_lora:
            import json as _json
            cfg_path = os.path.join(want_lora, "lora_config.json")
            if not os.path.exists(cfg_path):
                raise RuntimeError(
                    f"{want_lora} is not a LoRA checkpoint - no lora_config.json")
            cfg = _json.load(open(cfg_path))
            # VoxCPM2 and VoxCPM1 each define their own LoRAConfig; the
            # checkpoint does not say which, so take whichever accepts it.
            try:
                from voxcpm.model.voxcpm2 import LoRAConfig
            except ImportError:
                from voxcpm.model.voxcpm import LoRAConfig
            _vox = VoxCPM.from_pretrained(
                hf_model_id=cfg.get("base_model") or base,
                load_denoiser=False, optimize=True,
                lora_config=LoRAConfig(**(cfg.get("lora_config") or {})),
                lora_weights_path=want_lora)
        else:
            _vox = VoxCPM.from_pretrained(base, device=_device())
        _vox_lora = want_lora
    seed = o.get("seed")
    if seed is not None:
        import torch
        torch.manual_seed(int(seed))
    # DEFAULT OFF, and this is the single biggest quality lever measured here.
    #
    # The zipenhancer takes 21 dB of floor out of the reference (-58.9 to
    # -79.7), leaving a prompt no microphone ever produced. The model copies
    # what it is given, so most lines then come back at digital silence - and
    # roughly one in eight, having no real floor to imitate, invents one.
    # Measured over eight lines, same seeds, one flag apart:
    #
    #     denoised prompt   floor -100 dB, spread 25.5 dB  (one line at -75)
    #     raw prompt        floor  -62 dB, spread  2.4 dB
    #
    # That spread IS the "random bursts of background noise" and the room that
    # changes between lines. A clean prompt is not the goal; a CONSISTENT one
    # is, and the operator's own recording already separates speech from floor
    # by 40 dB.
    want_clean = bool(o.get("denoise", False))
    prompt = voice or None
    if prompt and want_clean:
        prompt = _denoised_reference(_vox, prompt)

    # Two cloning modes, and they are mutually exclusive.
    #
    # prompt_wav + prompt_text is "ultimate cloning": the model continues from
    # the reference and reproduces every nuance of it, which is why this
    # project uses it. It cannot take a style instruction — VoxCPM refuses the
    # combination outright.
    #
    # reference_wav is "controllable cloning": timbre only, and a parenthesised
    # instruction at the front of the text steers emotion, pace and expression.
    # Asking for a direction therefore means giving up the closer clone, so it
    # only happens when a direction is actually asked for.
    # NO REFERENCE AT ALL, when the voice is supposed to live in the weights.
    #
    # Every mode below conditions on a clip, so the voice and its room are
    # re-derived on every line - which is what a video full of rooms changing
    # and the voice switching actually is. A fine-tune is the alternative: if
    # it has learned the speaker, the speaker comes from the weights and every
    # line is the same person in the same place by construction.
    #
    # It also unlocks direction, which VoxCPM refuses only alongside a
    # reference transcript.
    if o.get("no_prompt"):
        wav = _vox.generate(
            text=f"({direction_of(o)}){text}" if direction_of(o) else text,
            cfg_value=float(o.get("cfg_value", 2.0)),
            inference_timesteps=int(o.get("timesteps", 10)),
            normalize=bool(o.get("normalize", True)),
            retry_badcase=bool(o.get("retry_badcase", True)),
        )
        y = np.asarray(wav, dtype=np.float32).reshape(-1)
        sr = getattr(_vox, "sample_rate", None) or getattr(
            getattr(_vox, "tts_model", None), "sample_rate", None) or 16000
        return y, int(o.get("vox_sr") or sr)

    direction = (o.get("direction") or "").strip()
    if direction:
        wav = _vox.generate(
            # The instruction is a parenthesised prefix on the text, which the
            # model reads as direction and does not speak.
            text=f"({direction}){text}",
            reference_wav_path=voice,
            cfg_value=float(o.get("cfg_value", 2.0)),
            inference_timesteps=int(o.get("timesteps", 10)),
            normalize=bool(o.get("normalize", True)),
            denoise=False,
            retry_badcase=bool(o.get("retry_badcase", True)),
        )
        y = np.asarray(wav, dtype=np.float32).reshape(-1)
        sr = getattr(_vox, "sample_rate", None) or getattr(
            getattr(_vox, "tts_model", None), "sample_rate", None) or 16000
        return y, int(o.get("vox_sr") or sr)

    wav = _vox.generate(
        text=text,
        prompt_wav_path=prompt,
        prompt_text=prompt_text,
        cfg_value=float(o.get("cfg_value", 2.0)),
        inference_timesteps=int(o.get("timesteps", 10)),
        normalize=bool(o.get("normalize", True)),
        # Always False: the prompt handed over above has already been cleaned
        # once and cached. Leaving this True would redo that work on every
        # single line, which doubles the cost of a render.
        denoise=False,
        # It retries its own bad cases. Chatterbox does not, which is why a
        # third of its generations could come back as silence and we had to
        # build the re-roll ourselves.
        retry_badcase=bool(o.get("retry_badcase", True)),
    )
    y = np.asarray(wav, dtype=np.float32).reshape(-1)
    sr = getattr(_vox, "sample_rate", None) or getattr(
        getattr(_vox, "tts_model", None), "sample_rate", None) or 16000
    return y, int(o.get("vox_sr") or sr)


PROVIDERS = {"voxcpm": _voxcpm}


def generate(text: str, voice: str, provider: str = "voxcpm", **o):
    """One line of audio, at the pipeline's sample rate.

    Returns float32 mono at 48 kHz whatever the provider works in, so nothing
    downstream has to ask where the line came from.
    """
    fn = PROVIDERS.get(provider or "voxcpm")
    if fn is None:
        raise RuntimeError(f"no such voice provider: {provider}")
    y, rate = fn(text, voice, o)
    if rate == SR:
        return y
    a, b = tempfile.mktemp(suffix=".wav"), tempfile.mktemp(suffix=".wav")
    try:
        import wave
        with wave.open(a, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(int(rate))
            w.writeframes((np.clip(y, -1, 1) * 32767).astype(np.int16).tobytes())
        subprocess.run(["ffmpeg", "-v", "error", "-i", a, "-ar", str(SR),
                        "-c:a", "pcm_s16le", b, "-y"], check=True, capture_output=True)
        with wave.open(b) as w:
            out = np.frombuffer(w.readframes(w.getnframes()),
                                dtype=np.int16).astype(np.float32) / 32768.0
        return out
    finally:
        for f in (a, b):
            try:
                os.unlink(f)
            except OSError:
                pass
