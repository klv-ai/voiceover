"""Generate one line: the studio's synthesis worker.

Loads the model once and serves single lines, so changing a sentence and
hearing it again is a few seconds rather than a cold start. Each line is
generated, trimmed, toned, given its ending and paced here.

    ../.venv-tts/bin/python3 lab/synth_one.py --serve            # stdin/stdout JSON
    ../.venv-tts/bin/python3 lab/synth_one.py --text "..." -o out.wav --voice-ref ref.wav [--lora DIR]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import render_repair as R  # noqa: E402
import prosody              # noqa: E402
import psola                # noqa: E402
import tts_provider         # noqa: E402



def apply_tone(y, sr, pitch_st=0.0, warmth_db=0.0):
    """A small global pitch shift and a low shelf, via ffmpeg.

    NOT PSOLA. Per-frame contour manipulation was tried and rejected — it
    turned the voice robotic — so this deliberately does the dumbest thing that
    can work: resample the whole take (which shifts pitch and length together)
    then put the length back with a single atempo. Over a couple of semitones
    that is clean, and it cannot distort a contour because it does not touch
    one.

    Measured against the real voice, the takes sit 1.4 semitones LOW at the
    median while reaching 2.9 semitones HIGHER at the top — a wider, more
    top-skewed range than the speaker actually has. `warmth` is the more useful
    knob of the two: it adds body without moving any pitch at all.
    """
    if abs(pitch_st) < 0.01 and abs(warmth_db) < 0.01:
        return y
    import subprocess
    import tempfile
    chain = []
    if abs(pitch_st) >= 0.01:
        ratio = 2.0 ** (pitch_st / 12.0)
        chain += [f"asetrate={int(round(sr * ratio))}", f"aresample={sr}",
                  f"atempo={1.0 / ratio:.6f}"]
    if abs(warmth_db) >= 0.01:
        # 300 Hz shelf, gently sloped. A 200 Hz shelf with w=0.7 put nearly all
        # its lift BELOW 200 Hz (+6.4 dB at 70-120, but only +1.0 at 200-300),
        # and most speakers roll off below 150 Hz — so the boost went where it
        # could not be heard. At f=300:w=0.5 the same +6 delivers +5.5 at
        # 120-200 and +3.9 at 200-300, which is the chest register that
        # actually carries on a laptop.
        chain.append(f"bass=g={warmth_db:.2f}:f=300:w=0.5")
    a, b = tempfile.mktemp(suffix=".wav"), tempfile.mktemp(suffix=".wav")
    try:
        R.write_wav(a, y, sr)
        r = subprocess.run(["ffmpeg", "-v", "error", "-i", a, "-af", ",".join(chain),
                            "-ar", str(sr), "-c:a", "pcm_s16le", b, "-y"],
                           capture_output=True)
        if r.returncode != 0 or not os.path.exists(b):
            return y
        out, _ = R.read_wav(b)
        return out
    finally:
        for f in (a, b):
            try:
                os.unlink(f)
            except OSError:
                pass


def free_device():
    """Release cached accelerator memory between lines. Best effort: a failure
    here must never take down a worker that is otherwise fine."""
    try:
        import gc, torch
        gc.collect()
        if torch.backends.mps.is_available():
            torch.mps.empty_cache()
        elif torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def measure_terminal(y, sr):
    """How far the line ends above its own pitch, in semitones.

    Always measured, never conditional on wanting to fix it: the operator has
    to be able to SEE which lines end like a question before deciding to
    correct one. Hunting for them by ear across forty sentences is the job the
    tool exists to remove.
    """
    import numpy as np
    x16 = np.interp(np.linspace(0, len(y) - 1, int(len(y) * prosody.SR / sr)),
                    np.arange(len(y)), y).astype(np.float32)
    t, f0, db, ap = prosody.f0_track(x16)
    voiced = ~np.isnan(f0)
    if voiced.sum() < 8:
        return None, (t, f0, voiced)
    step = prosody.terminal_step(t, f0, float(t[voiced][0]), float(t[voiced][-1]))
    return (None if step != step else float(step)), (t, f0, voiced)


def fix_terminal(y, sr, fall_st=3.0, tail=0.45, trigger=0.4):
    """Bend a rising sentence-ending into a fall.

    The generator produces uptalk of its own: "…talking about MCP connections"
    lifts at the end and reads as a question. That is the same defect measured
    in the original recordings, and here it is cheap to correct because the
    audio is already synthetic — the objection to pitch-shifting a real voice
    (it stops sounding like the person) does not apply to a voice that was
    generated a second ago.

    Only the last `tail` seconds move, and only if the phrase actually rises.
    """
    import numpy as np
    step, (t, f0, voiced) = measure_terminal(y, sr)
    if step is None or step <= trigger:
        return y, step

    tv = t[voiced]
    st = prosody.semitones(f0[voiced])
    t_end = float(tv[-1])
    t0 = max(float(tv[0]), t_end - tail)
    sel = tv >= t0
    if sel.sum() < 3:
        return y, step
    start_st = float(st[sel][0])
    target = start_st - fall_st * (tv[sel] - t0) / max(t_end - t0, 1e-6)
    delta = np.clip(target - st[sel], -6.0, 6.0)
    ratio = np.ones(len(t), dtype=np.float64)
    idx = np.where(voiced)[0][sel]
    ratio[idx] = 2 ** (delta / 12.0)

    xs = np.concatenate(([0.0], t, [t[-1] + 1.0]))
    ys = np.concatenate(([1.0], ratio, [1.0]))
    at = lambda i: float(np.interp(i / sr, xs, ys, left=1.0, right=1.0))
    marks = psola.pitch_marks(y, sr, t, f0)
    return psola.shift(y, sr, marks, at), step


def _speaking_seconds(y, sr):
    """Seconds of actual speaking, pauses excluded.

    Gaps under 150 ms are counted as speech: a stop consonant is not a rest,
    and treating it as one makes every line look faster than it is.
    """
    import numpy as _np
    h = max(1, int(sr * 0.02))
    n = len(y) // h
    if n < 3:
        return len(y) / float(sr)
    f = _np.asarray(y[:n * h], dtype=_np.float32).reshape(-1, h)
    db = 20.0 * _np.log10(_np.sqrt((f ** 2).mean(axis=1)) + 1e-12)
    live = db > db.max() - 35
    hole, i = max(1, int(0.15 / 0.02)), 0
    while i < len(live):
        if not live[i]:
            j = i
            while j < len(live) and not live[j]:
                j += 1
            if j - i <= hole and 0 < i and j < len(live):
                live[i:j] = True
            i = j
        else:
            i += 1
    return float(live.sum()) * 0.02


def synth(text, voice_ref, out, *, pitch_st=0.0, warmth_db=0.0,
          target_wpm=210.0, seed=None, terminal="none",
          provider="voxcpm", voice_opts=None):
    y = tts_provider.generate(text, voice_ref, provider, seed=seed, **(voice_opts or {}))

    # A fraction-of-PEAK threshold is a bad silence detector for speech: the
    # quiet parts of a phrase sit 30-40 dB under its loudest moment. That was
    # already learned on the TAIL (2% of peak cut 150ms of a word's release and
    # the line "just ended"), and the HEAD had the same fault the other way
    # round: a word-initial stop burst is far below the phrase peak, so 2% with
    # only 25ms of lookback started the audio at the vowel - "Today" arrived as
    # "oday". Measured: 3 of 6 takes began ABOVE the tail threshold, i.e. the
    # trim had already cut into real content.
    env = np.abs(y)
    peak = env.max() or 1.0
    head = np.where(env > peak * 0.01)[0]
    tail = np.where(env > peak * 0.004)[0]
    if len(head) and len(tail):
        y = y[max(0, head[0] - int(0.080 * R.SR)):min(len(y), tail[-1] + int(0.080 * R.SR))]

    y = apply_tone(y, R.SR, pitch_st, warmth_db)

    # measure always; correct only when asked and only when it actually rises
    if terminal == "fall":
        y, step = fix_terminal(y, R.SR)
    else:
        step, _ = measure_terminal(y, R.SR)

    if target_wpm and target_wpm > 0:
        # Pace by holding or clipping the beats between phrases FIRST, and only
        # then, for whatever is left over, by the uniform stretch. Doing it the
        # other way round — the way it was — squeezes the punctuation by the
        # same factor as the words, so a line brought up to the operator's own
        # rate loses the spaces that made it sound spoken rather than read.
        # PACE THE SPEAKING, NOT THE LINE.
        #
        # Words per minute over a whole line counts its pauses as if they were
        # words, and the ear does not. Measured on two lines of ten words each:
        # one scored 174 wpm and was heard as far too fast, the other scored
        # 180 and was heard as right — because the first held half a second of
        # silence and was therefore ARTICULATING at 205 wpm while the second
        # ran straight through at 180. Regulating the gross figure makes the
        # pacing wander exactly as described: fast, then normal, then fast,
        # then slow, with the numbers on screen insisting nothing is wrong.
        #
        # So work out how long the SPEAKING should take, and scale the take by
        # what it would need. Pauses scale with it, which is what happens when
        # a person slows down.
        words = len(text.split())
        want_speak = words / float(target_wpm) * 60.0
        dur_now = len(y) / R.SR
        pause_now = sum((b - a) for a, b in R._quiet_runs(y, R.SR)) / R.SR
        speak_now = max(0.2, dur_now - pause_now)
        want = dur_now * (want_speak / speak_now)
        # What that target means as a gross figure, so `slow_to_rate` — which
        # works in words per minute over the whole take — asks for the same
        # thing.
        target_wpm = words / max(want, 0.2) * 60.0
        y, moved = R.fit_by_pauses(y, R.SR, want, seed=int(seed or 0))
        # How much room the pauses had. A short line has almost none — six
        # words, no clause break, nowhere to give a beat back — so pacing it
        # falls entirely to the stretch, and an 8% stretch cannot close a gap
        # of 90 wpm. Measured on a six-word line read at 205 wpm: the model
        # produced 107 and the pass left it there, half the operator's rate,
        # which is the cluster of lines that "drag".
        #
        # Allow a bigger SPEED-UP in that case only. Speeding a short line up
        # is far less audible than drawling one out, and the alternative is a
        # line that plainly is not how he speaks.
        # Judge it on WHAT IS LEFT, not on the shape of the line. Pauses take
        # up whatever slack they can; if the take is still well over its
        # target afterwards there is nothing else to give, and an 8% ceiling
        # simply abandons it there — two lines in this cluster stayed 25%
        # slower than the operator reads them even though both had pauses to
        # trim. The wider ceiling only ever SPEEDS UP.
        behind = (len(y) / R.SR) / max(want, 1e-6)
        # ...and the same allowance in the other direction, for a line that
        # arrives RUSHED.
        #
        # The ceiling was widened because short lines came out slow and an 8%
        # squeeze could not reach them. Short lines come out FAST just as
        # often — three words generated in 0.60s is 300 wpm against a 210
        # target — and a 6% floor leaves that at 281, which is heard, exactly
        # as reported, as one block being suddenly far too quick. The reason
        # to keep the floor tight is that drawling is worse than rushing, and
        # that reason does not apply to a take that is nowhere near its
        # target: 0.80 on a line running 20% fast lands it at its own pace,
        # not below it.
        # REVERTED. The floor was widened to 0.80 for a take arriving fast,
        # by the same argument that widened the ceiling: a line nowhere near
        # its target should be allowed to reach it. The argument is wrong, and
        # listening is what says so — "Our goal is to keep the human in the
        # loop" generated at 283 wpm and was stretched 24% to reach 227, which
        # is heard as a drawl AND as the voice itself going strange, because a
        # quarter is far past what time-stretching survives.
        #
        # 0.94 is what shipped before and what sounded right. A take that far
        # ahead of its target does not need a bigger stretch; it needs to be
        # generated again, which is a change to make deliberately rather than
        # by loosening a bound.
        y, _, _ = R.slow_to_rate(y, R.SR, len(text.split()), target_wpm=float(target_wpm),
                                 floor_tempo=0.94,
                                 ceil_tempo=1.25 if behind > 1.12 else 1.08)

    # A clipped take cannot be repaired later — scaling squared-off tops just
    # makes them quieter — so the guard belongs here, before it is written.
    peak = float(np.abs(y).max()) if len(y) else 0.0
    if peak > 0.89:
        y = y * (0.89 / peak)
    R.write_wav(out, y, R.SR)
    # Which processor made this. Carried on every take so a render that has
    # quietly dropped to the CPU says so in the job log instead of only in how
    # long it takes.
    return {"path": out, "duration": round(len(y) / R.SR, 3),
            "words": len(text.split()),
            "device": tts_provider._device(),
            "terminal_step": None if step is None else round(float(step), 2),
            # Two rates, because one of them has been lying.
            #
            # `wpm` counts a line's pauses as though they were words, and the
            # ear does not: a three-word line half of which is silence scores
            # 154 while ARTICULATING at 300, and reads on screen as the
            # slowest line in the video when it is the fastest. The operator
            # spotted it from the numbers disagreeing with what he heard.
            #
            # `speech_wpm` is the rate the words actually come out at, which
            # is both what is heard and what the pacing above regulates. The
            # gross figure is kept because block timing is a real constraint —
            # a take still has to fit its slot.
            "speech_wpm": round(len(text.split())
                                / max(_speaking_seconds(y, R.SR), 1e-6) * 60),
            "wpm": round(len(text.split()) / max(len(y) / R.SR, 1e-6) * 60)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--serve", action="store_true")
    ap.add_argument("--text"); ap.add_argument("-o", "--output")
    ap.add_argument("--voice-ref", default="")
    ap.add_argument("--target-wpm", type=float, default=0.0)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--terminal", default="none", choices=["none", "fall"])
    ap.add_argument("--prompt-text", help="what the reference says, if not in its .txt")
    ap.add_argument("--lora", help="a LoRA checkpoint directory to narrate with")
    ap.add_argument("--direction", help="how to deliver the line, e.g. 'warm, unhurried'")
    a = ap.parse_args()

    if a.serve:
        # one line of JSON in, one line of JSON out; the model stays loaded
        print(json.dumps({"ready": True}), flush=True)
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            # Echo the caller's id on every reply, success or failure. Without
            # it the protocol is positional, and ONE lost or extra line offsets
            # every later response by one for the life of the worker - the
            # caller then waits forever for a take that was already delivered
            # to the wrong promise.
            rid = None
            try:
                req = json.loads(line)
                rid = req.get("id")
                out = synth(req["text"], req["voice_ref"], req["output"],
                            target_wpm=req.get("target_wpm", 210.0),
                            seed=req.get("seed"),
                            terminal=req.get("terminal", "none"),
                            pitch_st=float(req.get("pitch_st") or 0.0),
                            warmth_db=float(req.get("warmth_db") or 0.0),
                            provider=req.get("provider") or "voxcpm",
                            voice_opts=req.get("voice_opts"))
                out["id"] = rid
                print(json.dumps(out), flush=True)
            except Exception as e:
                print(json.dumps({"id": rid, "error": str(e)}), flush=True)
            finally:
                # Hand the Metal allocator its buffers back after EVERY line.
                #
                # Nothing here frees them otherwise: torch caches MPS blocks
                # for reuse, generation lengths vary, and the cache fragments a
                # little more each time. The worker survives roughly twenty
                # lines and then stalls outright - 0% CPU, no output, no error
                # - which is the "stopped responding" the batch reports from
                # around block 20 onward, always at a different line because it
                # depends on how much was allocated before it, not on the text.
                free_device()
        return

    opts = {k: v for k, v in (("prompt_text", a.prompt_text), ("lora", a.lora),
                              ("direction", a.direction)) if v}
    print(json.dumps(synth(a.text, a.voice_ref, a.output, target_wpm=a.target_wpm,
                           seed=a.seed, terminal=a.terminal, voice_opts=opts)))


if __name__ == "__main__":
    main()
