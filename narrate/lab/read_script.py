#!/usr/bin/env python3
"""Generate something worth reading aloud, to train a voice on.

A clone learns what it is given. The corpus mined out of existing screencasts
is thirty-odd minutes of product talk, cut at filler words, recorded across
different days at different distances from the microphone — enough to bias a
model toward a speaker and not enough to make it BE one. Tested directly: with
that fine-tune loaded and the reference clip taken away, pitch consistency got
WORSE, because the voice was never really in the weights.

Purpose-recorded material fixes the parts of that which are fixable. One
sitting, one distance, one mood — and above all a KNOWN script, so every clip
arrives with an exact transcript rather than a transcriber's guess at one.

What makes a good training read, and why this asks for it:

  * VARIED TOPICS. A model trained on nothing but product demos learns the
    cadence of a product demo. The narration it will have to perform is not
    only that.
  * VARIED SENTENCE LENGTH. Short declaratives and long winding ones teach
    different rhythms; a corpus of one length teaches a speaker who breathes
    on a timer.
  * QUESTIONS, LISTS, NUMBERS, NAMES. These carry intonation contours plain
    prose never reaches, and they are exactly what a narrator hits and fumbles.
  * NOTHING THAT NEEDS PERFORMING. This is not acting practice. Anything that
    invites a funny voice teaches a funny voice.

Prints JSON: {"language": ..., "passages": [{"id", "title", "text"}], "words"}.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request

# Short, and in the shape the model answered. An elaborate version of this —
# the same requirements as a bulleted list of rules — came back EMPTY every
# time, while a two-line request for a JSON array worked first go. The
# requirements are still here; they are just phrased as description rather
# than as a specification to comply with.
PROMPT = (
    "Return ONLY a JSON array of {n} objects, each "
    '{{"title": "two or three words", "text": "a passage of about {words} '
    'words to read aloud"}}. '
    "Language: {lang}. "
    "The passages are for someone training a speech model on their own voice, "
    "so they should be ordinary modern prose, nothing to perform, no dialogue "
    "or jokes or accents. "
    "Vary the topics widely and vary the sentence lengths a lot. "
    "Somewhere in the set include a question, a short list, some numbers or "
    "dates, and a couple of proper names."
)

# Asked for separately rather than folded into the prompt above. A single
# request carrying both the general requirements and a word list came back
# with the list obeyed and everything else ignored - every passage about the
# same handful of brands, which teaches the cadence of a product page.
LEARN = (
    "Return ONLY a JSON array of {n} objects, each "
    '{{"title": "two or three words", "text": "a passage of about {words} '
    'words to read aloud"}}. '
    "Language: {lang}. "
    "Each passage must use these names naturally, in ordinary sentences: "
    "{names}. "
    "Spread them out; do not put them all in one passage. Plain modern prose, "
    "nothing to perform."
)


def ask(model: str, prompt: str, url: str, timeout: int = 900) -> str:
    # keep_alive: a local model must not sit on the GPU the voice model needs.
    body = json.dumps({"model": model, "prompt": prompt, "stream": False, "keep_alive": "30s",
                       "options": {"temperature": 0.9}}).encode()
    req = urllib.request.Request(f"{url}/api/generate", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode()).get("response", "")


def parse(raw: str):
    """Take the JSON array out of whatever the model wrapped it in."""
    m = re.search(r"\[.*\]", raw, re.S)
    if not m:
        return []
    try:
        rows = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    out = []
    for i, r in enumerate(rows):
        if not isinstance(r, dict):
            continue
        text = " ".join((r.get("text") or "").split())
        if len(text.split()) < 15:
            continue
        out.append({"id": f"p{i+1:03d}",
                    "title": (r.get("title") or f"Passage {i+1}").strip()[:40],
                    "text": text})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--output", required=True)
    # The CLOUD model by default. This runs once to make a script, not per
    # line of a render, so the thing that matters is how long the operator
    # waits — and a bigger model writes more varied passages, which is the
    # whole point of the exercise.
    ap.add_argument("--model", default="gemma4:12b")
    ap.add_argument("--ollama", default="http://localhost:11434")
    ap.add_argument("--language", default="English")
    # Thirty minutes at a narrator's ~160 wpm is roughly 4800 words. Asked for
    # in batches because one request for all of it comes back truncated or
    # repetitive, and because a reader wants to stop and start anyway.
    ap.add_argument("--passages", type=int, default=24)
    # Words the model will get wrong on sight - brand names, surnames,
    # acronyms. Putting them in the script is the ONLY way a fine-tune learns
    # them, because it learns from what it is given; a substitution table at
    # synthesis time is a patch over a model that never heard the word.
    ap.add_argument("--words-to-learn", default="",
                    help="comma-separated names the voice must learn to say")
    ap.add_argument("--words", type=int, default=90)
    a = ap.parse_args()

    # A quarter of the script, at most, is about the words that have to be
    # learned. Enough repetition for a model to hear them several times in
    # this speaker's mouth; not so much that the corpus becomes a brand list.
    learn = [w.strip() for w in a.words_to_learn.split(",") if w.strip()]
    learn_n = min(max(2, a.passages // 4), 8) if learn else 0

    got, tries = [], 0
    while len(got) < a.passages and tries < 4:
        tries += 1
        want = a.passages - len(got)
        if learn_n and len(got) < learn_n:
            raw = ask(a.model, LEARN.format(n=min(learn_n - len(got), 4),
                                            lang=a.language, words=a.words,
                                            names=", ".join(learn)), a.ollama)
        else:
            raw = ask(a.model, PROMPT.format(n=min(want, 6), lang=a.language,
                                             words=a.words), a.ollama)
        batch = parse(raw)
        # A model asked four times for varied passages will repeat itself; keep
        # the reader from reading the same paragraph twice.
        seen = {p["text"][:60] for p in got}
        got += [p for p in batch if p["text"][:60] not in seen]
    if not got:
        # Keep what it DID say. "Returned nothing usable" without the thing it
        # returned is a dead end; the failure is almost always the prompt.
        json.dump({"error": f"{a.model} returned nothing usable",
                   "raw": raw[:2000]}, open(a.output, "w"))
        return 1

    for i, p in enumerate(got):
        p["id"] = f"p{i+1:03d}"
    words = sum(len(p["text"].split()) for p in got)
    json.dump({"language": a.language, "model": a.model,
               "words_to_learn": learn,
               "passages": got, "words": words,
               "minutes_at_160wpm": round(words / 160.0, 1)},
              open(a.output, "w"), indent=2)
    print(json.dumps({"passages": len(got), "words": words,
                      "minutes": round(words / 160.0, 1)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
