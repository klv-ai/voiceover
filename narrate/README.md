# narrate

The pipeline behind the studio. Two layers:

- **`narrate/`** — the core, standard-library Python that shells out to
  ffmpeg and Whisper: transcription with verbatim priming, detection of
  fillers, stutters and gaps, the mastering chain, and sample-exact audio
  cutting. It runs under any `python3` with no install.
- **`lab/`** — the scripts the studio runs for everything else. They need
  numpy, and the voice scripts need the voice environment (torch, VoxCPM).

Environments, models and install: see the README at the repo root.

## The command line

```bash
bin/narrate analyze demo.mov       # → demo.analysis.json and a report
bin/narrate master demo.mp4 -o demo.final.mp4
bin/narrate doctor                 # what is installed
```

`analyze` transcribes with Whisper primed to keep disfluency (unprimed, it
drops most "um"s), then finds fillers, restarts, low-confidence words and long
gaps. `--lexicon` primes it with your product names; the studio passes the
terms from its settings.

## lab/ — what the studio runs

**Script**

| | |
|---|---|
| `repair_dsl.py` | cut the transcript into lines, one per sentence, anchored to the picture |
| `clean_script.py` | rewrite improvised speech into sentences (LLM, via Ollama) |
| `consistency_pass.py` | make the rewritten lines read as one piece |
| `polish_script.py` | line-by-line suggestions that learn from your own past edits; never applied without you |
| `read_script.py` | write passages for a voice to read, for training |

**Lines**

| | |
|---|---|
| `synth_one.py` | the synthesis worker: generate a line, trim it, tone, ending, pace |
| `tts_provider.py` | VoxCPM: a LoRA fine-tune and/or a sample of the voice |
| `verify_take.py` | the take-checking worker: transcribe a take and compare it with the script |
| `trim_tail.py` | cut a take at the end of its last word |
| `whisper_any.py` | Whisper on whatever the machine has (mlx on Apple Silicon, openai-whisper elsewhere) |
| `prosody.py`, `psola.py` | pitch tracking; a falling close for a line that ends rising |

**Voices**

| | |
|---|---|
| `cut_reads.py` | turn recorded reads into clips with exact transcripts |
| `train_voice.py` | cut the reads and run VoxCPM's LoRA fine-tune |
| `compare_ckpts.py` | score checkpoints on the faults that happen: wrong words, runaway tails, wandering pitch |
| `build_sample.py`, `score_samples.py` | find the sample the model is handed on every line, by what the clone makes of it |
| `scrub.py`, `clicks.py` | take the clicks out of a sample; count them |
| `measure_audio.py` | length, level, noise floor and holes of a wav |

**Picture and sound**

| | |
|---|---|
| `join.py` | join several recordings into one video |
| `assemble.py` | lay the takes into one track; clean the background |
| `glide.py` | close dead air by speeding the picture through it; cuts; blur masks |
| `detear.py` | repair screen tearing in a recording |
| `captions.py` | subtitles from the script and the render's timings |
| `render_repair.py` | wav helpers shared by the above |

Each script's docstring explains why it does what it does, including the
measurements behind its thresholds. Read it before changing one.

## Tests

```bash
python3 tests/check.py
```

Covers the standard-library rule, the brand-name detector (it needs a system
word list, `/usr/share/dict/words`), the cut invariants, frame snapping,
filler ducking and verbatim priming.
