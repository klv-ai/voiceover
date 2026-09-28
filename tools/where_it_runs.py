#!/usr/bin/env python3
"""Is the model ACTUALLY on the card, or just asked to be?

`VoxCPM.from_pretrained(device="cuda")` is a request, not a receipt. A wrapper
that takes the argument and drops it, a submodule built before the move, a
component that constructs its own tensors on CPU — each leaves a model that
reports the right device at the top and runs on the processor underneath. The
symptom is the honest one the operator noticed: the card never spins up.

So do not ask the model where it is. Count where its weights are, watch the
card while it works, and time the same line with the card taken away. If
forcing CPU costs nothing, it was never on the GPU.

    python3 tools/where_it_runs.py --ref voice.wav --text "a line to say"
    python3 tools/where_it_runs.py --ref voice.wav --lora CHECKPOINT_DIR
    python3 tools/where_it_runs.py --ref voice.wav --cpu-control   # also time on CPU
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "narrate", "lab"))

LINE = ("These are long-form documents, and the point of the feature is that "
        "you never have to read one twice.")


def say(k, v):
    print(f"  {k:26} {v}", flush=True)


# ---------------------------------------------------------------- the card

def sysfs_sample():
    """Use%, watts and VRAM straight out of sysfs.

    rocm-smi is a separate package and is not installed on this box — which is
    part of why a card that had stopped working went unnoticed for a whole
    render. The kernel driver exports the same numbers with no package at all,
    so read those and never depend on a tool being present to find out whether
    the GPU is doing anything.
    """
    import glob
    for dev in sorted(glob.glob("/sys/class/drm/card*/device")):
        try:
            use = float(open(dev + "/gpu_busy_percent").read().strip())
        except Exception:
            continue
        watts = None
        for f in glob.glob(dev + "/hwmon/hwmon*/power1_average"):
            try:
                watts = float(open(f).read().strip()) / 1e6   # microwatts
                break
            except Exception:
                pass
        vram = None
        try:
            vram = float(open(dev + "/mem_info_vram_used").read().strip()) / 2**30
        except Exception:
            pass
        return (dev.split("/")[4], use, watts, vram)
    return None


def smi_sample():
    """One reading of use% and watts, or None."""
    r = sysfs_sample()
    if r:
        return r
    try:
        out = subprocess.run(["rocm-smi", "--showuse", "--showpower", "--json"],
                             capture_output=True, text=True, timeout=10)
        d = json.loads(out.stdout)
    except Exception:
        return None
    for card, fields in d.items():
        if not isinstance(fields, dict):
            continue
        use = watts = None
        for k, v in fields.items():
            kl = k.lower()
            try:
                f = float(str(v).split()[0])
            except Exception:
                continue
            if "use" in kl and "gpu" in kl:
                use = f
            elif "power" in kl and watts is None:
                watts = f
        if use is not None or watts is not None:
            return (card, use, watts, None)
    return None


class Watch:
    """Sample the card while something else runs. Idle is measured FIRST and
    reported beside the busy figure, because a card at 4% and 38W during
    generation means nothing until you know it sits at 4% and 38W doing
    nothing at all."""

    def __init__(self):
        self.on = False
        self.rows = []
        self.idle = smi_sample()

    def _loop(self):
        while self.on:
            r = smi_sample()
            if r:
                self.rows.append(r)
            time.sleep(0.4)

    def __enter__(self):
        self.on = True
        self.t = threading.Thread(target=self._loop, daemon=True)
        self.t.start()
        return self

    def __exit__(self, *a):
        self.on = False
        self.t.join(timeout=3)

    def report(self):
        if self.idle:
            _, u, w, v = self.idle
            say("idle", f"{u if u is not None else '?'}% use, "
                        f"{w if w is not None else '?'} W, "
                        f"{v if v is not None else '?'} GiB VRAM")
        if not self.rows:
            print("  (rocm-smi said nothing — not installed, or no card here)")
            return
        uses = [u for _, u, _, _ in self.rows if u is not None]
        watts = [w for _, _, w, _ in self.rows if w is not None]
        vram = [v for _, _, _, v in self.rows if v is not None]
        card = self.rows[0][0]
        say("card", card)
        if uses:
            say("use while generating", f"peak {max(uses):.0f}%, "
                                        f"mean {sum(uses)/len(uses):.0f}%  "
                                        f"({len(uses)} samples)")
        if watts:
            say("power while generating", f"peak {max(watts):.0f} W, "
                                          f"mean {sum(watts)/len(watts):.0f} W")
        if vram:
            say("VRAM held", f"peak {max(vram):.1f} GiB")


# ------------------------------------------------------------- the weights

def census(mod):
    """How many parameters sit on each device."""
    c = {}
    for _, t in list(mod.named_parameters()) + list(mod.named_buffers()):
        c[str(t.device)] = c.get(str(t.device), 0) + t.numel()
    return c


def find_modules(obj, depth=3):
    """Every nn.Module hanging off a plain wrapper object.

    VoxCPM is not itself an nn.Module — it holds them. Walking the attributes
    is what makes a component that missed the move visible, which asking the
    wrapper never would."""
    import torch.nn as nn
    found, seen = [], set()

    def walk(o, path, d):
        if d > depth or id(o) in seen:
            return
        seen.add(id(o))
        for name, val in vars(o).items():
            if name.startswith("__"):
                continue
            p = f"{path}.{name}" if path else name
            if isinstance(val, nn.Module):
                found.append((p, val))
                walk(val, p, d + 1)
            elif hasattr(val, "__dict__") and not isinstance(val, (str, bytes)):
                walk(val, p, d + 1)

    walk(obj, "", 0)
    return found


def report_weights(obj):
    mods = find_modules(obj)
    if not mods:
        print("  (found no torch modules on this object)")
        return
    # Top-level first: the deepest tree is noise once the roots agree.
    roots, seen_ids = [], set()
    for path, m in mods:
        if any(path.startswith(r + ".") for r, _ in roots):
            continue
        if id(m) in seen_ids:
            continue
        seen_ids.add(id(m))
        roots.append((path, m))

    total = {}
    for path, m in roots:
        c = census(m)
        if not c:
            continue
        for d, n in c.items():
            total[d] = total.get(d, 0) + n
        where = ", ".join(f"{d}: {n/1e6:.0f}M" for d, n in sorted(c.items()))
        say(path, where)
    print()
    grand = sum(total.values()) or 1
    for d, n in sorted(total.items(), key=lambda x: -x[1]):
        say(f"TOTAL on {d}", f"{n/1e6:.0f}M parameters  ({100*n/grand:.0f}%)")
    if any(d.startswith("cpu") for d in total) and len(total) > 1:
        print("\n  Split across devices. Every boundary crossing is a copy, and a"
              "\n  small component left on the CPU in an autoregressive loop is"
              "\n  paid once per step, not once per line.")
    elif list(total) == ["cpu"]:
        print("\n  ENTIRELY ON THE CPU. This is the answer.")


# ------------------------------------------------------------------- timing

def time_one(fn, label):
    t0 = time.time()
    y = fn()
    dt = time.time() - t0
    secs = len(y) / 48000
    say(label, f"{dt:.1f}s for {secs:.1f}s of audio  ({dt/max(secs,.01):.1f}x realtime)")
    return dt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", required=True)
    ap.add_argument("--text", default=LINE)
    ap.add_argument("--lora", default=None, help="a LoRA checkpoint directory")
    ap.add_argument("--cpu-control", action="store_true",
                    help="time the same line with the card hidden — the decisive test")
    a = ap.parse_args()

    import torch
    import tts_provider

    print("== torch")
    hip = getattr(torch.version, "hip", None)
    say("build", f"ROCm {hip}" if hip else
                 (f"CUDA {torch.version.cuda}" if torch.version.cuda else "CPU ONLY"))
    say("cuda.is_available()", torch.cuda.is_available())
    say("devices", torch.cuda.device_count())
    for i in range(torch.cuda.device_count()):
        p = torch.cuda.get_device_properties(i)
        say(f"cuda:{i}", f"{p.name}  {getattr(p, 'gcnArchName', '?')}")
    for v in ("HIP_VISIBLE_DEVICES", "CUDA_VISIBLE_DEVICES", "HSA_OVERRIDE_GFX_VERSION"):
        if os.environ.get(v):
            say(v, os.environ[v])
    say("tts_provider asks for", tts_provider._device())

    print("\n== loading")
    t0 = time.time()
    opts = {"lora": a.lora} if a.lora else {}
    tts_provider.generate(a.text, a.ref, seed=1, **opts)
    say("first call", f"{time.time() - t0:.1f}s  (load + one line)")

    obj = tts_provider._vox
    print("\n== where the weights actually are")
    report_weights(obj)

    print("\n== the card, while it generates")
    w = Watch()
    with w:
        warm = time_one(lambda: tts_provider.generate(a.text, a.ref, seed=2, **opts), "warm")
    w.report()

    if a.cpu_control:
        # Decisive: hide the card and reload. If this costs the same, the card
        # was never doing the work.
        print("\n== control: same line, card hidden")
        os.environ["HIP_VISIBLE_DEVICES"] = ""
        os.environ["CUDA_VISIBLE_DEVICES"] = ""
        tts_provider._vox = None
        print("  (reloading on CPU — this may take minutes)")
        tts_provider.generate(a.text, a.ref, seed=3, **opts)
        cpu = time_one(lambda: tts_provider.generate(a.text, a.ref, seed=4, **opts), "cpu warm")
        print()
        if cpu < warm * 1.25:
            say("VERDICT", "the card is NOT doing the work — CPU is as fast")
        else:
            say("VERDICT", f"the card is {cpu/warm:.1f}x faster — it is being used")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
