# CLAUDE.md

Guidance for Claude Code when working in `narrate/`, the pipeline behind the
Voiceover studio (`../studio`).

## What this is

Two layers. `narrate/` (the package) is the core: transcription with verbatim
priming, disfluency detection, the mastering chain, and sample-exact audio
cutting, exposed as a CLI (`analyze`, `master`, `doctor`) and used as a
library. `lab/` holds the scripts the studio runs for everything else —
rewriting, synthesis with VoxCPM, take checking, voice training and sample
search, mixing, gliding, rendering. The README has the map of `lab/`.

## Hard constraint: the core stays dependency-free

`narrate/` is **stdlib only**. It shells out to `ffmpeg` and to a Whisper
binary. Do not add a Python import that is not in the standard library to any
module under `narrate/`. The point is that it runs under whatever `python3` is
on the machine, including a customer's, with no venv and no install step.

Heavy things live behind subprocess boundaries: Whisper, via `mlx_whisper`'s
Python API in whichever interpreter can import it, or the `whisper` CLI.

`lab/` is not bound by this: it runs in `.venv` (numpy) or `.venv-tts`
(torch, VoxCPM, openai-whisper) — `requirements.txt` and
`requirements-tts.txt`.

## Module map

| File | Role |
|------|------|
| `util.py` | subprocess, ffprobe, loudness/astats measurement, logging |
| `transcribe.py` | Whisper engines, model-name mapping, lexicon → initial_prompt |
| `analyze.py` | detectors, metrics, EDL construction |
| `cut.py` | sample-exact audio cutting and room tone, used by `lab/assemble.py` |
| `master.py` | spoken-word chain: one loudness measurement, a fixed gain, a true-peak limiter |
| `cli.py` | argparse dispatch, report rendering |

## Invariants worth not breaking

**Everything is measured on the source timeline.** `analyze` measures
timecodes against the source recording, and the studio's lines, cuts and
masks are all in source seconds. Anything that produces audio to lay against
the source must return it at exactly the source duration.

**Tempo belongs to `cut`, not `master`.** A tempo change is the one audio edit
that must also move the picture. `master` is a 1:1 transform by construction.

**A drop-list must actually cut.** `build_edl` walks kept words and breaks the
range wherever a dropped word sits between two of them. An earlier version used
the drop-set only to decide where gaps were measured, so a word was excised only
if removing it opened a gap wider than `gap_max` — 26 of 59 dropped words
survived in the audio while the report said they were gone. There are two
junction types and they take different pauses: `gap_keep` for collapsed silence,
`splice_pad` for a mid-sentence excision.

**Prime the decoder for disfluency.** Whisper deletes most fillers from the
transcript by default — 32 found versus 80 on the same audio, and four of five
videos reported zero. `VERBATIM_PREAMBLE` in `transcribe.py` fixes it. Do not
remove it to "clean up" the prompt; the tool exists to find these.

**Disfluency is n-gram shaped.** Single-word doubles were never the main
problem; two-word restarts are. Keep `repeat_drops` matching phrases, skipping
fillers between the copies, and keep `RHETORICAL` and the 0.6s recency guard —
without them it eats "very, very" and deliberate restatements.

**Denoise with `arnndn`, not `afftdn`.** Measured: +16 dB versus +0.2 dB on the
same sample. Do not "simplify" back to afftdn because it needs no model file.

**Compression stays off by default.** It is the single biggest contributor to
sounding processed. `--compress gentle|firm` exists for material that needs it.

**Never widen a pause.** `build_edl` pads each side of a collapsed gap by
`gap_keep/2` but caps that at 45% of the real gap, so the EDL can only ever
shorten silence.

**The brand-miss detector needs both filters.** A dictionary check alone misses
"Fable"; a confidence check alone misses "Fably" (the decoder is confidently
wrong). It suppresses dictionary words *unless* confidence is below the gate.
Removing either filter regresses it — the first version produced 21 false
positives on a 3-minute clip and buried the 4 real hits.

## ffmpeg landmines

See README "Landmines". All four produce silently wrong output rather than an
error, so they are easy to reintroduce:

* `aselect` is inert in ffmpeg 8.x — never cut audio with it.
* The source recordings are VFR. `setpts=N/FRAME_RATE/TB` assumes CFR and will
  silently compress the timeline (a 60s selection became 20.5s). Normalise with
  `fps=` before `select`.
* Snap EDL boundaries to the frame grid before cutting, or picture and sound
  accumulate drift at up to one frame per segment.
* `alimiter` needs `level=disabled` or it undoes `loudnorm`.
* `astats` is silent under `-v error`; use `-hide_banner` alone.
* Always map `0:v:0` / `0:a:0` — the source recordings have mjpeg thumbnail
  tracks at higher indices.
* Every measurement call needs `-map 0:a:0 -vn`. Without it `-f null` re-encodes
  the video stream and a six-second measurement takes minutes on 4K60.
* Measure loudness on the source, not on a mono extraction — a downmix reads
  ~3 LU quieter than what the platform will receive.

## Tried and rejected: WSOLA seam alignment

Sliding the resume point to maximise waveform correlation (the trick that makes
time-stretchers work) *degrades* these joins: click ratio p90 went 0.84 → 1.14
and the worst join crossed into audible. Two reasons. The 24 ms crossfade
already smooths any phase discontinuity, and cutting a word out joins genuinely
different phonemes — best correlation achieved was 0.33, so there is no phase
to align to. Worse, the search pulls the resume point off the level-matched
optimum, which is the thing that actually matters. Do not re-add it.

## Operational note

`bin/narrate` execs into `python3 -m narrate`, so the process command line never
contains "bin/narrate". `pkill -f "bin/narrate"` matches nothing and leaves a
stale run alive, competing for CPU with the one you just started — and, if you
edited the code in between, running the pre-edit version. Match on
`python3 -m narrate` instead.

## Testing

```bash
python3 tests/check.py     # no pytest — the core has no deps and neither does its test
```

Covers the stdlib-only invariant, the brand detector against every true and
false positive found on the real footage, "never widen a pause", stutter and
filler dropping, and the frame-grid arithmetic. Add a case here whenever a
detector is retuned — those thresholds were arrived at empirically and there is
nothing in the code that explains why 0.65 and 0.80 rather than one number.

Two of the six checks failed on first run because the *test* arithmetic was
wrong (`round(59.5)` is 60 under banker's rounding; and the pause a listener
hears is the padding either side of a junction, not the distance between ranges
on the source timeline). Worth knowing before trusting a failure here.

The brand detector needs a system word list (`/usr/share/dict/words`; on
Debian/Ubuntu the `wamerican` package). Without one, two checks fail.
