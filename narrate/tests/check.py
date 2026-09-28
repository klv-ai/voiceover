"""Self-checks. Plain python3, no pytest — the core has no dependencies and
neither does its test.

    python3 tests/check.py
"""
from __future__ import annotations

import ast
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from narrate import analyze as A
from narrate import cut as C
from narrate import transcribe as T

FAILS = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'  — ' + detail if detail and not cond else ''}")
    if not cond:
        FAILS.append(name)


def words(spec):
    """spec: [(text, start, end, prob), …]"""
    return [{"w": t, "start": s, "end": e, "p": p} for t, s, e, p in spec]


# ---------------------------------------------------------------- stdlib only

def test_stdlib_only():
    std = set(sys.stdlib_module_names)
    pkg = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "narrate")
    bad = []
    for f in sorted(os.listdir(pkg)):
        if not f.endswith(".py"):
            continue
        tree = ast.parse(open(os.path.join(pkg, f)).read())
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                bad += [(f, a.name) for a in n.names if a.name.split(".")[0] not in std]
            elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
                if n.module.split(".")[0] not in std:
                    bad.append((f, n.module))
    check("core is stdlib-only", not bad, str(bad))


# ------------------------------------------------------------ brand detector

LEX = ["Fablie", "Klavi", "Tabl", "Chatterbox", "CopyWrite", "on-premise",
       "vector search", "Experts", "Projects", "SvelteKit", "self-hosted"]


def test_brand_detector():
    # (heard, confidence, should_flag)
    cases = [
        ("Fably", 0.32, True),        # mangled, not a word
        ("Fable", 0.07, True),        # a word, but the decoder was unsure
        ("Clavi", 0.44, True),        # C for K
        ("fables", 0.30, True),       # the plural, just as unsure
        ("products", 0.90, False),    # ordinary word, said clearly
        ("select", 0.80, False),      # ordinary word
        ("expert", 0.95, False),      # singular of a brand term, not a mangling
        ("experts", 0.95, False),     # exact brand term
        ("writer", 0.70, False),      # ordinary word
        ("chatbot", 0.40, False),     # too far from Chatterbox in length
        ("notably", 0.30, False),     # a word, and only 0.73 to "Tabl"
        ("OneDrive", 0.40, False),    # 0.59 to "on-premise" — below the floor
        ("search", 0.30, False),      # multi-word lexicon entries can't match
    ]
    ws = words([(t, i, i + 0.4, p) for i, (t, p, _) in enumerate(cases)])
    hits = {h["text"] for h in A._brand_misses(ws, LEX)}
    for text, _, want in cases:
        check(f"brand: {text!r} {'flagged' if want else 'ignored'}",
              (text in hits) == want, f"hits={sorted(hits)}")


# ------------------------------------------------------------------- the EDL

def test_edl_never_widens():
    ws = words([("a", 0.0, 0.3, .9), ("b", 0.5, 0.8, .9),      # 0.20s gap: untouched
                ("c", 1.5, 1.8, .9),                            # 0.70s gap: collapsed
                ("d", 6.0, 6.3, .9)])                           # 4.20s gap: collapsed
    edl = A.build_edl(ws, 8.0, gap_max=0.60, gap_keep=0.30)
    keep = edl["keep"]
    check("EDL collapses only gaps over gap_max", len(keep) == 3, str(keep))

    # The pause the LISTENER hears is the padding kept on either side of the
    # junction, because concatenation makes the ranges adjacent. Measuring the
    # distance between ranges on the source timeline measures the deleted gap.
    junctions = [(0.8, 1.5), (1.8, 6.0)]   # (prev word end, next word start)
    heard, originals = [], []
    for i, (prev_end, next_start) in enumerate(junctions):
        heard.append(round((keep[i][1] - prev_end) + (next_start - keep[i + 1][0]), 4))
        originals.append(round(next_start - prev_end, 4))
    check("EDL never widens a pause",
          all(h <= 0.30 + 1e-9 and h < o for h, o in zip(heard, originals)),
          f"heard={heard} original={originals}")
    check("EDL keeps sub-threshold gaps intact",
          abs((keep[0][1] - keep[0][0]) - 0.95) < 1e-6, str(keep))


def test_phrase_repeats():
    """The dominant disfluency here is a two-word restart, which a
    word-vs-previous-word check cannot see: no token equals its neighbour."""
    def seq(tokens, step=0.25):
        return words([(t, i * step, i * step + 0.2, 0.9) for i, t in enumerate(tokens)])

    ws = seq(["is", "I", "can", "I", "can", "take", "this"])
    drops = A.repeat_drops(ws)
    check("two-word restart caught", drops == {1, 2}, f"drops={sorted(drops)}")

    ws = seq(["in", "the", "in", "the", "workspace"])
    check("'in the, in the' caught", A.repeat_drops(ws) == {0, 1},
          str(sorted(A.repeat_drops(ws))))

    # fillers between the two copies must not hide the repeat
    ws = seq(["we've", "got", "um", "uh", "we", "got", "one"])
    check("repeat across intervening fillers caught",
          A.repeat_drops(ws) == {0, 1}, str(sorted(A.repeat_drops(ws))))

    ws = seq(["these", "are", "very", "very", "generic"])
    check("rhetorical doubling left alone", A.repeat_drops(ws) == set(),
          str(sorted(A.repeat_drops(ws))))

    # a restatement seconds later is deliberate, not a stumble
    ws = words([("so", 0.0, 0.2, .9), ("so", 3.0, 3.2, .9)])
    check("distant repeat left alone", A.repeat_drops(ws) == set(),
          str(sorted(A.repeat_drops(ws))))


def test_edl_actually_excises():
    """Regression: the drop-set used to be advisory. A word was excised only
    when removing it happened to open a gap wider than gap_max, so short
    fillers between tightly-spoken words survived — 26 of 59 on real footage."""
    ws = words([("keep", 0.0, 0.30, .9), ("um", 0.34, 0.48, .9),
                ("also", 0.52, 0.90, .9), ("end", 0.95, 1.30, .9)])
    edl = A.build_edl(ws, 2.0, cut_fillers=True, cut_stutters=True)
    mid = (0.34 + 0.48) / 2
    inside = any(a <= mid <= b for a, b in edl["keep"])
    check("a short filler is actually removed", not inside, str(edl["keep"]))
    check("excision junction recorded", edl["excision_junctions"] == 1,
          str(edl.get("excision_junctions")))
    # The pause a LISTENER hears is the padding kept either side, since
    # concatenation makes the ranges adjacent — not the source-timeline
    # distance between them, which is the deleted word plus the padding.
    heard = (edl["keep"][0][1] - 0.30) + (0.52 - edl["keep"][1][0])
    check("excision leaves a splice, not a sentence break", heard <= 0.08,
          f"{heard:.3f}s heard")


def test_edl_stutters_and_fillers():
    ws = words([("we", 0.0, 0.2, .9), ("we", 0.25, 0.45, .9), ("click", 0.5, 0.9, .9),
                ("um", 1.0, 1.2, .9), ("then", 1.3, 1.7, .9)])
    plain = A.build_edl(ws, 2.0)
    both = A.build_edl(ws, 2.0, cut_fillers=True, cut_stutters=True)
    check("EDL drops nothing by default", plain["dropped_words"] == 0)
    check("EDL drops the filler and the first of a repeat", both["dropped_words"] == 2,
          f"dropped={both['dropped_words']}")


# ------------------------------------------------------- frame-grid snapping

def test_frame_snapping():
    fps = 60.0
    raw = [(0.0, 1.007), (2.013, 3.004)]
    snapped = [(round(a * fps) / fps, round(b * fps) / fps) for a, b in raw]
    graph = C.video_filtergraph(snapped, 1.0, fps)
    check("filtergraph normalises to CFR before select",
          f"fps={fps:g}," in graph and "select=" in graph
          and graph.index("fps=") < graph.index("select="), graph)
    check("filtergraph divides by the same rate it forced",
          f"setpts=N/{fps:g}/TB" in graph, graph)
    # The start guard band is load-bearing: without it the frame at exactly
    # t = a is sometimes dropped, and only the picture loses it, so sound
    # creeps ahead of picture across a long video.
    import re as _re
    pairs = _re.findall(r"between\(t,([-\d.]+),([-\d.]+)\)", graph)
    check("filtergraph emits one between() per range", len(pairs) == len(snapped),
          str(pairs))
    got_lo = float(pairs[0][0])
    want_lo = snapped[0][0] - 0.25 / fps
    check("start boundary pulled back a quarter frame",
          abs(got_lo - want_lo) < 1e-4, f"{got_lo} vs {want_lo}")
    got_hi = float(pairs[0][1])
    want_hi = snapped[0][1] - 0.5 / fps
    check("end boundary pulled back half a frame",
          abs(got_hi - want_hi) < 1e-4, f"{got_hi} vs {want_hi}")
    for a, b in snapped:
        # frames at k/fps with a - 1/4 frame <= t <= b - 1/2 frame
        lo, hi = a - 0.25 / fps, b - 0.5 / fps
        first = math.ceil(round(lo * fps, 6))
        last = math.floor(round(hi * fps, 6))
        frames = last - first + 1
        check(f"segment {a:.4f}-{b:.4f} yields exact frames",
              abs(frames - (b - a) * fps) < 1e-6, f"{frames} vs {(b - a) * fps}")


# ------------------------------------------------------------- seam gating

def test_seam_gate_abandons_unhideable_cuts():
    """A junction has to earn its cut. Whisper's word boundaries sit inside
    continuous speech, so most candidate cuts land mid-vowel; on real footage
    70 of 79 were in voiced audio at a median of -22 dBFS, which is what made
    the edits audible. Cuts that cannot be hidden are abandoned instead."""
    import array
    import math as _m
    rate, ch = 48000, 1
    n = rate * 3
    sm = array.array("h", [0] * n)

    def tone(t0, t1, amp):
        for f in range(int(t0 * rate), int(t1 * rate)):
            sm[f] = int(amp * 32767 * _m.sin(2 * _m.pi * 150 * f / rate))

    # loud throughout: any cut here lands in voiced speech
    tone(0.0, 3.0, 0.30)
    keep = [(0.0, 1.0), (1.4, 3.0)]
    rep = {}
    out = C.refine_boundaries(sm, rate, ch, keep, report=rep)
    check("cut through loud speech is abandoned", rep["abandoned"] == 1, str(rep))
    check("abandoning rejoins the ranges", len(out) == 1, str(out))

    # now make the junction land in silence: the cut should be taken
    for f in range(int(0.95 * rate), int(1.45 * rate)):
        sm[f] = 0
    rep = {}
    out = C.refine_boundaries(sm, rate, ch, keep, report=rep)
    check("cut through silence is taken", rep["abandoned"] == 0, str(rep))
    check("taken cut leaves two ranges", len(out) == 2, str(out))


def test_duck_fills_with_room_tone():
    """Ducking replaces a filler with the ROOM, not with silence.

    Attenuating to a gain floor takes the room tone down with the voice, so the
    noise floor drops out and returns — audible precisely because the rest of
    the track has a floor. Reported from the delivered videos as the mute being
    obvious. The span is now crossfaded to a bed lifted from the quietest part
    of the same recording, matched to the local floor.
    """
    import array
    import math as _m
    import random
    rate, ch = 48000, 1
    n = rate * 3
    rnd = random.Random(7)
    # room tone everywhere, speech only in the middle second
    sm = array.array("h", [int(rnd.gauss(0, 300)) for _ in range(n)])
    for f in range(rate, 2 * rate):
        sm[f] = max(-32768, min(32767,
                   sm[f] + int(0.3 * 32767 * _m.sin(2 * _m.pi * 150 * f / rate))))

    def rms(a, b):
        seg = sm[int(a * rate):int(b * rate)]
        return (sum(v * v for v in seg) / max(1, len(seg))) ** 0.5

    floor_before = rms(0.2, 0.6)
    speech_before = rms(1.2, 1.4)
    done = C.duck_spans(sm, rate, ch, [[1.3, 1.6]], [(0.0, 3.0)])
    check("one span filled", done == 1, str(done))
    check("length unchanged", len(sm) == n)
    filled = rms(1.35, 1.55)
    check("voice removed from the span", filled < speech_before * 0.15,
          f"{filled:.0f} vs speech {speech_before:.0f}")
    check("room tone preserved, not muted",
          floor_before * 0.4 < filled < floor_before * 2.5,
          f"filled {filled:.0f} vs room floor {floor_before:.0f}")


def test_duck_leaves_timeline_intact():
    """Ducking does not cut, so picture and sound cannot drift."""
    import array
    import math as _m
    rate, ch = 48000, 1
    n = rate * 2
    sm = array.array("h", [int(0.3 * 32767 * _m.sin(2 * _m.pi * 150 * f / rate))
                           for f in range(n)])
    before = len(sm)
    keep = [(0.0, 2.0)]
    done = C.duck_spans(sm, rate, ch, [[0.8, 1.2]], keep)
    check("one span ducked", done == 1, str(done))
    check("length unchanged", len(sm) == before, f"{len(sm)} vs {before}")

    def rms(a, b):
        seg = sm[int(a * rate):int(b * rate)]
        return (sum(v * v for v in seg) / max(1, len(seg))) ** 0.5

    check("voice removed from the span", rms(0.98, 1.02) < rms(0.2, 0.4) * 0.1,
          f"{rms(0.98, 1.02):.0f} vs {rms(0.2, 0.4):.0f}")
    check("audio outside the span untouched", rms(0.2, 0.4) > 6000,
          f"{rms(0.2, 0.4):.0f}")

    # a span already cut out must not be ducked
    sm2 = array.array("h", [10000] * n)
    check("span outside the keep-ranges is skipped",
          C.duck_spans(sm2, rate, ch, [[0.8, 1.2]], [(1.5, 2.0)]) == 0)


def test_verbatim_priming_is_default():
    """Whisper drops most disfluency unless primed for it: 32 hard fillers
    found versus 80 on the same audio. Finding disfluency is the point here."""
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write("# a comment\nFablie\nTabl\n")
        lex = f.name
    p = T.load_lexicon(lex)
    check("verbatim preamble present by default", "um, uh, er" in p, p[:60])
    check("brand terms survive alongside it", "Fablie" in p, p[:80])
    check("prompt stays inside Whisper's limit", len(p) <= 700, str(len(p)))
    check("verbatim can be turned off",
          "um, uh, er" not in T.load_lexicon(lex, verbatim=False))


def test_duck_rejects_bad_spans():
    """Priming for disfluency also invites the occasional invention."""
    import array
    import math as _m
    rate, ch = 48000, 1
    sm = array.array("h", [int(0.3 * 32767 * _m.sin(2 * _m.pi * 150 * f / rate))
                           for f in range(rate * 2)])
    keep = [(0.0, 2.0)]
    check("span too short to be an 'um' is skipped",
          C.duck_spans(sm, rate, ch, [[0.8, 0.83]], keep) == 0)
    silent = array.array("h", [0] * (rate * 2))
    check("span with nothing audible in it is skipped",
          C.duck_spans(silent, rate, ch, [[0.8, 1.1]], keep) == 0)
    check("a real span is still ducked",
          C.duck_spans(sm, rate, ch, [[0.8, 1.1]], keep) == 1)


# -------------------------------------------------------------- stopwords

def test_low_conf_ignores_function_words():
    ws = words([("x", 0, .2, .9), ("and", .3, .5, .02), ("mumble", .6, 1.0, .10),
                ("y", 1.1, 1.3, .9)])
    kinds = {(f["text"], f["kind"]) for f in A.detect(ws, [], lexicon_terms=[])}
    check("low_conf ignores function words", ("and", "low_conf") not in kinds, str(kinds))
    check("low_conf catches real mumbles", ("mumble", "low_conf") in kinds, str(kinds))


if __name__ == "__main__":
    for fn in [test_stdlib_only, test_brand_detector, test_edl_never_widens,
               test_phrase_repeats, test_edl_actually_excises,
               test_edl_stutters_and_fillers, test_frame_snapping,
               test_seam_gate_abandons_unhideable_cuts,
               test_duck_leaves_timeline_intact, test_duck_fills_with_room_tone,
               test_verbatim_priming_is_default,
               test_duck_rejects_bad_spans,
               test_low_conf_ignores_function_words]:
        print(f"\n{fn.__name__}")
        fn()
    print(f"\n{'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAILED: ' + ', '.join(FAILS)}")
    sys.exit(1 if FAILS else 0)
