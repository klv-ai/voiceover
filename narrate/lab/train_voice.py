#!/usr/bin/env python3
"""Everything between "I have read for thirty minutes" and "here are checkpoints".

Cut the reads into clips, fold them in with whatever was mined from finished
projects, hold a slice back, write a config, and train. One command, because
every one of these steps has been done by hand at least once and each hand
pass is a chance to sync the wrong file or point at the wrong corpus.

Two choices are baked in, both learned the hard way:

  * loss/stop is weighted. The first run here traded this speaker's timbre for
    its sense of an ending - loss/diff stood still while loss/stop TRIPLED -
    and a model that cannot stop appends noise to every line, puts ticks in
    the pauses, and says words nobody asked for.
  * the holdout is a whole SOURCE, not a random slice. Clips from one sitting
    are near-duplicates of each other, so a random split validates against
    material the model has effectively already seen, and the loss curve lies.

Prints one JSON line per phase so a UI can follow it.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import random
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


_PROGRESS = None


def say(**kw):
    """One destination, and a file when there is one.

    Reporting down a pipe only works while somebody is draining it, and the
    reader lives in a module that is replaced on every edit. When that
    happened a sibling script filled the OS buffer and BLOCKED - holding the
    GPU, reporting itself as running, for eighty-eight minutes. Writing to
    both destinations does not help: the pipe still fills.
    """
    line = json.dumps(kw)
    if _PROGRESS:
        _PROGRESS.write(line + "\n")
    else:
        print(line, flush=True)


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--voice", required=True, help="the voice directory")
    ap.add_argument("--base", required=True, help="the VoxCPM2 snapshot")
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--steps", type=int, default=600)
    ap.add_argument("--save-every", type=int, default=50)
    ap.add_argument("--stop-weight", type=float, default=3.0)
    # VoxCPM's own fine-tuning script, from a checkout of github.com/OpenBMB/VoxCPM:
    # $VOXCPM_SRC, or ~/VoxCPM-src.
    ap.add_argument("--trainer", default=os.path.join(
        os.path.expanduser(os.environ.get("VOXCPM_SRC") or "~/VoxCPM-src"),
        "scripts", "train_voxcpm_finetune.py"))
    ap.add_argument("--progress", help="write one JSON line per phase HERE")
    a = ap.parse_args()

    global _PROGRESS
    if a.progress:
        _PROGRESS = open(a.progress, "w", buffering=1)

    V = os.path.abspath(a.voice)
    sessions = sorted(glob.glob(os.path.join(V, "sessions", "*.wav")))
    script = os.path.join(V, "read_script.json")
    work = os.path.join(V, "t5-voxcpm")
    os.makedirs(work, exist_ok=True)

    # ---- 1. the reads become clips with exact transcripts -----------------
    rows = []
    if sessions and os.path.exists(script):
        say(phase="cutting", sessions=len(sessions))
        man = os.path.join(work, "read_manifest.jsonl")
        r = run([a.python, os.path.join(HERE, "cut_reads.py"),
                 "--script", script, "--sessions", *sessions,
                 "--out", os.path.join(V, "read_clips"), "--manifest", man])
        if r.returncode != 0:
            say(phase="failed", where="cutting", error=r.stderr[-600:])
            return 1
        try:
            say(phase="cut", **json.loads(r.stdout.strip().splitlines()[-1]))
        except Exception:
            pass
        if os.path.exists(man):
            rows += [json.loads(l) for l in open(man) if l.strip()]
    else:
        say(phase="cut", clips=0, why="no sessions or no script yet")

    # ---- 2. fold in whatever was mined from finished projects -------------
    mined = os.path.join(V, "manifest.jsonl")
    if os.path.exists(mined):
        for l in open(mined):
            if not l.strip():
                continue
            d = json.loads(l)
            p = d.get("audio") or ""
            if not os.path.isabs(p):
                p = os.path.join(V, p)
            if os.path.exists(p) and 1.2 <= float(d.get("seconds", 0)) <= 18:
                rows.append({"audio": p, "text": d.get("text", ""),
                             "seconds": d.get("seconds", 0),
                             "source": d.get("source", "mined"),
                             "passage": d.get("project", "mined")})
    rows = [r for r in rows if len((r.get("text") or "").split()) >= 3]
    read_min = sum(r["seconds"] for r in rows if r.get("source") == "read") / 60
    total_min = sum(r["seconds"] for r in rows) / 60
    say(phase="corpus", clips=len(rows), minutes=round(total_min, 1),
        read_minutes=round(read_min, 1))
    if len(rows) < 40:
        say(phase="failed", where="corpus",
            error="not enough material to train on yet")
        return 1

    # ---- 3. hold out a whole SOURCE, never a random slice -----------------
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(str(r.get("passage") or "mined"), []).append(r)
    keys = sorted(groups)
    random.Random(7).shuffle(keys)
    val, need = [], max(12, len(rows) // 12)
    for k in keys:
        if len(val) >= need:
            break
        val += groups[k]
    val_keys = {str(r.get("passage")) for r in val}
    train = [r for r in rows if str(r.get("passage")) not in val_keys]

    def dump(name, rs):
        p = os.path.join(work, f"{name}.jsonl")
        with open(p, "w") as f:
            for r in rs:
                f.write(json.dumps({"audio": r["audio"], "text": r["text"]}) + "\n")
        return p

    tp, vp = dump("train", train), dump("val", val)
    say(phase="split", train=len(train), val=len(val),
        held_out=sorted(val_keys)[:6])

    # ---- 4. a config, and a run directory that does not clobber ----------
    n = 1
    while os.path.exists(os.path.join(work, f"ckpt{n}" if n > 1 else "ckpt")):
        n += 1
    ck = os.path.join(work, f"ckpt{n}" if n > 1 else "ckpt")
    logs = os.path.join(work, f"logs{n}" if n > 1 else "logs")
    cfg = os.path.join(work, f"lora{n}.yaml")
    open(cfg, "w").write(f"""pretrained_path: {a.base}
train_manifest: {tp}
val_manifest: {vp}
sample_rate: 16000
out_sample_rate: 48000
batch_size: 1
grad_accum_steps: 16
num_workers: 4
max_batch_tokens: 4096
num_iters: {a.steps}
max_steps: {a.steps}
log_interval: 10
valid_interval: {a.save_every}
save_interval: {a.save_every}
learning_rate: 0.0001
weight_decay: 0.01
warmup_steps: 50
max_grad_norm: 1.0
save_path: {ck}
tensorboard: {logs}
lambdas:
  loss/diff: 1.0
  loss/stop: {a.stop_weight}
lora:
  enable_lm: true
  enable_dit: true
  enable_proj: false
  r: 32
  alpha: 32
  dropout: 0.0
""")
    say(phase="training", run=os.path.basename(ck), steps=a.steps, config=cfg)

    # ---- 5. train, streaming what the UI needs -----------------------------
    p = subprocess.Popen([a.python, a.trainer, "--config_path", cfg],
                         cwd=os.path.dirname(os.path.dirname(a.trainer)),
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, bufsize=1)
    for line in p.stdout or []:
        line = line.strip()
        if line.startswith("[train] step") or line.startswith("[val] step"):
            say(phase="step", line=line[:200])
    code = p.wait()
    steps = sorted(d for d in os.listdir(ck)) if os.path.exists(ck) else []
    say(phase="done" if code == 0 else "failed", run=os.path.basename(ck),
        checkpoints=[s for s in steps if s.startswith("step_")], code=code)
    return 0 if code == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
