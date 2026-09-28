#!/usr/bin/env python3
"""Will this machine actually run the synthesis?

Written for an AMD box, because that is the question that matters here. On
ROCm, PyTorch keeps the CUDA spelling — `torch.cuda.is_available()` returns
True, devices are "cuda:0" — so the usual check tells you nothing about
whether you are on ROCm, which card it picked, or whether that card is one
ROCm actually supports. `torch.version.hip` is the tell.

Two things specific to this machine:

  * It has two DIFFERENT AMD architectures in it. RDNA3 (RX 7900, gfx1100) has
    been supported by ROCm for years; RDNA4 (RX 9070, gfx120x) only since ROCm
    6.4. Torch will happily pick either, and picking the newer one is how you
    end up debugging the toolchain instead of the audio. Pin the 7900 with
    HIP_VISIBLE_DEVICES until it is working.
  * An unsupported-but-close card often needs HSA_OVERRIDE_GFX_VERSION to make
    ROCm treat it as its nearest supported sibling. If the tensor test below
    fails with "invalid device function", that is the knob.

    python3 tools/check_gpu.py              # what is here, and can it do maths
    python3 tools/check_gpu.py --generate   # also load VoxCPM and say a line
"""
import argparse
import os
import subprocess
import sys
import time


def line(k, v):
    print(f"  {k:24} {v}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--generate", action="store_true",
                    help="load VoxCPM and synthesise one line (slow, honest)")
    a = ap.parse_args()

    print("== machine")
    for cmd, label in (("rocm-smi --showproductname", "rocm-smi"),
                       ("nvidia-smi -L", "nvidia-smi")):
        try:
            out = subprocess.run(cmd.split(), capture_output=True, text=True, timeout=20)
            if out.returncode == 0 and out.stdout.strip():
                print(f"  {label}:")
                for l in out.stdout.strip().splitlines()[:12]:
                    print(f"    {l.strip()}")
        except Exception:
            pass

    print("\n== torch")
    try:
        import torch
    except ImportError:
        print("  torch is not installed in this environment.")
        print("  ROCm build:  pip install torch --index-url https://download.pytorch.org/whl/rocm6.2")
        return 1

    hip = getattr(torch.version, "hip", None)
    line("version", torch.__version__)
    line("build", f"ROCm {hip}" if hip else (f"CUDA {torch.version.cuda}" if torch.version.cuda else "CPU only"))
    if not hip and not torch.version.cuda:
        print("\n  This is a CPU-only torch. Synthesis will run, extremely slowly.")
        return 1

    n = torch.cuda.device_count()
    line("devices", n)
    if not n:
        print("\n  Torch is a GPU build but sees no device. On ROCm that is usually")
        print("  the card being unsupported, or the user not being in the `render`")
        print("  and `video` groups.")
        return 1

    for i in range(n):
        p = torch.cuda.get_device_properties(i)
        arch = getattr(p, "gcnArchName", "?")
        line(f"cuda:{i}", f"{p.name}  {arch}  {p.total_memory / 2**30:.0f} GiB")

    print("\n== can each device do arithmetic")
    ok = []
    for i in range(n):
        try:
            t0 = time.time()
            x = torch.randn(2048, 2048, device=f"cuda:{i}")
            y = (x @ x).sum().item()
            torch.cuda.synchronize(i)
            assert y == y            # NaN would mean it computed rubbish
            line(f"cuda:{i}", f"ok, {time.time() - t0:.2f}s")
            ok.append(i)
        except Exception as e:
            line(f"cuda:{i}", f"FAILED — {type(e).__name__}: {str(e)[:90]}")

    if not ok:
        print("\n  No device can multiply two matrices. Try:")
        print("    HSA_OVERRIDE_GFX_VERSION=11.0.0 python3 check_gpu.py")
        return 1

    # Prefer the OLDEST supported architecture, which is the opposite of the
    # usual instinct and correct here: gfx1100 is years of ROCm releases deep,
    # gfx120x is months.
    def rank(i):
        arch = getattr(torch.cuda.get_device_properties(i), "gcnArchName", "")
        return (0 if "gfx110" in arch else 1, i)
    pick = sorted(ok, key=rank)[0]
    arch = getattr(torch.cuda.get_device_properties(pick), "gcnArchName", "?")
    print(f"\n  use cuda:{pick} ({torch.cuda.get_device_properties(pick).name}, {arch})")
    print(f"  pin it with:  export HIP_VISIBLE_DEVICES={pick}")

    if not a.generate:
        print("\n  Re-run with --generate to prove the voice model itself works.")
        return 0

    print("\n== voxcpm")
    try:
        from voxcpm import VoxCPM
    except ImportError:
        print("  voxcpm is not installed in this interpreter (pip install voxcpm)")
        return 1
    os.environ.setdefault("HIP_VISIBLE_DEVICES", str(pick))
    t0 = time.time()
    m = VoxCPM.from_pretrained("openbmb/VoxCPM2", device="cuda")
    line("loaded in", f"{time.time() - t0:.1f}s")
    t0 = time.time()
    wav = m.generate(text="This machine can generate speech, which is the only thing "
                          "worth proving before moving anything onto it.")
    dt = time.time() - t0
    sr = (getattr(m, "sample_rate", None)
          or getattr(getattr(m, "tts_model", None), "sample_rate", None) or 16000)
    secs = len(wav) / sr
    line("generated", f"{secs:.1f}s of audio in {dt:.1f}s  ({dt / secs:.2f}x realtime)")
    print("\n  Working.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
