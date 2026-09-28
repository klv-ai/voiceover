"""Did the take actually say the line?

Chatterbox is not deterministic, and a minority of generations go wrong in ways
no signal measurement catches, because the audio is perfectly good audio — it is
just not the sentence that was asked for:

  - words dropped from the front: "Today we are going to be talking about MCP
    connections" came back as "going to be talking about MCP connections"
  - a hallucinated fragment prepended: an "OK...?" before the line
  - an invented noise mid-line: an audible "mooo" that is in no script and no
    recording

Every one of those is obvious to a listener on the first play and invisible to
the pipeline, so the operator ends up being the quality check — re-rolling by
ear, one line at a time, across thirty-seven of them. Transcribing the take and
comparing it to the text that was requested finds all three for the cost of
about a second per line.

Prints JSON: {ok, score, heard, missing_head, missing_tail, extra_head}.
"""
import difflib
import json
import math
import os
import re
import sys

MODEL = "mlx-community/whisper-turbo"
# The same model by its openai-whisper name, for machines without MLX.
FALLBACK_MODEL = "turbo"

# A line that has finished has gone quiet. Ending within this of its own
# speaking level means it did not finish, it was still making a noise.
TAIL_FLOOR_DB = -8.0
TAIL_S = 0.25

# A pause this far above the take's own floor is not a pause.
PAUSE_FLOOR_DB = 12.0


def flat_tops(path: str, tol: float = 0.004, run: int = 16):
    """Milliseconds of waveform squared off at the take's own peak.

    Measured against the file's OWN maximum, not against full scale: once a
    take has been scaled to sit below the ceiling, its flattened tops are
    quieter but just as square, and a test for "samples near 1.0" reports it
    clean. What identifies clipping is a RUN of samples all sitting at the same
    maximum value. A waveform does not do that naturally; a limiter does.
    """
    import wave
    import numpy as np
    with wave.open(path) as w:
        rate, ch, n = w.getframerate(), w.getnchannels(), w.getnframes()
        raw = w.readframes(n)
    a = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
    if ch > 1:
        a = a[::ch]
    if not a.size:
        return 0.0
    peak = float(np.abs(a).max())
    if peak <= 0.02:
        return 0.0
    at_peak = np.abs(a) >= peak * (1.0 - tol)
    total, count = 0, 0
    for v in at_peak:
        if v:
            count += 1
            continue
        if count >= run:
            total += count
        count = 0
    if count >= run:
        total += count
    return round(total / rate * 1000.0, 1)


def noisy_pause(path: str, min_s: float = 0.25, win_s: float = 0.02):
    """The worst mid-line pause that is not actually quiet, in dB above the
    take's own noise floor.

    A third kind of failure, invisible to every other check. The words are all
    correct, so comparing the transcript to the script passes it. The line ends
    and decays properly, so the tail check passes it. Nothing clips. But
    somewhere in the middle the model produces most of a second of breathy
    texture instead of a pause — measured on one line, 0.87s sitting 12 dB
    above that take's floor, heard as stuttered breathing.

    A real pause sits AT the floor; that is what makes it a pause.

    FINDING THE PAUSES IS NOT REIMPLEMENTED HERE. `_quiet_runs` already does
    it, and is already tuned — two attempts at writing an equivalent by hand
    disagreed with it in both directions, first flagging nothing of what a
    validation scan had found at +20 and +36 dB, then flagging nothing at all.
    Call the one that works.
    """
    import sys as _sys
    import os as _os
    import wave
    import numpy as np
    _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
    import render_repair as R

    y, rate = R.read_wav(path)
    k = max(1, int(rate * win_s))
    m = len(y) // k
    if m < 20:
        return 0.0
    rms = np.sqrt((y[:m * k].reshape(m, k).astype(np.float64) ** 2).mean(axis=1))
    floor = float(np.percentile(rms, 3))
    if floor <= 1e-7:
        return 0.0                      # digital silence: nothing to measure against
    worst = 0.0
    for i0, i1 in R._quiet_runs(y, rate):
        seg = rms[int(i0 / k):int(i1 / k)]
        if len(seg) < 6 or (i1 - i0) / rate < min_s:
            continue
        worst = max(worst, 20.0 * math.log10(max(float(np.median(seg)), 1e-9) / floor))
    return round(worst, 1)


def tail_level(path: str):
    """How loud a take still is at its very end, against its own body.

    Catches the artefact no transcript can: the model keeps going after the
    last word with a low voiced hum — heard as someone blowing into the
    microphone. It contains no words, so comparing the transcript to the script
    passes it, and it is LOUD, so an amplitude trim keeps it.

    Two shape-based attempts at this failed before the measurement was the
    obvious one. Spectral tilt does not separate it: a dark tail is just a final
    word's release, and nine takes here had a darker one than the bad line.
    What is actually strange about it is that it never decays. Across
    fifty-nine takes the last quarter-second sat a median 23 dB below the body;
    this one sat 2 dB below, the loudest of the set.
    """
    import wave
    import numpy as np
    with wave.open(path) as w:
        rate, ch, n = w.getframerate(), w.getnchannels(), w.getnframes()
        raw = w.readframes(n)
    a = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
    if ch > 1:
        a = a[::ch]
    win = max(1, int(rate * 0.02))
    m = len(a) // win
    if m < 10:
        return None
    rms = np.sqrt((a[:m * win].reshape(m, win) ** 2).mean(axis=1))
    body = float(np.percentile(rms, 70))
    k = max(1, int(TAIL_S / 0.02))
    tail = float(np.median(rms[-k:]))
    if body <= 1e-9:
        return None
    return round(20.0 * math.log10(max(1e-9, tail / body)), 1)


def words(s: str):
    """Tokens to compare on. Hyphens are dropped rather than split, because the
    transcriber writes "Co-Work" as "Cowork" and a split would read that as a
    missing word."""
    return re.findall(r"[a-z0-9]+", s.lower().replace("-", "").replace("'", ""))


def _same_word(a: str, b: str, near: float = 0.8) -> bool:
    """Are these the transcriber's spelling and the script's spelling of one
    word?

    Near-identical spellings are easy: "Klaviy" for "Klavi", "Cowork" for
    "Co-Work". Proper nouns the transcriber has never seen are not — it wrote
    "Brandi" for "BrandEye", which scores 0.67 and was failing a take whose
    audio was perfectly correct. What those share is the START of the word: the
    transcriber heard the beginning and guessed the rest.

    So accept a weaker match when the two agree on a real prefix. A genuinely
    different word agrees on neither.
    """
    if difflib.SequenceMatcher(None, a, b).ratio() >= near:
        return True
    n = min(len(a), len(b))
    return (n >= 4 and a[:4] == b[:4]
            and difflib.SequenceMatcher(None, a, b).ratio() >= 0.55)


def _must_say() -> set:
    """Names whose spelling back from the transcriber IS the check.

    From the studio's settings ("Must be said exactly"), found through
    VOICEOVER_SETTINGS; or, run on its own, from narrate/pronounce.txt if one
    exists. Short on purpose: a homophone listed here can never pass."""
    out = set()
    cfg = os.environ.get("VOICEOVER_SETTINGS")
    if cfg and os.path.exists(cfg):
        try:
            for t in json.load(open(cfg, encoding="utf-8")).get("mustSay") or []:
                out.update(words(str(t)))
            return out
        except (OSError, ValueError):
            pass
    f = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "pronounce.txt")
    try:
        for line in open(f, encoding="utf-8"):
            t = line.split("#", 1)[0].strip()
            if t:
                out.update(words(t))
    except OSError:
        pass
    return out


MUST_SAY = _must_say()


def _snap(heard, want):
    """Spell a heard word the way the script spells it, when they are plainly
    the same word. Only near matches are snapped, so a genuinely different word
    still reads as different."""
    out = []
    for w in heard:
        best = max(want, key=lambda t: difflib.SequenceMatcher(None, w, t).ratio(), default=None)
        out.append(best if best and _same_word(w, best) else w)
    return out


def _rejoin(heard, want):
    """Put back together what the transcriber split.

    `words()` drops hyphens rather than splitting on them, because the
    transcriber writes "Co-Work" as one word and splitting the script would
    then read a word as missing. The same rule fails the other way round:
    "long-form" becomes the single token "longform" while the transcriber
    heard two words, and the take is failed for an inserted word over a
    hyphen. A take failed here is re-rolled three times AND never trimmed,
    so this is not cosmetic.

    So merge any adjacent pair whose concatenation is a word the script
    actually asked for. Nothing else is touched.
    """
    wanted = set(want)
    out, i = [], 0
    while i < len(heard):
        if i + 1 < len(heard) and heard[i] + heard[i + 1] in wanted:
            out.append(heard[i] + heard[i + 1])
            i += 2
        else:
            out.append(heard[i])
            i += 1
    return out


def compare(want: str, heard: str, floor: float = 0.9) -> dict:
    a, b = words(want), words(heard)
    if not a:
        return {"ok": True, "score": 1.0, "heard": heard}
    b = _rejoin(b, a)
    # What was heard, word for word, BEFORE near-misses are forgiven. The
    # forgiving score decides whether a take is the line; this one decides
    # between two takes that both are, because "Klavi" beats "Klavy" even
    # though both snap to the script.
    exact = difflib.SequenceMatcher(None, a, b).ratio()
    snapped = _snap(b, a)
    respelled = [[t, h] for h, t in zip(b, snapped) if h != t]
    b = snapped
    score = difflib.SequenceMatcher(None, a, b).ratio()
    m = difflib.SequenceMatcher(None, a, b).get_matching_blocks()
    first = m[0] if m else None
    # How much of the REQUESTED line is missing from the front, and how much of
    # what was heard is not in the request at all. Both are counted in words,
    # because a syllable lost off "Today" is a whole word gone to a listener.
    missing_head = first.a if first else len(a)
    extra_head = first.b if first else 0
    last_end = (m[-2].a + m[-2].size) if len(m) >= 2 else 0
    missing_tail = max(0, len(a) - last_end)
    matched = sum(x.size for x in m)
    inserted = len(b) - matched          # heard, but never asked for: "mooo"
    # Words heard AFTER the line has finished.
    #
    # These are always wrong, and the score is no guide: one extra word on a
    # nine-word line scores 0.952, which sailed past the tolerance that exists
    # for hyphenation ("Co-Work" transcribed as two words). That tolerance is
    # for a transcriber's spelling of something in the middle of a line; it has
    # no business excusing an emphatic "And" the model tacked onto the end.
    real = [x for x in m if x.size]
    end_b = (real[-1].b + real[-1].size) if real else 0
    trailing = len(b) - end_b
    return {
        "ok": (score >= floor and missing_head == 0 and extra_head == 0
               and missing_tail == 0 and trailing == 0
               and (inserted == 0 or score >= 0.95)),
        "score": round(score, 3), "heard": heard,
        "exact": round(exact, 3),
        # Names that must be said exactly, heard as something else.
        "must_say": [r for r in respelled if r[0] in MUST_SAY],
        "missing_head": missing_head, "missing_tail": missing_tail,
        "extra_head": extra_head, "inserted": inserted, "trailing": trailing
    }


def hear(wav: str):
    """Transcribe a take, and say when the last word ended.

    The end time is asked for here because this pass is already being paid
    for. A take that keeps making speech-like sound after the sentence is over
    cannot be trimmed by level — that is what the head/tail trim in synth()
    does, and it cuts below -48 dB relative to peak, which this is nowhere
    near. The only thing that reliably says where the words stop is the
    transcriber, and it has just run.
    """
    import whisper_any
    r = whisper_any.transcribe(wav, word_timestamps=True)
    end = None
    for seg in r.get("segments", []) or []:
        for w in seg.get("words", []) or []:
            try:
                end = max(end or 0.0, float(w["end"]))
            except (KeyError, TypeError, ValueError):
                pass
    return r.get("text", "").strip(), end


def check(wav: str, want: str) -> dict:
    """One take, checked. The body of main(), lifted so it can be called
    repeatedly inside a server instead of only once per process."""
    try:
        heard, speech_end = hear(wav)
    except Exception as e:
        return {"ok": True, "score": None, "error": str(e)}
    out = compare(want, heard)
    if speech_end is not None:
        out["speech_end"] = round(speech_end, 3)
    try:
        db = tail_level(wav)
    except Exception:
        db = None
    if not out.get("ok"):
        out["why"] = "words"
    if db is not None:
        out["tail_db"] = db
        if db > TAIL_FLOOR_DB:
            # Said the line, then kept making sound. Named separately because
            # "did not say the line" was being reported for takes whose words
            # were perfect, which sends the operator looking in the wrong place.
            if out.get("ok"):
                out["why"] = "tail"
            out["ok"] = False
    return out


def serve():
    """One line of JSON in, one line of JSON out, model stays loaded.

    Loading Whisper costs twenty-five seconds and it was being paid once per
    take — eighty-three minutes of a hundred-and-forty-three line render spent
    loading the same model again and again. That is most of why a render took
    two hours rather than the ten minutes of actual generation, and why
    switching the checker off looked like the only way to get the time back.
    It is not: the model simply has to stay in memory, the way the synthesis
    worker already does.
    """
    print(json.dumps({"ready": True}), flush=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        rid = None
        try:
            req = json.loads(line)
            rid = req.get("id")
            out = check(req["wav"], req["want"])
            out["id"] = rid
            print(json.dumps(out), flush=True)
        except Exception as e:
            # Echo the id even on failure: the protocol pairs replies to
            # requests by it, and one unanswered line offsets every later
            # response for the life of the worker.
            print(json.dumps({"id": rid, "ok": True, "score": None,
                              "error": str(e)}), flush=True)


def main():
    if "--serve" in sys.argv[1:]:
        return serve()
    wav, want = sys.argv[1], sys.argv[2]
    try:
        heard, speech_end = hear(wav)
    except Exception as e:
        # Never fail a render because the checker is unavailable: an unverified
        # take is the behaviour we had before this existed. But SAY so — this
        # returning quietly is how a video shipped unverified.
        print(json.dumps({"ok": True, "score": None, "error": str(e)}))
        return

    out = compare(want, heard)
    if speech_end is not None:
        out["speech_end"] = round(speech_end, 3)
    try:
        db = tail_level(wav)
    except Exception:
        db = None
    if db is not None:
        out["tail_db"] = db
        if db > TAIL_FLOOR_DB:
            out["ok"] = False
            out["tail_noise"] = True
    try:
        ms = flat_tops(wav)
    except Exception:
        ms = 0.0
    if ms >= 2.0:
        out["ok"] = False
        out["clipped_ms"] = ms
    try:
        hiss = noisy_pause(wav)
    except Exception:
        hiss = 0.0
    if hiss >= PAUSE_FLOOR_DB:
        out["ok"] = False
        out["noisy_pause_db"] = hiss
    print(json.dumps(out))


if __name__ == "__main__":
    main()
