"""Rewrite an improvised transcript into a script worth speaking.

The mechanical rewrite in repair_dsl.py only deletes — fillers and repeated
words — which leaves the grammar of improvised speech intact: "save that
against that experts", "within the... when they can copywrite". A voice model
reads those literally and the result sounds wrong in a way that is nothing to
do with the voice.

This asks a language model to turn each line into clean spoken prose. Rules
that matter, and that the validator enforces rather than trusts:

  * one line in, one line out, in order — the timing anchors depend on it
  * no new facts, no invented product behaviour
  * roughly the same length; a line that doubles has had content invented,
    and a line that collapses has had content dropped

Anything failing those checks keeps the mechanical rewrite instead.

    ../.venv/bin/python3 lab/clean_script.py requests.json -o cleaned.json
"""
from __future__ import annotations

import argparse
import json
import re
import urllib.request

SYSTEM = (
    "You clean up transcripts of improvised technical narration so they can be "
    "read aloud. For each numbered line, rewrite it as fluent spoken English: "
    "fix broken grammar, false starts and dangling phrases; keep the speaker's "
    "own words, tone and technical terms. Never add facts, claims or product "
    "behaviour that is not already there. Never merge or split lines. Reply "
    "with the same numbered lines and nothing else."
)

# The transcript is what a speech recogniser HEARD, and it mishears product
# names confidently: "Ask" came back as "asset", "CopyWrite" as "copyright".
# Those survive every downstream step — the grammar gets cleaned around the
# wrong word and the voice model reads it faithfully. The rewriter is the right
# place to catch them, because it is the only stage with enough context to know
# that "voice capability within the copyright" is not a thing and
# "within CopyWrite" is.
LEXICON_NOTE = (
    "\n\nThese are product names in this narration: {terms}. The transcript was "
    "produced by speech recognition and frequently mishears them as ordinary "
    "words (a product named 'Lumen' might come back as 'lemon'). Where a "
    "line clearly means one of these products, restore the correct name. Do not "
    "introduce a product name into a line that was not talking about one."
)


def ask(model: str, host: str, system: str, user: str, timeout: int = 900) -> str:
    req = urllib.request.Request(
        f"{host}/api/chat",
        # keep_alive: a LOCAL model otherwise stays on the GPU for five
        # minutes after the pass, and the voice model does not fit beside it.
        # Thirty seconds keeps it warm between a pass's own batches.
        data=json.dumps({"model": model, "stream": False, "think": False, "keep_alive": "30s",
                         "options": {"temperature": 0.2},
                         "messages": [{"role": "system", "content": system},
                                      {"role": "user", "content": user}]}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())["message"]["content"]


def for_speech(t: str) -> str:
    """Normalise punctuation a TTS engine reads badly.

    The rewrite comes back as written prose — em-dashes, smart quotes, ellipses
    — and a speech model renders those literally or stumbles on them. Spoken
    text wants commas and full stops and nothing else.
    """
    t = (t.replace("\u2014", ", ").replace("\u2013", ", ")
          .replace("\u2026", ".").replace("...", ".")
          .replace("\u201c", "").replace("\u201d", "")
          .replace("\u2018", "'").replace("\u2019", "'")
          .replace('"', "").replace(" - ", ", "))
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"\s+([,.!?])", r"\1", t)
    t = re.sub(r",\s*,", ",", t)
    return t.strip()


def parse_numbered(text: str, n: int):
    out = {}
    for line in text.splitlines():
        m = re.match(r"\s*(\d+)[.)]\s*(.+?)\s*$", line)
        if m:
            i = int(m.group(1))
            if 1 <= i <= n:
                out[i] = m.group(2).strip().strip('"').strip("'")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("requests")
    ap.add_argument("-o", "--output", required=True)
    ap.add_argument("--model", default="gemma4:12b")
    ap.add_argument("--host", default="http://localhost:11434")
    ap.add_argument("--batch", type=int, default=12)
    ap.add_argument("--lexicon", default=None,
                    help="brand terms file (defaults to the repo's lexicon.txt)")
    a = ap.parse_args()

    import os
    lex_path = a.lexicon or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lexicon.txt")
    system = SYSTEM
    if os.path.exists(lex_path):
        terms = [ln.strip() for ln in open(lex_path)
                 if ln.strip() and not ln.lstrip().startswith("#")]
        if terms:
            system += LEXICON_NOTE.format(terms=", ".join(terms))
            print(f"  lexicon: {len(terms)} product names given to the rewriter")

    doc = json.load(open(a.requests))
    reqs = doc["requests"]
    kept = changed = rejected = 0

    for start in range(0, len(reqs), a.batch):
        chunk = reqs[start:start + a.batch]
        prompt = "\n".join(f"{i+1}. {r['say']}" for i, r in enumerate(chunk))
        try:
            reply = ask(a.model, a.host, system, prompt)
        except Exception as e:
            print(f"  batch {start//a.batch+1}: request failed ({e}) — keeping originals")
            continue
        got = parse_numbered(reply, len(chunk))
        for i, r in enumerate(chunk):
            new = got.get(i + 1)
            r["say_raw"] = r["say"]
            if not new:
                rejected += 1
                continue
            old_w, new_w = len(r["say"].split()), len(new.split())
            if new_w < old_w * 0.55 or new_w > old_w * 1.6:
                # content dropped or invented
                rejected += 1
                continue
            if for_speech(new) == r["say"].strip():
                kept += 1
                continue
            r["say"] = for_speech(new)
            changed += 1

    doc["cleaned_with"] = a.model
    json.dump(doc, open(a.output, "w"), indent=2)
    print(f"  {changed} rewritten, {kept} unchanged, {rejected} rejected "
          f"(kept the mechanical version)")
    for r in reqs[:6]:
        if r.get("say_raw") and r["say_raw"] != r["say"]:
            print(f"\n    was: {r['say_raw'][:96]}")
            print(f"    now: {r['say'][:96]}")


if __name__ == "__main__":
    main()
