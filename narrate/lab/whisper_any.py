"""Transcribe on whatever this machine has.

mlx_whisper is Apple-only, and two files in this lab imported it directly. The
moment the pipeline moved to a Linux box with a GPU, both started failing — and
because the failure path in verify_take deliberately PASSES a take rather than
blocking a render, it did so invisibly. A whole video was rendered with the
text check silently disabled, which is precisely the net that catches a line
coming out as "Again" and stopping.

So there is one place to ask for a transcript now, and it falls back rather
than raising. openai-whisper runs on torch, so it finds CUDA or ROCm by itself.
"""
from __future__ import annotations

MLX_MODEL = "mlx-community/whisper-turbo"
TORCH_MODEL = "turbo"

_cached = None


def _torch_model():
    global _cached
    if _cached is None:
        import torch
        import whisper
        dev = "cuda" if torch.cuda.is_available() else "cpu"
        _cached = whisper.load_model(TORCH_MODEL, device=dev)
    return _cached


def transcribe(path: str, *, word_timestamps: bool = False,
               language: str | None = "en") -> dict:
    """Whisper's own result dict — `text`, and `segments` with `words` when
    asked. Shape is identical across both engines; mlx_whisper mirrors
    openai-whisper's API deliberately."""
    try:
        import mlx_whisper
        return mlx_whisper.transcribe(path, path_or_hf_repo=MLX_MODEL,
                                      word_timestamps=word_timestamps)
    except ImportError:
        pass
    return _torch_model().transcribe(path, word_timestamps=word_timestamps,
                                     language=language, verbose=False)


def engine() -> str:
    try:
        import mlx_whisper  # noqa: F401
        return "mlx_whisper"
    except ImportError:
        return "openai-whisper"
