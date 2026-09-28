"""Turn a word-level transcript into metrics, flags, a cut EDL and a pickup list.

The detectors are deliberately conservative. Anything that would delete speech
is opt-in; the default EDL only removes silence, which is the one edit that
cannot change what you said.
"""
from __future__ import annotations

import difflib
import functools
import os
import re

HARD_FILLERS = {"um", "umm", "uh", "uhh", "uhm", "er", "erm", "ah", "hmm", "mm", "mhm"}
SOFT_FILLERS = {"like", "so", "basically", "actually", "right", "okay", "yeah",
                "obviously", "essentially", "literally"}

# Function words carry low decoder confidence for reasons that have nothing to
# do with articulation — sentence-boundary ambiguity, mostly. Flagging them
# buries the real mumbles.
STOPWORDS = {
    "a", "an", "and", "the", "or", "but", "so", "of", "to", "in", "on", "at",
    "is", "it", "its", "we", "you", "i", "that", "this", "with", "for", "as",
    "be", "are", "was", "have", "has", "can", "will", "what", "if", "do", "our",
    "your", "they", "he", "she", "them", "there", "here", "from", "by", "not",
}

_norm_re = re.compile(r"[^a-z0-9']+")

# Conservative stems only. An aggressive "-ly" rule can cut a mangled brand
# term down to a short dictionary word — and a mangled brand term is precisely
# what we are trying to catch.
_SUFFIXES = (("'s", ""), ("s", ""), ("es", ""), ("ies", "y"), ("ed", ""), ("ing", ""))
_DICT_PATHS = ("/usr/share/dict/words", "/usr/dict/words")


def norm(token: str) -> str:
    return _norm_re.sub("", token.lower())


@functools.lru_cache(maxsize=1)
def _dictionary() -> frozenset:
    for path in _DICT_PATHS:
        if os.path.exists(path):
            try:
                with open(path, errors="ignore") as f:
                    return frozenset(w.strip().lower() for w in f if w.strip())
            except OSError:
                pass
    return frozenset()


def is_english_word(n: str) -> bool:
    d = _dictionary()
    if not d:
        return False
    if n in d:
        return True
    for suf, repl in _SUFFIXES:
        if n.endswith(suf) and len(n) - len(suf) >= 3:
            if n[: len(n) - len(suf)] + repl in d:
                return True
    return False


def _variant_of(a: str, b: str) -> bool:
    """True when a and b differ only by a plural/tense ending."""
    lo, hi = sorted((a, b), key=len)
    if not hi.startswith(lo):
        return False
    return hi[len(lo):] in ("", "s", "es", "d", "ed", "ing", "'s")


# Doubling these reads as emphasis, not as a stumble. Leave them alone.
RHETORICAL = {"very", "really", "no", "yes", "never", "much", "many", "far"}

_CONTRACTIONS = ("'ve", "'ll", "'re", "'d", "'m", "'s")


def repeat_key(token: str) -> str:
    """Normalise for repeat comparison only.

    Strips contraction endings so "we've got, um, uh, we got" is recognised as
    one phrase said twice rather than two different phrases.
    """
    # norm() keeps apostrophes, so match the suffix WITH its apostrophe.
    # Stripping the bare form instead left "we'" — which then failed to match
    # the plain "we" it was supposed to pair with.
    n = norm(token)
    for suf in _CONTRACTIONS:
        if n.endswith(suf) and len(n) - len(suf) >= 2:
            return n[: len(n) - len(suf)]
    return n


def repeat_drops(words, *, max_n=4, max_gap=0.6, skip_fillers=True):
    """Indices of the FIRST copy of an adjacent repeated phrase.

    Single-word doubles were never the main problem here. The dominant pattern
    is a two-word restart — "I can, I can take this", "in the, in the
    workspace", "he could, he could then" — which a word-vs-previous-word check
    cannot see at all, because no token equals its immediate neighbour.
    """
    live = [i for i, w in enumerate(words)
            if not (skip_fillers and norm(w["w"]) in HARD_FILLERS)]
    keys = [repeat_key(words[i]["w"]) for i in live]

    drops, j, n_live = set(), 0, len(live)
    while j < n_live:
        hit = 0
        for n in range(min(max_n, (n_live - j) // 2), 0, -1):
            a, b = keys[j:j + n], keys[j + n:j + 2 * n]
            if not all(a) or a != b:
                continue
            if n == 1 and a[0] in RHETORICAL:
                continue
            # A restatement seconds later is deliberate; a stumble is immediate.
            if words[live[j + n]]["start"] - words[live[j + n - 1]]["end"] > max_gap:
                continue
            drops.update(live[j:j + n])
            hit = n
            break
        j += hit or 1
    return drops


def disfluency_drops(words, *, fillers=False, repeats=False):
    """The single source of truth for what gets excised, shared by the
    detectors and the EDL so the report and the cut cannot disagree."""
    drop = set()
    if fillers:
        drop |= {i for i, w in enumerate(words) if norm(w["w"]) in HARD_FILLERS}
    if repeats:
        drop |= repeat_drops(words)
    return drop


# --------------------------------------------------------------- detectors

def _brand_misses(words, lexicon_terms, near_lo=0.65, near_hi=0.97,
                  conf_gate=0.55, dict_lo=0.80):
    """Words that look like a mangled brand term.

    Whisper writing "Fably" where you said "Fablie" is the most useful signal
    we get: the decoder is confidently wrong, so a plain confidence scan will
    never surface it.

    The discriminator is that a mangled brand term is not a word. Without that
    test this detector fires on every ordinary noun that happens to rhyme with
    a product — "products"/"Projects", "select"/"SvelteKit" — and drowns the
    one real hit. With it, the similarity threshold can stay loose: a mangling
    can score as little as 0.67, and tightening the ratio to exclude the noise
    would have excluded the signal too.

    The dictionary alone is not enough, though: a bigger model hears the same
    mumble as "Fable", which IS a word. So a dictionary word still counts as a
    miss when the decoder was unsure of it — confidence is what separates
    "products" said clearly from "fable" said instead of the company name.

    A dictionary word has to clear a higher bar (dict_lo) than a non-word,
    because "notably" is genuinely not an attempt at "Tabl" even when the
    decoder was unsure of it.
    """
    hits = []
    # Single-token terms only: a multi-word entry like "vector search"
    # normalises to "vectorsearch" and then scores 0.67 against the ordinary
    # word "search". Multi-word terms still earn their keep in initial_prompt.
    lex = [(t, norm(t)) for t in lexicon_terms
           if len(norm(t)) >= 4 and " " not in t.strip()]
    if not lex:
        return hits
    exact = {n for _, n in lex}
    for i, w in enumerate(words):
        n = norm(w["w"])
        if len(n) < 4 or n in exact:
            continue
        known = is_english_word(n)
        if known and w["p"] >= conf_gate:
            continue
        floor = dict_lo if known else near_lo
        best, score = None, 0.0
        for term, tn in lex:
            if _variant_of(n, tn):
                best, score = None, 0.0
                break
            # A real mangling is the same word said badly, so it is about the
            # same length. Without this, "chatbot" matches "Chatterbox".
            if abs(len(n) - len(tn)) > 2:
                continue
            r = difflib.SequenceMatcher(None, n, tn).ratio()
            if r > score:
                best, score = term, r
        if best and floor <= score < near_hi:
            hits.append({
                "kind": "brand",
                "index": i,
                "start": w["start"],
                "end": w["end"],
                "text": w["w"].strip(),
                "p": w["p"],
                "suggest": best,
                "detail": f"heard {w['w'].strip()!r}, closest brand term {best!r} ({score:.2f})",
            })
    return hits


def detect(words, segments, *, gap_min=0.9, low_conf=0.45, lexicon_terms=()):
    flags = []

    for i, w in enumerate(words):
        n = norm(w["w"])
        if n in HARD_FILLERS:
            flags.append({"kind": "filler", "index": i, "start": w["start"], "end": w["end"],
                          "text": w["w"].strip(), "p": w["p"], "detail": "hard filler"})
        elif n in SOFT_FILLERS:
            flags.append({"kind": "soft_filler", "index": i, "start": w["start"], "end": w["end"],
                          "text": w["w"].strip(), "p": w["p"], "detail": "discourse marker"})

        # Skip function words and the very first/last token: their confidence
        # reflects segmentation ambiguity, not how clearly you said them.
        if (w["p"] < low_conf and n not in STOPWORDS and len(n) >= 3
                and 0 < i < len(words) - 1):
            flags.append({"kind": "low_conf", "index": i, "start": w["start"], "end": w["end"],
                          "text": w["w"].strip(), "p": w["p"],
                          "detail": f"decoder confidence {w['p']:.2f}"})

    for i in range(1, len(words)):
        gap = words[i]["start"] - words[i - 1]["end"]
        if gap > gap_min:
            flags.append({"kind": "gap", "index": i, "start": words[i - 1]["end"],
                          "end": words[i]["start"], "text": "", "p": None,
                          "detail": f"{gap:.2f}s of dead air"})

    for i in sorted(repeat_drops(words)):
        flags.append({"kind": "stutter", "index": i, "start": words[i]["start"],
                      "end": words[i]["end"], "text": words[i]["w"].strip(),
                      "p": words[i]["p"], "detail": "repeated phrase"})

    flags.extend(_brand_misses(words, lexicon_terms))
    flags.sort(key=lambda f: (f["start"], f["kind"]))
    return flags


# ----------------------------------------------------------------- metrics

def metrics(words, flags, duration):
    if not words:
        return {}
    speech = words[-1]["end"] - words[0]["start"]
    minutes = speech / 60.0 or 1e-9
    gaps = [f for f in flags if f["kind"] == "gap"]
    gap_total = sum(f["end"] - f["start"] for f in gaps)
    hard = [f for f in flags if f["kind"] == "filler"]
    low = [f for f in flags if f["kind"] == "low_conf"]
    return {
        "words": len(words),
        "duration": round(duration, 2),
        "speech_span": round(speech, 2),
        "wpm": round(len(words) / minutes),
        "hard_fillers": len(hard),
        "fillers_per_min": round(len(hard) / minutes, 2),
        "soft_fillers": sum(1 for f in flags if f["kind"] == "soft_filler"),
        "stutters": sum(1 for f in flags if f["kind"] == "stutter"),
        "low_conf": len(low),
        "low_conf_per_min": round(len(low) / minutes, 2),
        "brand_misses": sum(1 for f in flags if f["kind"] == "brand"),
        "gap_count": len(gaps),
        "gap_seconds": round(gap_total, 2),
        "gap_pct": round(100 * gap_total / speech, 1) if speech else 0.0,
        "longest_gap": round(max((f["end"] - f["start"] for f in gaps), default=0.0), 2),
    }


# --------------------------------------------------------------------- EDL

def build_edl(words, duration, *, gap_max=0.60, gap_keep=0.30, splice_pad=0.03,
              head_pad=0.25, tail_pad=0.40, cut_fillers=False,
              cut_stutters=False, tempo=1.0, target_wpm=145):
    """Keep-ranges over the SOURCE timeline.

    Two kinds of junction, and they want different pauses:

    * a **gap** junction, where real silence was collapsed — leave `gap_keep`,
      because a beat between sentences is what makes speech listenable;
    * an **excision** junction, where a filler or a repeated phrase was lifted
      out mid-sentence — leave only `splice_pad` either side. The speaker did
      not pause there, and fluent speech runs words together with well under
      100ms between them, so anything longer reads as a hiccup rather than an
      edit. 0.03 each side lands at 60ms.

    An earlier version used the drop-set only to decide where gaps were
    measured, never to cut. A word was therefore excised only when removing it
    happened to open a gap wider than `gap_max`, so short fillers between
    tightly-spoken words survived: 26 of 59 supposedly-dropped words were still
    in the audio. Ranges are now built by walking kept words and breaking
    wherever a dropped word sits between two of them.
    """
    empty = {"keep": [], "gap_fill": gap_keep, "cut_fillers": cut_fillers,
             "cut_stutters": cut_stutters, "tempo": tempo}
    if not words:
        return empty

    drop = disfluency_drops(words, fillers=cut_fillers, repeats=cut_stutters)
    kept = [i for i in range(len(words)) if i not in drop]
    if not kept:
        return empty

    half = gap_keep / 2.0
    ranges, excisions, collapses = [], 0, 0
    start = max(0.0, words[kept[0]]["start"] - head_pad)

    for k in range(1, len(kept)):
        prev, cur = words[kept[k - 1]], words[kept[k]]
        removed = kept[k] - kept[k - 1] > 1          # dropped words in between
        gap = cur["start"] - prev["end"]
        if removed:
            pad = min(splice_pad, max(gap, 0.0) * 0.45)
            excisions += 1
        elif gap > gap_max:
            pad = min(half, gap * 0.45)
            collapses += 1
        else:
            continue
        ranges.append([round(start, 3), round(prev["end"] + pad, 3)])
        start = round(cur["start"] - pad, 3)

    ranges.append([round(start, 3),
                   round(min(duration, words[kept[-1]]["end"] + tail_pad), 3)])
    ranges = [r for r in ranges if r[1] - r[0] > 0.02]

    # Spans of hard fillers, for the ducking fallback. A filler that cannot be
    # cut without an audible seam can still be silenced in place: the timeline
    # is untouched, so there is no splice, and "um" simply becomes a pause.
    # Repeated phrases are deliberately NOT listed — silencing "I can" leaves a
    # half-second hole mid-sentence, which sounds worse than the stumble.
    filler_spans = [[round(max(0.0, words[i]["start"] - 0.02), 3),
                     round(min(duration, words[i]["end"] + 0.02), 3)]
                    for i in sorted(drop)
                    if norm(words[i]["w"]) in HARD_FILLERS]

    kept_secs = sum(b - a for a, b in ranges)
    post_cut_wpm = round(len(kept) / (kept_secs / 60.0)) if kept_secs else 0
    suggested = 1.0
    if post_cut_wpm:
        suggested = max(1.0, min(1.25, round(target_wpm / post_cut_wpm, 2)))

    return {
        "post_cut_wpm": post_cut_wpm,
        "suggested_tempo": suggested,
        "suggested_wpm": round(post_cut_wpm * suggested) if post_cut_wpm else 0,
        "keep": ranges,
        "gap_fill": gap_keep,
        "gap_max": gap_max,
        "splice_pad": splice_pad,
        "cut_fillers": cut_fillers,
        "cut_stutters": cut_stutters,
        "dropped_words": len(drop),
        "filler_spans": filler_spans,
        "excision_junctions": excisions,
        "gap_junctions": collapses,
        "tempo": tempo,
        "kept_seconds": round(kept_secs, 2),
        "estimated_out": round(kept_secs / (tempo or 1.0), 2),
        "saved_seconds": round(duration - kept_secs / (tempo or 1.0), 2),
    }


# ------------------------------------------------------------------ pickups

def build_pickups(words, segments, flags, *, max_items=40):
    """Sentences worth re-reading, worst first.

    A segment is scored by what went wrong inside it. Brand misses dominate,
    because a mangled product name in a sales video costs more than a mumble.
    """
    by_seg = {}
    for f in flags:
        if f["kind"] not in ("low_conf", "brand", "stutter", "filler"):
            continue
        seg = next((j for j, s in enumerate(segments)
                    if s["start"] - 0.01 <= f["start"] <= s["end"] + 0.01), None)
        if seg is None:
            continue
        by_seg.setdefault(seg, []).append(f)

    weight = {"brand": 6.0, "stutter": 2.0, "filler": 1.5, "low_conf": 1.0}
    strip_punct = str.maketrans("", "", ".,?!;:\"'")
    items = []
    for seg_i, fs in by_seg.items():
        s = segments[seg_i]
        score = sum(weight.get(f["kind"], 1.0) for f in fs)
        if score < 2.0:
            continue
        reasons = sorted({f["detail"] for f in fs})

        # The line as it should be READ, not as it was heard. A teleprompter
        # that says "Fably" because that is what Whisper transcribed would
        # reproduce the exact mistake we flagged it for.
        read_text, corrections = s["text"], []
        for f in fs:
            if f["kind"] != "brand" or not f.get("suggest"):
                continue
            core = f["text"].translate(strip_punct)
            if not core:
                continue
            pattern = re.compile(rf"\b{re.escape(core)}\b")
            if pattern.search(read_text):
                read_text = pattern.sub(f["suggest"], read_text, count=1)
                corrections.append(f"{core} → {f['suggest']}")

        items.append({
            "id": "",
            "segment": seg_i,
            "start": s["start"],
            "end": s["end"],
            "budget": round(s["end"] - s["start"], 2),
            "text": s["text"],
            "read_text": read_text,
            "corrections": corrections,
            "score": round(score, 1),
            "reasons": reasons[:4],
            "kinds": sorted({f["kind"] for f in fs}),
        })

    items.sort(key=lambda x: -x["score"])
    items = items[:max_items]
    items.sort(key=lambda x: x["start"])
    for n, it in enumerate(items, 1):
        it["id"] = f"{n:03d}"
    return items
