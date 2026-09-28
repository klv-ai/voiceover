#!/usr/bin/env python3
"""Polish a narration script line by line, as suggestions — never in place.

The rewrite pass turns improvised speech into grammatical speech, and the flow
pass makes the lines read as one piece. Neither makes it a SCRIPT. Every video
so far was then rewritten by hand, line by line, and the hand edits are
remarkably consistent — across six videos the words fell by 32-42%:

    said:  And so what we'll do is we'll go ahead and crack a project based
           off of, our ask event.
    final: Let's create this Project from our Ask response.

    said:  And so you can see here, I've got this revise option.
    final: We see a revise option.

That is: narrate what is on screen, now; cut the framing; one idea per line;
say a limitation once and plainly; use the product's own terms. This pass
learns it from those edits — the examples it is shown are the operator's own
before-and-after pairs, picked per batch for similarity to the lines at hand.

Two kinds of line, treated differently:

    DRAFT   still as a machine pass left it: polished fully.
    EDITED  already rewritten by the operator: a light touch only — spelling,
            grammar, product names, punctuation that reads badly aloud. His
            wording and meaning stay. A suggestion that moves his line further
            than that is thrown away rather than offered.

Nothing here changes the script. The output is a list of suggestions, each
against the exact text it was made for; the page offers them one at a time.

Beside the model sits a checker that is not a model at all, and runs on every
line: product-name spellings ("CopyWrote" is read aloud as "copy wrote"),
leftover filler, a full stop in mid-sentence, commas that become pauses,
"one of the best tool", lines too long for their place in the picture.

    ../.venv/bin/python3 lab/polish_script.py in.json -o polish.json
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import difflib
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from clean_script import ask, for_speech  # noqa: E402

SYSTEM = """You polish narration scripts for short product videos. The narrator \
recorded an improvised voice-over of a screen recording; each line will be \
spoken by a voice model over the exact moment of the recording it came from.

Write every line the way the narrator's own finished scripts are written (the \
examples show them):
- Narrate what is on screen, in the present tense: "We see a revise option." \
"Let's highlight a section and right-click."
- Cut framing and filler: "you can see here", "basically", "kind of", "I think", \
"and so forth", "go ahead and", "what we'll do is", "we've got".
- One idea per line, short and direct. Active voice.
- State a limitation once, plainly, without roadmap talk.
- Use the product's own names exactly, as listed.
- Never add a fact, number, claim or feature the line did not already contain, \
and never change what an action does ("pulls it out of Knowledge" is not \
"deletes it") or swap one person or thing for another.
- Keep contractions (it's, I'm, we're, don't); this is spoken, not written.
- Keep greetings, introductions and sign-offs whole: "Hi, this is Sam from Acme." \
stays a full sentence.
- Every block is spoken on its own, so each one is complete: never end a block \
on a comma or continue a sentence into the next block.
- Write for the ear: only commas and full stops; no dashes, brackets, \
quotation marks or abbreviations the voice would read oddly.

Reply with one line per requested block, as "NUMBER. text", in the order given, \
and nothing else. Only the numbered blocks you are asked for; never the \
context lines."""

GLOSSARY_NOTE = """Product names and terms, spelled exactly like this: {terms}. \
Speech recognition mishears them, so a transcript may have an ordinary word \
where one of these names was said; restore the right name where the line \
clearly means the product."""

# Words a coined product name is commonly misheard or misspelled as
# ("copyright" for "CopyWrite"). The checker flags these; it cannot know the
# narration did not mean the word. Filled from the studio's settings.
ALIASES: dict[str, str] = {}

# Coined names: any spelling that differs from these — case included — is a
# mistake. Ordinary words that happen to be product names are left out, since
# the lowercase word is usually meant. Filled from the studio's settings.
COINED: list[str] = []

FILLER = ["you can see here", "you know", "basically", "kind of", "sort of",
          "and so forth", "go ahead and", "i think", "what we'll do is",
          "we've got", "i've got"]

NUMBER_WORDS = {0: "zero", 1: "one", 2: "two", 3: "three", 4: "four", 5: "five",
                6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten", 11: "eleven",
                12: "twelve", 20: "twenty", 30: "thirty", 40: "forty", 50: "fifty",
                100: "hundred"}

STOP = {"i", "i'm", "we", "you", "that", "which", "in", "for", "to", "on", "of",
        "is", "are", "and", "or", "but", "it", "this", "as", "at", "by", "with"}


def words(t: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9']+", t or "")


def norm(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "").strip())


def check(text: str, budget: int | None = None) -> list[str]:
    """What is wrong with a line, in plain words. No model involved."""
    notes: list[str] = []
    t = norm(text)
    if not t:
        return notes
    low = t.lower()
    for w in words(t):
        for name in COINED:
            if w == name:
                continue
            if w.lower() == name.lower():
                notes.append(f'"{w}" should be spelled "{name}"')
            elif len(w) >= 5 and w[0].lower() == name[0].lower() and \
                    difflib.SequenceMatcher(None, w.lower(), name.lower()).ratio() >= 0.8:
                notes.append(f'"{w}" looks like a misspelling of "{name}"')
        if w.lower() in ALIASES and w != ALIASES[w.lower()]:
            notes.append(f'"{w}" — did you mean "{ALIASES[w.lower()]}"?')
    if re.search(r"\bword (document|doc|file|format)", t):
        notes.append('"word" should be "Word" (the file format)')
    for f in FILLER:
        if re.search(rf"\b{re.escape(f)}\b", low):
            notes.append(f'filler: "{f}"')
    if re.search(r"[A-Za-z]\.\s+[a-z]", t):
        notes.append("a full stop in the middle of a sentence")
    if t[0].isalpha() and t[0].islower():
        notes.append("starts with a lowercase letter")
    if re.search(r",\s*\w+,\s", t) and re.search(r"^\W*\w+,\s*\w+,", t):
        notes.append("commas around one word are read as two pauses")
    m = re.search(r"\bone of (?:the|my|our|your|its)\s+((?:[a-z-]+\s+){0,3}?[a-z-]+)"
                  r"(?=\s+(?:i|i'm|we|you|that|which|in|for|to|on|and|is)\b|[.,!?]|$)", t, re.I)
    if m:
        last = m.group(1).split()[-1].lower()
        if last not in STOP and not last.endswith("s"):
            notes.append(f'"one of the … {last}" wants a plural')
    if re.search(r"\s{2,}", text or ""):
        notes.append("double space")
    # Budget is TIME, not the words first spoken: the gap from this block's
    # start to the next one's, at the voice's pace. A line longer than what
    # was said is fine while the pause after it can absorb the difference;
    # past that, it pushes every following line late against the picture.
    if budget and len(words(t)) > budget:
        notes.append(f"{len(words(t))} words, but only about {budget} fit before the next block — "
                     f"later lines will start late")
    # One note per finding, in order.
    seen, out = set(), []
    for n in notes:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def content(t: str) -> set[str]:
    return {w.lower() for w in words(t) if len(w) > 3 and w.lower() not in STOP}


def pick_examples(bank: list[dict], batch: list[dict], k: int = 14) -> list[dict]:
    """The operator's own pairs most like the lines at hand, a few per video.

    Similar wording teaches the terms this video needs ("workspace user",
    "co-author"); capping per project keeps one video's habits from becoming
    the only style on show.
    """
    want = set().union(*(content(x["said"]) | content(x["now"]) for x in batch)) or set()
    scored = []
    for e in bank:
        c = content(e["said"]) | content(e["final"])
        if not c:
            continue
        s = len(c & want) / (len(c) ** 0.5 + 1)
        # Favour real transformations over near-copies: those carry the style.
        cut = 1 - len(words(e["final"])) / max(1, len(words(e["said"])))
        scored.append((s + 0.4 * max(0.0, cut), e))
    scored.sort(key=lambda x: -x[0])
    out, per = [], {}
    for _, e in scored:
        if per.get(e["project"], 0) >= 4:
            continue
        out.append(e)
        per[e["project"]] = per.get(e["project"], 0) + 1
        if len(out) >= k:
            break
    return out


def prompt_for(batch, before, after, examples, glossary):
    lines = [GLOSSARY_NOTE.format(terms=", ".join(glossary)), "",
             "EXAMPLES — what the narrator said, and their finished line:"]
    for e in examples:
        lines += [f"  said:  {e['said']}", f"  final: {e['final']}", ""]
    if before:
        lines.append("CONTEXT BEFORE (do not return):")
        lines += [f"  {x['n']}. {x['now']}" for x in before]
        lines.append("")
    lines.append("BLOCKS TO WRITE:")
    for x in batch:
        if x["edited"]:
            lines += [f"{x['n']}. [THE NARRATOR'S EDIT — keep their wording and meaning; fix only spelling, "
                      f"grammar, product names and punctuation that reads badly aloud; if it is "
                      f"already right, return it exactly]",
                      f"   current: {x['now']}"]
        else:
            lines += [f"{x['n']}. [DRAFT — write it as a finished line; at most {x['cap']} words; "
                      f"if it says nothing worth keeping, reply DROP]",
                      f"   said: {x['said']}",
                      f"   current: {x['now']}"]
    if after:
        lines += ["", "CONTEXT AFTER (do not return):"]
        lines += [f"  {x['n']}. {x['now']}" for x in after]
    return "\n".join(lines)


def parse(reply: str, wanted: set[int]) -> dict[int, str]:
    out = {}
    for line in reply.splitlines():
        m = re.match(r"\s*(\d+)[.)]\s*(.+?)\s*$", line)
        if m and int(m.group(1)) in wanted:
            out[int(m.group(1))] = m.group(2).strip().strip('"').strip("'")
    return out


def swaps(old: list[str], new: list[str], said: str) -> str | None:
    """A word in the operator's line replaced by a DIFFERENT word, or a new one added.

    A light touch respells ("ChatterBox" to "Chatterbox", "feature" to
    "features") and deletes ("if I were to share" to "if I share"). It does not
    swap one thing for another: asked only to tidy, the model still turned his
    "co-workers" into "co-authors" — one word, a different meaning, and
    invisible to any similarity score over the whole line.
    """
    a, b = [w.lower() for w in old], [w.lower() for w in new]
    heard = {w.lower() for w in words(said)}
    for op, i0, i1, j0, j1 in difflib.SequenceMatcher(None, a, b).get_opcodes():
        if op == "replace":
            for k, nb in enumerate(b[j0:j1]):
                oa = a[i0 + k] if i0 + k < i1 else None
                if nb in STOP or nb in heard:
                    continue
                if oa is None or difflib.SequenceMatcher(None, oa, nb).ratio() < 0.7:
                    return f'"{oa or ""}" became "{nb}"'
        elif op == "insert":
            for nb in b[j0:j1]:
                if nb not in STOP and nb not in heard and len(nb) > 3:
                    return f'added "{nb}"'
    return None


def judge(x: dict, got: str | None) -> dict | None:
    """Turn a model reply into a suggestion, or refuse it."""
    if got is None:
        return None
    if got.strip().upper().rstrip(".") == "DROP":
        if x["edited"]:
            return None           # never propose dropping a line he wrote
        return {"kind": "drop", "text": "", "why": "carries nothing the picture needs"}
    new = for_speech(got)
    if not new or norm(new) == norm(x["now"]):
        return None
    if new[0].isalpha() and new[0].islower():
        return {"rejected": "starts lowercase, as if continuing the block before"}
    if new.rstrip()[-1:] in ",;:":
        return {"rejected": "ends mid-sentence, as if continuing into the next block"}
    # A number the narration never contained is an invented fact — unless it
    # was said as a word the transcript kept as one: "one series" is the M1.
    spoken = (x["now"] + " " + x["said"]).lower()
    for d in re.findall(r"\d+", new):
        if d not in spoken and NUMBER_WORDS.get(int(d), "#") not in spoken:
            return {"rejected": f"added a number ({d}) that was never said"}
    nw, cw = words(new), words(x["now"])
    if x["edited"]:
        # A light touch moves few words. Anything more is the model rewriting
        # his line, which is exactly what this must not do.
        sim = difflib.SequenceMatcher(None, [w.lower() for w in cw],
                                      [w.lower() for w in nw]).ratio()
        if sim < 0.6 or len(nw) > len(cw) + 3:
            return {"rejected": f"rewrote an edited line too far (similarity {sim:.2f})"}
        swapped = swaps(cw, nw, x["said"])
        if swapped:
            return {"rejected": f"changed the operator's word: {swapped}"}
        kind = "spruce"
    else:
        # Shorter than the line it replaces is always an improvement in length,
        # even where both overrun; only a line that GROWS is held to the limits.
        if len(nw) > len(cw) and len(nw) > min(x["cap"] + 2, x["budget"]):
            return {"rejected": f"{len(nw)} words: longer than was said ({x['cap']}) "
                                f"or than fits before the next block ({x['budget']})"}
        if len(nw) < 2:
            return {"rejected": "too short to be a line"}
        kind = "polish"
    return {"kind": kind, "text": new}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("-o", "--output", required=True)
    ap.add_argument("--model", default="gemma4:12b")
    ap.add_argument("--host", default="http://localhost:11434")
    ap.add_argument("--batch", type=int, default=10)
    ap.add_argument("--jobs", type=int, default=3)
    a = ap.parse_args()

    doc = json.load(open(a.input))
    lines = doc["lines"]
    bank = doc.get("examples", [])
    glossary = doc.get("glossary", [])
    COINED[:] = doc.get("coined", [])
    ALIASES.update({str(k).lower(): str(v) for k, v in (doc.get("aliases") or {}).items()})
    todo = [x for x in lines if not x.get("skip")]

    # Context is the neighbouring CURRENT text, skipped lines included — a line
    # kept in the narrator's own voice is still part of what is being said.
    pos = {x["id"]: i for i, x in enumerate(lines)}
    batches = [todo[i:i + a.batch] for i in range(0, len(todo), a.batch)]

    def run(batch):
        i0, i1 = pos[batch[0]["id"]], pos[batch[-1]["id"]]
        before, after = lines[max(0, i0 - 2):i0], lines[i1 + 1:i1 + 3]
        ex = pick_examples(bank, batch)
        p = prompt_for(batch, before, after, ex, glossary)
        for attempt in range(2):
            try:
                return batch, parse(ask(a.model, a.host, SYSTEM, p), {x["n"] for x in batch}), None
            except Exception as e:  # noqa: BLE001 — reported per batch, never fatal
                err = str(e)
                time.sleep(2)
        return batch, {}, err

    suggestions, rejected, failed, replies = [], [], [], {}
    with cf.ThreadPoolExecutor(max_workers=max(1, a.jobs)) as pool:
        for batch, got, err in pool.map(run, batches):
            if err:
                failed.append({"blocks": [x["n"] for x in batch], "error": err[:200]})
            for x in batch:
                replies[x["n"]] = got.get(x["n"])
                j = judge(x, got.get(x["n"]))
                if not j:
                    continue
                if "rejected" in j:
                    rejected.append({"id": x["id"], "n": x["n"], "why": j["rejected"],
                                     "reply": got.get(x["n"], "")})
                    continue
                suggestions.append({"id": x["id"], "n": x["n"], "against": x["now"],
                                    "edited": x["edited"], **j,
                                    "notes": check(j["text"], x["budget"]) if j["text"] else []})

    # The checker, over every line as it stands — including lines the model
    # left alone, and including the operator's own.
    # Each note is pinned to the text it was made about, so the page can drop
    # it the moment the line is edited rather than nag about a fixed typo.
    checks = {x["id"]: {"text": x["now"], "notes": n} for x in lines if not x.get("skip")
              for n in [check(x["now"], x.get("budget"))] if n}

    json.dump({"model": a.model, "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
               "suggestions": sorted(suggestions, key=lambda s: s["n"]),
               "checks": checks, "rejected": rejected, "failed": failed,
               # Every block's raw reply. A block with no suggestion was either
               # returned unchanged or never answered, and those need telling apart.
               "replies": replies,
               "unanswered": [x["n"] for x in todo if replies.get(x["n"]) is None]},
              open(a.output, "w"), indent=1, ensure_ascii=False)
    print(f"{len(suggestions)} suggestions ({sum(s['edited'] for s in suggestions)} on edited lines), "
          f"{len(checks)} lines with checker notes, {len(rejected)} replies refused, "
          f"{len(failed)} batches failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
