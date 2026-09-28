#!/usr/bin/env python3
"""Point a project library at wherever it now lives.

Every take, every read, every source video and every voice reference is
recorded in the project files as an ABSOLUTE path — about 570 of them in a
143-line project. That is fine while nothing moves and catastrophic the moment
anything does: copy the library to another disk, another directory or another
machine and the studio opens six projects with every line orphaned, no error,
just silence where the audio was.

So this is the move. It rewrites the stored paths to match where the files
actually are now, and it is the first thing to run after relocating the
checkout — including onto a Linux box with a real GPU, which is the whole
reason it exists.

    python3 tools/relocate.py           # say what would change
    python3 tools/relocate.py --write   # change it
"""
import argparse
import glob
import json
import os
import re
import sys

# The repo root: this file lives in tools/.
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STUDIO = os.path.join(HERE, "studio")
NARRATE = os.path.join(HERE, "narrate")

# Any absolute path ending in one of the two project directory names, however
# they were spelled on the machine that wrote them. Matching on the TAIL rather
# than on a known prefix is what lets this work for a library that has already
# moved twice, or arrived from somebody else's home directory.
TAIL = re.compile(r'(/(?:[^"\s:]+/)*?)(experts-narrate-studio|experts-narrate|studio|narrate)(?=/)')


def remap(text: str) -> tuple[str, int]:
    n = 0

    def sub(m):
        nonlocal n
        prefix, name = m.group(1), m.group(2)
        want = STUDIO if name in ("experts-narrate-studio", "studio") else NARRATE
        have = prefix + name
        if have == want:
            return have
        n += 1
        return want

    return TAIL.sub(sub, text), n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="apply, rather than report")
    a = ap.parse_args()

    targets = (sorted(glob.glob(os.path.join(STUDIO, "data/projects/*.json")))
               + sorted(glob.glob(os.path.join(STUDIO, "data/voices/*/voice.json"))))
    if not targets:
        print("no project or voice files found — is this the right checkout?", file=sys.stderr)
        return 1

    total, touched, missing = 0, 0, 0
    for f in targets:
        s = open(f).read()
        t, n = remap(s)
        if n:
            touched += 1
            total += n
            if a.write:
                open(f, "w").write(t)
        # Report what is STILL not there afterwards. A rewrite that produces
        # tidy paths to files which do not exist is the failure worth catching,
        # and it is invisible unless somebody looks.
        for p in set(re.findall(r'"(/[^"]+\.(?:wav|mp4|mov|m4v))"', t if a.write or n else s)):
            if not os.path.exists(p):
                missing += 1
                if missing <= 6:
                    print(f"  still missing: {p}")

    verb = "rewrote" if a.write else "would rewrite"
    print(f"{verb} {total} paths across {touched} of {len(targets)} files")
    if missing:
        print(f"{missing} referenced file(s) do not exist — copy data/ across too")
    elif a.write:
        print("every referenced file is present")
    if not a.write and total:
        print("re-run with --write to apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
