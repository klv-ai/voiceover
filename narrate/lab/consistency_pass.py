"""Make a cleaned script read as ONE piece, not thirty-seven repairs.

`clean_script.py` fixes each line in isolation, which is the right unit for
removing a false start and the wrong one for flow. Once the ums are gone what
is left reads as a list: every sentence opens the same way, the same idea is
named three different things, and nothing connects to what came before.

This pass sees the WHOLE script at once and edits for continuity — consistent
terminology, varied sentence openings, connective tissue between neighbours —
while leaving the content alone.

The invariant that matters is unchanged: ONE LINE IN, ONE LINE OUT, IN ORDER.
Every line is anchored to a moment in the picture, so a merge or a reorder
silently breaks the edit. The validator enforces that rather than trusting it,
and any line that fails its checks keeps the text it came in with.

    ../.venv/bin/python3 lab/consistency_pass.py script.json -o out.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from clean_script import LEXICON_NOTE, ask, for_speech, parse_numbered  # noqa: E402

SYSTEM = (
    "You are editing a narration script that is read aloud over a screen "
    "recording. The words are already correct; your job is to make the whole "
    "thing read as one continuous piece rather than a list of separate "
    "sentences.\n"
    "Do: vary sentence openings that repeat, settle on ONE name for each thing "
    "and use it throughout, and add small connectives so each line follows on "
    "from the one before.\n"
    "Never: add facts, claims or product behaviour that is not already there; "
    "merge, split, reorder or drop a line; change a technical or product term "
    "to a different word.\n"
    "Every line is anchored to a moment in the video, so the reply must be the "
    "SAME numbered lines in the SAME order, and nothing else.\n"
    "Two rules about SPEECH, because each line is recorded separately with a "
    "pause after it: keep contractions (it's, we're, that's) — this is spoken, "
    "not written — and never end a line with a colon, semicolon or a dangling "
    "connector that leads into the next one. Every line must stand as a "
    "complete spoken sentence on its own."
)

# A flow pass legitimately adds a connective or two, so the upper bound is
# looser than the repair pass's — but a line that doubles has had content
# invented, and one that halves has had content dropped.
LO, HI = 0.6, 1.8


def words(t: str) -> int:
    return len([w for w in t.strip().split() if w])


def check(before: str, after: str, lexicon):
    """Why this rewrite should be rejected, or None to accept it."""
    a = after.strip()
    if not a:
        return "empty"
    # Each line is a separate take with a pause after it, so a line that leads
    # into the next one is read with a trailing rise and then silence. The
    # model reaches for these when asked to improve flow: it produced
    # "...MCP connections, starting with the question:" as a lead-in.
    if a[-1] in ":;,-–—":
        return f"dangles on '{a[-1]}'"
    n0, n1 = words(before), words(a)
    if n0 and not (LO * n0 <= n1 <= HI * n0):
        return f"length {n0}->{n1}"
    # A product name that was present must survive. The rewriter is allowed to
    # ADD one (that is the point of settling on a single term) but never to
    # drop or replace one it was given.
    low_before, low_after = before.lower(), a.lower()
    for term in lexicon:
        t = term.lower()
        if t in low_before and t not in low_after:
            return f"dropped '{term}'"
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("script", help='JSON: {"lines": ["...", ...]}')
    ap.add_argument("-o", "--output", required=True)
    ap.add_argument("--model", default="gemma4:12b")
    ap.add_argument("--host", default="http://localhost:11434")
    ap.add_argument("--lexicon", default=None, help="brand terms, one per line")
    ap.add_argument("--window", type=int, default=40,
                    help="lines per request; they overlap so flow carries across")
    a = ap.parse_args()

    doc = json.load(open(a.script))
    lines = list(doc["lines"])
    lexicon = []
    if a.lexicon and os.path.exists(a.lexicon):
        lexicon = [w.strip() for w in open(a.lexicon) if w.strip()]

    out = list(lines)
    rejected = []
    # Windows overlap by a few lines so the model can see what it just wrote
    # and carry the tone across the seam.
    step = max(1, a.window - 4)
    for start in range(0, len(lines), step):
        chunk = lines[start:start + a.window]
        if not chunk:
            break
        body = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(chunk))
        note = LEXICON_NOTE + "\n" + ", ".join(lexicon) if lexicon else ""
        reply = ask(a.model, a.host, SYSTEM + ("\n" + note if note else ""), body)
        # parse_numbered returns a DICT keyed by line number, not a list —
        # iterating it yields the keys.
        got = parse_numbered(reply, len(chunk))
        if not got:
            rejected.append(f"window at {start + 1}: unparseable reply")
            continue
        for i in range(len(chunk)):
            idx = start + i
            if idx >= len(lines):
                break
            new = got.get(i + 1)
            if not new:
                rejected.append(f"line {idx + 1}: missing from the reply")
                continue
            why = check(lines[idx], new, lexicon)
            if why:
                rejected.append(f"line {idx + 1}: {why}")
                continue
            out[idx] = for_speech(new)

    changed = [i for i, (b, c) in enumerate(zip(lines, out)) if b.strip() != c.strip()]
    json.dump({"lines": out, "changed": changed, "rejected": rejected},
              open(a.output, "w"), indent=2)
    print(json.dumps({"lines": len(out), "changed": len(changed),
                      "rejected": len(rejected)}))


if __name__ == "__main__":
    main()
