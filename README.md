# Voiceover

Narration for screen recordings, in your own voice, without re-recording.

Record a product walkthrough the way you normally would — ums, restarts,
long pauses while something loads. Voiceover transcribes it, helps you turn
what you said into a clean script, speaks that script in a voice cloned from
your own reads, lays each line back over the picture, glides the video
through the dead air, and masters a finished video. Everything runs on your
own machine.

```
studio/   the web app you work in (SvelteKit)
narrate/  the pipeline it drives (Python): transcription, synthesis, mixing, rendering
tools/    diagnostics: GPU checks, project inspection, moving a data folder
```

## How a video is made

1. **New project.** Upload the recording — or several, if it was recorded in
   sittings; they are joined in the order you set. The studio transcribes it
   and splits it into lines.
2. **Script.** *Rewrite* turns improvised speech into sentences; *Polish*
   suggests tighter lines, which you accept or ignore one at a time. Your own
   edits are never overwritten.
3. **Voice.** Pick a voice from the Voice lab.
4. **Render lines.** Each line is generated, transcribed back and checked
   against the script, and generated again if it came out wrong. A preview
   builds when it finishes.
5. **Final video.** The picture glides through the silences between lines,
   the sound is mastered, captions come with it.

Along the way, on the timeline: nudge and stretch lines, add a line over
dead air, slice and delete sections, merge lines, blur parts of the screen for
a stretch of time, and keep any line as the original recording.

**A voice** is made once, in the Voice lab: name it, read the scripts it
writes for you (half an hour or so of reading), train it — a LoRA fine-tune of
[VoxCPM](https://github.com/OpenBMB/VoxCPM) — score the checkpoints and
promote the best, then pick the short sample the model is handed on every line.

## Requirements

- **Linux with a GPU** is what this is built and tested on: Ubuntu 24.04, an
  AMD RX 7900 GRE (16 GB) under ROCm. An NVIDIA card with CUDA should work the
  same way. A CPU works but is about ten times slower per line. On Apple
  Silicon the transcription and the rest of the pipeline run; the voice model
  there is untested.
- **16 GB of GPU memory.** The voice model takes about 10 GB while it speaks;
  the studio unloads Ollama's models before it starts.
- **Disk:** about 15 GB of models (VoxCPM2 4.7 GB, Whisper large-v3-turbo
  1.6 GB, gemma4:12b 7.6 GB), plus a few GB per video.
- Python 3.12, Node 22, ffmpeg 6, [Ollama](https://ollama.com), git.

## Install (Linux)

**1. System packages.** `wamerican` is the word list the transcript checker
uses to tell a product name from an ordinary word.

```bash
sudo apt install ffmpeg python3-venv git wamerican
```

Install Node 22 from [nodejs.org](https://nodejs.org) or your package manager,
and [Ollama](https://ollama.com/download), then pull the default writing model:

```bash
ollama pull gemma4:12b
```

**2. Get the code.**

```bash
git clone https://github.com/klv-ai/voiceover.git && cd voiceover
```

**3. The analysis environment** — small; the pipeline's core is standard-library
Python.

```bash
python3 -m venv narrate/.venv
narrate/.venv/bin/pip install -r narrate/requirements.txt
```

**4. The voice environment** — torch and the voice model. Install torch for
your GPU first, then the rest:

```bash
python3 -m venv narrate/.venv-tts
# AMD (ROCm):
narrate/.venv-tts/bin/pip install torch==2.6.0 torchaudio==2.6.0 --index-url https://download.pytorch.org/whl/rocm6.2.4
# NVIDIA (CUDA):   ... --index-url https://download.pytorch.org/whl/cu124
# CPU only:        ... --index-url https://download.pytorch.org/whl/cpu
narrate/.venv-tts/bin/pip install -r narrate/requirements-tts.txt
```

Transcription runs Whisper through the `whisper` command, which the voice
environment provides; put it on the PATH:

```bash
sudo ln -s "$PWD/narrate/.venv-tts/bin/whisper" /usr/local/bin/whisper
```

On AMD, your user needs the `render` and `video` groups
(`sudo usermod -aG render,video $USER`, then log in again).

**5. For training voices**, a checkout of VoxCPM's own fine-tuning scripts:

```bash
git clone https://github.com/OpenBMB/VoxCPM.git ~/VoxCPM-src   # or set VOXCPM_SRC
```

**6. The studio.**

```bash
cd studio && npm ci && npm run build
PORT=5199 npm start          # http://localhost:5199
```

**7. Check it.**

```bash
narrate/.venv-tts/bin/python3 tools/check_gpu.py --generate   # the GPU, and one spoken line
(cd narrate && bin/narrate doctor)                            # ffmpeg, Whisper, RNNoise
```

The first render downloads the VoxCPM2 weights, and the first transcription
downloads Whisper's.

### Running it as a service

`docs/voiceover.service` is a systemd unit for the built studio. Copy it to
`/etc/systemd/system/`, set the user and paths, then
`sudo systemctl enable --now voiceover`.

For development, `cd studio && npm run dev` serves it on port 5199 with hot
reload.

## Configuration

**Settings** (in the studio's header) holds what belongs to your videos:

- the **writing model** new projects rewrite and polish with — any model
  Ollama runs; `gemma4:12b` by default. A cloud model is faster, and sends
  your transcripts off the machine.
- **your words:** product and place names (primed into the transcriber and
  handed to the rewriters), coined names and what the transcriber writes
  instead of them, names that must be said exactly, and the lines voices are
  tested with.

Environment variables, all optional:

| variable | default | what |
|---|---|---|
| `HOST` | `127.0.0.1` | address the built studio listens on (`npm start`) |
| `PORT` | `3000` | its port |
| `STUDIO_DATA` | `studio/data` | projects, media, voices, settings |
| `NARRATE_DIR` | `../narrate` from the studio | the pipeline |
| `NARRATE_PY` / `NARRATE_PY_TTS` | `narrate/.venv…/bin/python3` | the two environments |
| `OLLAMA_URL` | `http://localhost:11434` | Ollama |
| `VOXCPM_SRC` | `~/VoxCPM-src` | VoxCPM checkout, for training |
| `HF_HOME` / `HF_HUB_CACHE` | Hugging Face defaults | where the VoxCPM2 weights are cached |

## Security

The studio has **no login**. It is meant to run on your own machine and
listens on `127.0.0.1` by default. Anyone who can reach its port can read
your projects and recordings and start jobs on your GPU, so only set
`HOST=0.0.0.0` on a network you trust, and put it behind your own
authentication before exposing it any further.

Two consequences of running it locally: requests from other websites are
refused, and the browser only allows the microphone (for recording reads) on
`localhost` or HTTPS. To record on a studio running on another machine, open
it through an SSH tunnel: `ssh -L 5199:localhost:5199 you@host`, then
`http://localhost:5199`.

## Responsible use

This clones voices. Clone only your own voice, or a voice whose owner has
agreed to it, and say so where it matters. The recordings, reads and trained
voices stay in `studio/data` on your machine; nothing is uploaded unless you
choose a cloud writing model, which receives transcripts.

## Your data

`studio/data` holds everything a project is — the source video, the takes,
the renders, the voices and their training runs — and none of it is in git.
Deleting a project or a voice moves it to `studio/data/trash`; empty that
yourself.

Project files store **absolute paths**. After moving the checkout to another
folder or machine, run `python3 tools/relocate.py` to see what it would
rewrite, then `--write` to rewrite it.

## Tools

| | |
|---|---|
| `tools/check_gpu.py` | is there a usable GPU, and can it speak a line (`--generate`) |
| `tools/where_it_runs.py` | is the voice model really on the GPU, timed against the CPU |
| `tools/relocate.py` | rewrite a project library's paths after moving it |
| `tools/render_state.py` | what a project still needs rendered, and why |
| `tools/inspect_takes.py` | length and level of a project's takes |
| `tools/find_damaged.py` | takes too short to hold their line |

## Licence

Apache-2.0 — see `LICENSE`. The models it runs and downloads have their own
terms; see `NOTICE`.
