#!/usr/bin/env python3
"""Turn a recorded read into training clips with EXACT transcripts.

This is the step that makes reading a script worth doing. A fine-tune wants
(audio, text) pairs, and the text is usually a transcriber's guess — the same
transcriber whose mistakes the rest of this pipeline exists to correct. Here
the words are already known, because the reader was reading them.

So: transcribe the sitting only to find out WHERE things were said, align that
against the script to find out WHICH passage was being read, and then cut the
audio at the passage boundaries and label each clip with the SCRIPT's own text.
The transcript is used for timing and thrown away.

Alignment is by words and monotonic, borrowed from align_pass, because
duration and order are weak evidence: a reader who pauses mid-passage or
re-reads a sentence breaks anything that counts seconds, and a single mistake
there poisons every clip after it.

    python3 cut_reads.py --script read_script.json --sessions a.wav b.wav \
        --out clips/ --manifest manifest.jsonl
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import subprocess
import sys

SR = 24000
PAD = 0.15          # a breath either side, so an onset is never clipped
# Sentences, not whole passages. A seventy-word passage read aloud is over
# twenty seconds, which is poor training material and sat right on the length
# guard - one passage in three was dropped for being a fraction too long. A
# sentence is the unit the model generates anyway.
MIN_S, MAX_S = 1.2, 16.0


def norm(w: str) -> str:
    return re.sub(r"[^a-z0-9]", "", w.lower())


def words_of(t: str):
    return [w for w in (norm(x) for x in t.split()) if w]


def heard(path: str):
    """Every word in the sitting, with when it was said."""
    import whisper_any
    r = whisper_any.transcribe(path, word_timestamps=True)
    out = []
    for seg in r.get("segments", []) or []:
        for w in seg.get("words", []) or []:
            t = norm(w.get("word", ""))
            if t:
                try:
                    out.append((t, float(w["start"]), float(w["end"])))
                except (KeyError, TypeError, ValueError):
                    pass
    return out


def sentences(text: str):
    """Split a passage the way it was read: at sentence ends."""
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p.strip() for p in parts if len(words_of(p)) >= 3]


def stumbled(said, t0, t1, sentence, slack=1.3):
    """Did the reader fumble this sentence?

    A stumble does not LOSE words, it adds them: "the cre- creative space" is
    the script's words plus a fragment. So the match count cannot see it - the
    sentence is all there - and a clip cut here would carry clean text over
    audio containing a stutter, which teaches the model to stutter while
    producing clean text. That is precisely the fault being chased in the
    renders, arriving through the training data.

    Counting what was actually heard inside the span against what the script
    asks for catches it, and cheaply. One fumbled word costs its own sentence
    and nothing else.
    """
    want = len(words_of(sentence))
    if not want:
        return True
    heard_here = sum(1 for _w, a, b in said if a >= t0 - 0.01 and b <= t1 + 0.01)
    return heard_here > want * slack


def align(said, units):
    """When was each UNIT of script read?

    `units` is [(key, words)]. The script is flattened into one word sequence,
    each word remembering which unit it came from, and the whole transcript is
    aligned against it at once. Monotonic by construction, so a unit cannot be
    handed audio belonging after the unit that follows it - and a reader who
    skips one leaves it unmatched rather than shifting everything after it,
    which is the failure that made duration-based matching unusable.
    """
    flat, owner = [], []
    for key, ws in units:
        for w in ws:
            flat.append(w)
            owner.append(key)
    if not flat or not said:
        return {}
    got = [w for w, _a, _b in said]
    out = {}
    sm = difflib.SequenceMatcher(None, flat, got, autojunk=False)
    for a, b, size in sm.get_matching_blocks():
        for k in range(size):
            key = owner[a + k]
            _w, t0, t1 = said[b + k]
            lo, hi, n = out.get(key, (t0, t1, 0))
            out[key] = (min(lo, t0), max(hi, t1), n + 1)
    return out


def cut(src: str, a: float, b: float, dest: str) -> bool:
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{a:.3f}",
                        "-t", f"{b - a:.3f}", "-i", src, "-ac", "1",
                        "-ar", str(SR), "-c:a", "pcm_s16le", dest],
                       capture_output=True)
    return r.returncode == 0 and os.path.exists(dest)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--script", required=True)
    ap.add_argument("--sessions", nargs="+", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--min-matched", type=float, default=0.65,
                    help="fraction of a sentence's words that must be found")
    ap.add_argument("--slack", type=float, default=1.3,
                    help="how many more words than the script may be heard in "
                         "a sentence before it is treated as a stumble")
    a = ap.parse_args()

    script = json.load(open(a.script))
    passages = script.get("passages") or []
    if not passages:
        print(json.dumps({"error": "that script has no passages"}))
        return 1
    by_id = {p["id"]: p for p in passages}
    os.makedirs(a.out, exist_ok=True)

    rows, used, report = [], set(), []
    for sess in a.sessions:
        if not os.path.exists(sess):
            continue
        said = heard(sess)
        if not said:
            report.append({"session": os.path.basename(sess),
                           "kept": 0, "why": "nothing was heard"})
            continue
        units, texts = [], {}
        for p in passages:
            for i, sent in enumerate(sentences(p["text"])):
                key = f"{p['id']}s{i+1:02d}"
                units.append((key, words_of(sent)))
                texts[key] = (p["id"], sent)
        found = align(said, units)
        kept = stumbles = 0
        for key, (t0, t1, n) in sorted(found.items(), key=lambda kv: kv[1][0]):
            if key in used:
                continue
            pid, sent = texts[key]
            want = len(words_of(sent))
            # A span built from a third of a sentence is not a reading of it,
            # it is a coincidence - and a clip labelled with words that were
            # never said teaches the model to say them anyway.
            if not want or n / want < a.min_matched:
                continue
            if stumbled(said, t0, t1, sent, a.slack):
                stumbles += 1
                continue
            lo, hi = max(0.0, t0 - PAD), t1 + PAD
            if not (MIN_S <= hi - lo <= MAX_S):
                continue
            dest = os.path.join(a.out, f"read_{key}.wav")
            if not cut(sess, lo, hi, dest):
                continue
            rows.append({"audio": os.path.abspath(dest), "text": sent,
                         "seconds": round(hi - lo, 2), "passage": pid,
                         "source": "read"})
            used.add(key)
            kept += 1
        report.append({"session": os.path.basename(sess), "kept": kept,
                       "stumbled": stumbles, "words_heard": len(said)})

    with open(a.manifest, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    secs = sum(r["seconds"] for r in rows)
    print(json.dumps({"clips": len(rows), "minutes": round(secs / 60, 1),
                      "passages": len(passages), "clips_matched": len(used),
                      "sessions": report}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
