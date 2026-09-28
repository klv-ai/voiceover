import { copyFileSync, existsSync, readFileSync, rmSync } from 'node:fs';
import { readFile, writeFile } from 'node:fs/promises';
import { join, basename, extname } from 'node:path';
import { CLI, NARRATE, PY, PY_TTS, OLLAMA } from './config';
import { writeLexicon } from './settings';
import { run, synthWorker, verifyWorker } from './run';
import { getVoice, latestOf } from './voices';
import type { Project, Segment } from '$lib/types';

const stem = (p: string) => basename(p, extname(p));

/** Whisper with verbatim priming, so the transcript SEES the disfluency. */
export async function analyze(source: string, work: string) {
  const r = await run(CLI, ['analyze', source, '--model', 'turbo', '--outdir', work,
                            '--cut-fillers', '--cut-stutters', '--lexicon', writeLexicon()],
                      { cwd: NARRATE });
  const path = join(work, `${stem(source)}.analysis.json`);
  if (!existsSync(path)) throw new Error(`analyze failed:\n${r.err.slice(-1500)}`);
  return path;
}

/** Tier 0 only: loudness, EQ, denoise. Video stream-copied. */
export async function master(source: string, work: string) {
  const out = join(work, `${stem(source)}.master.mp4`);
  const r = await run(CLI, ['master', source, '-o', out], { cwd: NARRATE });
  if (!existsSync(out)) throw new Error(`master failed:\n${r.err.slice(-1500)}`);
  return out;
}

/** One synthesis request per sentence, with the timing anchors. */
export async function buildScript(analysis: string, work: string, voiceRef: string) {
  const out = join(work, 'requests.json');
  const args = [join(NARRATE, 'lab/repair_dsl.py'), analysis, '-o', out, '--all'];
  if (voiceRef) args.push('--voice-ref', voiceRef);
  const r = await run(PY, args, { cwd: NARRATE });
  if (!existsSync(out)) throw new Error(`script build failed:\n${r.err.slice(-1500)}`);
  return JSON.parse(await readFile(out, 'utf8'));
}

/** Ask the LLM to turn improvised speech into speakable prose. */
export async function cleanScript(requestsPath: string, work: string, model: string) {
  const out = join(work, 'requests.clean.json');
  const r = await run(PY, [join(NARRATE, 'lab/clean_script.py'), requestsPath,
                           '-o', out, '--model', model, '--host', OLLAMA, '--lexicon', writeLexicon()],
                      { cwd: NARRATE, timeout: 900_000 });
  if (!existsSync(out)) throw new Error(`clean failed:\n${r.err.slice(-1500)}`);
  return JSON.parse(await readFile(out, 'utf8'));
}

export async function startSynth() {
  await synthWorker.start(PY_TTS, join(NARRATE, 'lab/synth_one.py'));
  return synthWorker.ready;
}

/** The pace for one line: its own override, or the project's rate (0 = the
    voice's own pace). */
function paceFor(seg: Segment, p: Project) {
  return seg.over?.targetWpm || p.settings.targetWpm;
}

/** The take exactly as generated, before its tail was trimmed — what "use the
    whole take" restores. */
export const rawOf = (wav: string) => wav.replace(/\.wav$/, '.raw.wav');

/** A NEW generation replaces whatever raw was there: this is a different take. */
export function keepRawFresh(wav: string) {
  if (existsSync(wav)) copyFileSync(wav, rawOf(wav));
}

/** Transcribe a take and say whether it is the line that was asked for. */
type Verdict = {
  ok: boolean; score: number | null; heard?: string; error?: string;
  missing_head?: number; extra_head?: number; missing_tail?: number;
  inserted?: number; speech_end?: number;
  /** Word match BEFORE near-miss spellings are forgiven. */
  exact?: number;
  /** [script word, what was heard] for names that must be said exactly. */
  must_say?: [string, string][];
  why?: 'words' | 'tail';
  tail_db?: number;
};

/** What went wrong with a take, in the words the operator needs to act on. */
function whyFailed(v: Verdict): string {
  if (v.must_say?.length)
    return v.must_say.map(([want, got]) => `said "${got}" for "${want}"`).join(', ');
  if (v.why === 'tail')
    return `said the line, then kept making sound (tail ${v.tail_db} dB)`;
  return `did not say the line (${v.score}): "${(v.heard ?? '').slice(0, 60)}"`;
}

/**
 * Which of two takes to keep when neither is clean.
 *
 * Said the words; then said our names right; then passed the tail check;
 * then said the words verbatim; then the stronger forgiving match; then the
 * quieter tail. Names rank above the tail because the tail check flagged all
 * three attempts at a "Hello, this is ... with Klavi." and nobody heard a
 * tail, while the mispronounced name was heard straight away.
 *
 * Ties used to go to whichever came FIRST — which is how "Klavy" (attempt 1)
 * beat "Klavi" (attempt 2) when both scored a forgiving 1.0, and how the Klavy
 * draw's seed became the line's seed, so every Rebuild started from it again.
 */
function rank(v: Verdict): number[] {
  const saidIt = v.ok || v.why === 'tail';
  return [saidIt ? 1 : 0, v.must_say?.length ? 0 : 1, v.ok ? 1 : 0,
          v.exact ?? v.score ?? 0, v.score ?? 0, -(v.tail_db ?? 0)];
}
function better(a: Verdict, b: Verdict) {
  const x = rank(a), y = rank(b);
  for (let i = 0; i < x.length; i++) if (x[i] !== y[i]) return x[i] > y[i];
  return false;
}

/** Which interpreter could actually transcribe, once we have found out. */
let verifyPy: string | null = null;

/**
 * Transcribe a take and say whether it is the line that was asked for.
 *
 * Tries BOTH environments, because the one holding a usable Whisper differs by
 * machine: on the Mac it is the analysis venv with mlx-whisper, on the GPU box
 * it is the TTS venv where openai-whisper was installed next to torch. Getting
 * this wrong is not loud — verify_take deliberately passes a take when it
 * cannot check one, so that a missing checker never blocks a render, and a
 * whole video was rendered unverified on exactly that silence. So the first
 * success is remembered, and a failure to check either way is reported rather
 * than swallowed.
 */
export async function verifyTake(wav: string, want: string): Promise<Verdict> {
  // Keep the model loaded. Spawning a process per take paid twenty-five
  // seconds to load Whisper every time — eighty-three minutes across a
  // hundred-and-forty-three line render, which is most of why one took two
  // hours rather than the ten minutes of generation inside it.
  const tries = verifyPy ? [verifyPy] : [PY_TTS, PY];
  let last: Verdict = { ok: true, score: null };
  for (const py of tries) {
    try {
      await verifyWorker.start(py, join(NARRATE, 'lab/verify_take.py'));
      const v = await verifyWorker.send({ wav, want }) as Verdict;
      if (v.score !== null && v.score !== undefined) {
        verifyPy = py;
        return v;
      }
      last = v;
      // This interpreter answered but could not transcribe. Stop it before
      // trying the other, or the next start() finds a live process and keeps
      // the one that does not work.
      verifyWorker.stop('cannot transcribe');
    } catch (e: any) {
      last = { ok: true, score: null, error: String(e?.message ?? e) };
      verifyWorker.stop('failed');
    }
  }
  return last;
}

/**
 * Cut a take at the end of its last word.
 *
 * A fine-tuned voice measured 20 dB louder AFTER the sentence than the stock
 * one — speech-like sound that no level-based trim can remove, because it is
 * speech, just not the speech that was asked for. The transcriber has already
 * run for the text check, so the timestamp is free.
 */
/** A file's length in seconds, straight from ffprobe. */
export async function probeDuration(wav: string): Promise<number | undefined> {
  try {
    const r = await run('ffprobe', ['-v', 'error', '-show_entries', 'format=duration',
                                    '-of', 'csv=p=0', wav], { timeout: 30_000 });
    const d = Number(r.out.trim());
    return Number.isFinite(d) ? Math.round(d * 1000) / 1000 : undefined;
  } catch {
    return undefined;
  }
}

export async function trimTail(wav: string, speechEnd: number) {
  try {
    const r = await run(PY, [join(NARRATE, 'lab/trim_tail.py'), wav,
                             '--end', String(speechEnd)],
                        { cwd: NARRATE, timeout: 60_000 });
    return JSON.parse(r.out.trim().split('\n').pop() ?? '{}') as
      { trimmed: boolean; removed?: number };
  } catch {
    return { trimmed: false };
  }
}

/** How many goes a line gets before we keep the best of a bad lot. */
const TRIES = 3;

/** Why this project cannot render a line yet, in words — or null if it can.
    A chosen voice brings its own sample; without one the project needs a
    reference clip, or a fine-tune to narrate from the model alone. */
export function cannotRender(p: Project): string | null {
  const chosen = p.settings.voice ? getVoice(p.settings.voice) : null;
  if (p.settings.voice && !chosen) return `the voice "${p.settings.voice}" is not in the voice lab any more`;
  const lora = (p.settings.voice ? latestOf(p.settings.voice) : null) || p.settings.voxLora;
  if (p.settings.noAssist && lora) return null;
  return chosen?.reference || p.settings.voiceRef ? null : 'pick a voice first';
}

export async function synthOne(seg: Segment, p: Project, work: string) {
  await startSynth();
  const output = join(work, `seg_${seg.id}.wav`);
  // A line's own direction wins over the script's; neither is the default.
  const direction = (seg.direction ?? p.settings.direction ?? '').trim();

  // A chosen VOICE carries its own promoted checkpoint, so a project follows
  // what that voice currently is instead of pinning a path.
  const chosen = p.settings.voice ? getVoice(p.settings.voice) : null;
  const lora = (p.settings.voice ? latestOf(p.settings.voice) : null)
            || p.settings.voxLora || '';
  // No assistance: the model alone, with no sample of the voice to imitate.
  const bare = !!p.settings.noAssist && !!lora;
  return synthWorker.send({
    text: seg.say,
    provider: 'voxcpm',
    // Everything the model needs goes through `voice_opts`, because that is
    // the ONLY thing the worker spreads into the provider. A field added
    // beside it is dropped in silence — which is what happened to the
    // fine-tune: the picker was wired, the setting was saved, the checkpoint
    // was named in the request, and the worker never passed it on.
    voice_opts: {
      ...(lora ? { lora } : {}),
      ...(bare ? { no_prompt: true } : {}),
      // A line may be directed on its own; otherwise the whole script is.
      // Empty means neither, and empty is the undirected path.
      ...(direction ? { direction } : {})
    },
    voice_ref: chosen?.reference || p.settings.voiceRef,
    output,
    target_wpm: paceFor(seg, p),
    terminal: seg.over?.terminal ?? 'none',
    pitch_st: p.settings.pitch ?? 0,
    warmth_db: p.settings.warmth ?? 0,
    seed: seg.seed ?? null
  });
}

/**
 * Synthesise a line, and check it actually says the line.
 *
 * Chatterbox is not deterministic and a minority of generations go wrong in
 * ways no signal measurement can see, because the audio is good audio — it is
 * just not the sentence requested. Words fall off the front ("Today we are
 * going to be talking about MCP connections" came back starting at "going"), a
 * fragment gets prepended, or something is invented mid-line that is in no
 * script and no recording. Every one is obvious on the first listen, so the
 * operator was doing this check by ear across thirty-seven lines.
 *
 * A take that fails is re-rolled on a new seed. If none of the tries is clean,
 * the best-scoring one is kept and reported, because a flawed line is still
 * better than no line and the operator can re-roll it by hand.
 */
export async function synthChecked(
  seg: Segment, p: Project, work: string,
  onNote?: (m: string) => void
) {
  let best: any = null;
  let bestV: Verdict | null = null;
  let bestAt = 0;
  // Every attempt writes to the same file, so without a copy the audio left
  // on disk is always the LAST attempt — while the seed and the transcript
  // recorded for the line belonged to whichever was judged best. The line
  // then said one thing, and "Draw again" reproduced another.
  const keep = (n: number, wav: string) => {
    for (const [from, to] of [[wav, `${wav}.try${n}`], [rawOf(wav), `${rawOf(wav)}.try${n}`]])
      if (existsSync(from)) copyFileSync(from, to);
  };
  const drop = (wav: string) => {
    for (let n = 1; n <= TRIES; n++)
      for (const f of [`${wav}.try${n}`, `${rawOf(wav)}.try${n}`])
        if (existsSync(f)) rmSync(f, { force: true });
  };
  for (let attempt = 1; attempt <= TRIES; attempt++) {
    // Keep the caller's seed for the first go, so "Draw again" still means what
    // it says; only a REJECTED take gets a fresh one.
    const use = attempt === 1 ? seg : { ...seg, seed: Math.floor(Math.random() * 1_000_000) };
    const r = await synthOne(use as Segment, p, work);
    // Match it to the established voice BEFORE judging it, so what is checked
    // and what is kept are the same audio.
    // Snapshot the generation BEFORE anything is applied to it, and judge it
    // there. Clipping is only legible on the raw: the fit applies a gain that
    // varies over time, which leaves the distortion audible but breaks up the
    // runs of identical samples that identify it, so a check on the fitted
    // copy reports a squared-off take as clean.
    keepRawFresh(r.path);
    const v = await verifyTake(r.path, seg.say);
    // Cut whatever the model kept saying after the sentence, BEFORE the fit —
    // the fit measures the take, and measuring a take that ends in half a
    // second of unasked-for speech teaches it the wrong level.
    let cut: { trimmed: boolean; removed?: number } = { trimmed: false };
    if (v.ok && v.speech_end != null) {
      cut = await trimTail(r.path, v.speech_end);
      // Do NOT re-snapshot the raw here. `.raw.wav` is the pristine
      // generation, and everything that fits a take to the video starts from
      // it precisely so the corrections cannot compound. Refreshing it after a
      // trim overwrote the only copy that could undo a bad cut — which is how
      // two lines ended up forty milliseconds long with no way back but to
      // generate them again.
      if (cut.trimmed) onNote?.(`${seg.id} trimmed ${cut.removed}s of tail`);
    }
    const dur = cut.trimmed ? await probeDuration(r.path) : r.duration;
    const out = { ...r, duration: dur, seed: (use as Segment).seed ?? null,
                  verified: v.ok, heard: v.heard, score: v.score };
    // Clean means the line, said right, including our own names. A take that
    // only passes by having "Klavy" forgiven into "Klavi" is kept as a
    // fallback but does not stop the search.
    if (v.ok && !v.must_say?.length) { drop(r.path); return out; }
    if (!bestV || better(v, bestV)) {
      best = out; bestV = v; bestAt = attempt;
      keep(attempt, r.path);
    }
    onNote?.(`${seg.id} attempt ${attempt} ${whyFailed(v)}`);
  }
  // Put the chosen take back, so the audio, its seed and what it was heard to
  // say all describe the same generation.
  if (best && bestAt && bestAt !== TRIES) {
    for (const f of [best.path, rawOf(best.path)])
      if (existsSync(`${f}.try${bestAt}`)) copyFileSync(`${f}.try${bestAt}`, f);
    onNote?.(`${seg.id} kept attempt ${bestAt} of ${TRIES}`);
  }
  if (best) drop(best.path);
  return best;
}

/**
 * Give every line room for the whole of its take.
 *
 * A take's length is decided at synthesis and changes whenever the line is
 * redrawn — a slower pace makes it longer. Its neighbours do not move, so a
 * take that grew runs into the line after it and the assembler writes that
 * line over its tail: the words are all there in the file and the last of them
 * never reaches the mix. Heard as a line chopped off mid-word.
 *
 * Blocks therefore grow to accommodate the audio, rather than the audio being
 * cut to fit the block. Only ever LATER: a line already positioned by hand
 * keeps its place unless the line before it genuinely needs the space, and the
 * render glides the picture through whatever air this leaves.
 */
const SEAM = 0.12;

export function makeRoom(p: Project) {
  const live = p.segments.filter((s) => !s.drop);
  let cursor = 0;
  let moved = 0;
  for (const s of live) {
    if (s.locked) { cursor = s.end; continue; }
    const len = (s.duration ?? (s.end - s.start)) * (s.stretch ?? 1);
    const at = Math.max(s.start + (s.offset ?? 0), cursor + SEAM);
    const off = Math.round((at - s.start) * 100) / 100;
    if (off !== (s.offset ?? 0)) { s.offset = off; moved++; }
    cursor = at + len + (s.hold ?? 0);
  }
  return moved;
}

/** Cut the original audio for one span, so "keep real" can be auditioned. */
export async function clipOriginal(source: string, start: number, end: number, out: string) {
  const pad = 0.06;
  await run('ffmpeg', ['-v', 'error', '-ss', String(Math.max(0, start - pad)),
                       '-to', String(end + pad), '-i', source, '-map', '0:a:0', '-vn',
                       '-ac', '1', '-ar', '48000', '-c:a', 'pcm_s16le', out, '-y']);
  return out;
}

export async function ollamaModels(): Promise<string[]> {
  try {
    const r = await fetch(`${OLLAMA}/api/tags`);
    const d = await r.json();
    return (d.models ?? []).map((m: any) => m.name);
  } catch {
    return [];
  }
}

/**
 * The finished file: your recording with the rendered lines swapped in, then
 * the dead air those shorter lines left behind removed from picture and sound
 * together, then mastered.
 *
 * The retimer only ever cuts BETWEEN sentences, and inside a gap it cuts where
 * the picture is stillest — a screencast is mostly a static frame, so a splice
 * placed where nothing moves is invisible and one placed mid-cursor-sweep is
 * not. Lines kept real are reported at their full original span, so no gap is
 * taken from around them.
 */
export async function renderFinal(
  p: Project,
  work: string,
  placed: Array<{ id: string; start: number; end: number; original: [number, number] }>,
  previewWav: string,
  onStep: (step: string, detail?: string) => void
) {
  const media = p.outputs.master ?? p.source;
  const name = stem(p.source);

  // Glide through the dead air rather than cutting it. A tightened script
  // leaves a gap after almost every line, and a video full of jump cuts reads
  // worse than one that moves briskly through its own silences. Gaps the
  // operator held are left at full length.
  const holds: Record<string, boolean> = {};
  for (const s of p.segments) if (s.holdGap) holds[s.id] = true;
  // Room the operator gave a line belongs to that line. The assembler reports
  // where each take ENDS; extending that by `hold` is what stops the glide
  // pass from speeding the picture straight back through the space.
  const extra = new Map(p.segments.filter((s) => (s.hold ?? 0) > 0.01).map((s) => [s.id, s.hold as number]));
  const spans = placed.map((x) => extra.has(x.id) ? { ...x, end: x.end + (extra.get(x.id) as number) } : x);
  const held = Object.keys(holds).length;
  onStep('closing the dead air',
         `${placed.length} lines, ${held} gap(s) held at full length`);
  // If the mix now outlasts the picture, the picture holds its last frame
  // rather than the sound being cut to fit. Must be measured BEFORE the plan
  // is written — it is a value in that plan.
  const tail = Math.max(0, (await avDurations(previewWav)).audio - (p.duration ?? 0));
  const glidePlan = join(work, 'glide.plan.json');
  await writeFile(glidePlan, JSON.stringify({
    source: media, audio: previewWav, placed: spans, holds,
    gap_min: p.settings.gapMin ?? 0.8,
    speed: p.settings.gapSpeed ?? 3,
    tail_hold: Math.round(tail * 1000) / 1000,
    // Sections the operator deleted, removed from picture and sound together.
    // One that reaches the end of the recording is sent as reaching the end of
    // the VIDEO (1e9), so the analysis and the file disagreeing about the
    // length by a few hundredths cannot leave a sliver of it behind.
    cuts: (p.cuts ?? []).map((c) => [c.from, c.to >= (p.duration ?? 0) - 0.05 ? 1e9 : c.to]),
    // Blur masks, on the same source timeline: (from, to, x, y, w, h).
    masks: (p.masks ?? []).map((m) => [m.from, m.to, m.x, m.y, m.w, m.h])
  }));
  const cut = join(work, `${name}.narrated.cut.mp4`);
  const r2 = await run(PY, [join(NARRATE, 'lab/glide.py'), glidePlan, '-o', cut],
                       { cwd: NARRATE, timeout: 3_600_000 });
  if (!existsSync(cut)) throw new Error(`glide failed:\n${r2.err.slice(-1500) || r2.out.slice(-1500)}`);
  onStep('closing the dead air', lastLines(r2.out, 3));
  const cutAv = await avDurations(cut);
  onStep('picture and sound',
         `${cutAv.video.toFixed(2)}s picture / ${cutAv.audio.toFixed(2)}s sound`);

  // Repair screen tearing before mastering.
  //
  // The capture is torn at source — a recorder grabbing the framebuffer
  // without waiting for the display refresh catches it mid-update — so it
  // lands in the same frames on every render, which is why regenerating never
  // helped. 239 frames of 17,526 on this video: 1.4%, in 96 short bursts.
  // Dropping them and holding the frame before each costs 17ms apiece at
  // 60fps and is invisible; leaving them in is not.
  const clean = join(work, `${name}.narrated.clean.mp4`);
  let detear: any = null;
  try {
    onStep('repairing torn frames');
    const rt = await run(PY, [join(NARRATE, 'lab/detear.py'), cut, '-o', clean],
                         { cwd: NARRATE, timeout: 3_600_000 });
    detear = JSON.parse(rt.out.trim().split('\n').pop() ?? '{}');
    onStep('repairing torn frames',
           detear.torn ? `${detear.torn} of ${detear.frames} frames (${detear.percent}%)`
                       : 'none found');
  } catch (e: any) {
    // A failed repair must never cost the render: carry on with the cut.
    onStep('repairing torn frames', `skipped: ${e?.message ?? e}`);
  }
  const mastered = detear?.torn && existsSync(clean) ? clean : cut;

  onStep('mastering');
  const out = join(work, `${name}.narrated.mp4`);
  // This mix is synthetic: takes laid on silence with a bed drawn under them.
  // There is no room noise in it to remove, so the denoiser had nothing to do
  // except modulate the SPEECH — 2.9 dB of slow swing on its own, measured.
  // The CLI's defaults are for real recordings and stay as they are.
  //
  // -16 LUFS rather than -14: speech this dynamic only reaches -14 by having
  // its peaks limited on one moment in nine, where -16 touches one in
  // twenty-five by about a decibel. YouTube turns loud uploads down and never
  // turns quiet ones up, so this plays 2 dB under a -14 upload, not squashed.
  const r3 = await run(CLI, ['master', mastered, '-o', out, '--denoise', 'none', '--lufs', '-16'],
                       { cwd: NARRATE, timeout: 3_600_000 });
  if (!existsSync(out)) throw new Error(`master failed:\n${r3.err.slice(-1500) || r3.out.slice(-1500)}`);
  onStep('mastering', lastLines(r3.out, 6));
  return out;
}

const lastLines = (s: string, n: number) =>
  s.trim().split('\n').filter((l) => l.trim()).slice(-n).join(' · ').trim();

/** Both stream durations, so the log can show that they still agree. */
async function avDurations(path: string) {
  const r = await run('ffprobe', ['-v', 'error', '-show_entries',
                                  'stream=codec_type,duration', '-of', 'csv=p=0', path]);
  const d: Record<string, number> = {};
  for (const line of r.out.trim().split('\n')) {
    const [kind, secs] = line.split(',');
    if (kind) d[kind] = Number(secs);
  }
  return { video: d.video ?? 0, audio: d.audio ?? 0 };
}

/**
 * Read an analysis file.
 *
 * Python writes `Infinity` / `-Infinity` / `NaN` for an empty noise floor, and
 * none of those are legal JSON — `JSON.parse` throws on the whole document.
 * Every caller here had that failure swallowed by a catch, so the analysis
 * silently read as "no data": it is why the project duration had to be
 * backfilled by hand and why no fillers were ever found. Map them to null.
 */
export async function readAnalysis(path?: string): Promise<any | null> {
  if (!path || !existsSync(path)) return null;
  const raw = (await readFile(path, 'utf8'))
    .replace(/(:\s*)-Infinity\b/g, '$1null')
    .replace(/(:\s*)Infinity\b/g, '$1null')
    .replace(/(:\s*)NaN\b/g, '$1null');
  return JSON.parse(raw);
}

/** Hard fillers only. A soft filler is a real word and a stutter needs the
    sentence re-read, so neither should be silenced in place. */
export async function hardFillers(analysis?: string): Promise<Array<[number, number]>> {
  const doc = await readAnalysis(analysis).catch(() => null);
  return (doc?.flags ?? [])
    .filter((f: any) => f.kind === 'filler')
    .map((f: any) => [f.start, f.end] as [number, number]);
}

/** What the assembler is asked to build. Both the preview and the final render
    go through this, so the mix you approve is the mix that gets cut. */
export async function assemblePlan(p: Project, work: string) {
  return {
    source: p.source,
    source_wav: join(work, `${stem(p.source)}_src.wav`),
    // Build the mix from the takes alone, on a bed drawn from the takes.
    //
    // The assembler's default is to start from the screencast and patch each
    // take over its own span, which is right while any of the original is
    // being kept and wrong once every line has been re-read: a replacement is
    // never frame-perfect, so every seam leaks the original underneath — its
    // room, its breaths, the tail of the word being replaced. Clean takes
    // arrived in the mix sounding, exactly as reported, like someone in a
    // tunnel with stray breaths that belong to nobody.
    synthetic: p.settings.syntheticMix !== false
               && p.segments.every((s) => s.drop || s.locked || s.audio),
    fillers: await hardFillers(p.analysis),
    clean_background: p.settings.cleanBackground !== false,
    segments: p.segments.map((s) => ({
      id: s.id, start: s.start, end: s.end, audio: s.audio,
      locked: s.locked, offset: s.offset ?? 0, drop: !!s.drop,
      stretch: s.stretch ?? 1
    }))
  };
}

/**
 * A light copy of the picture for the editor, built once.
 *
 * The master is 4096x2178 at 60fps. Playing that while a separate <audio>
 * element carries the mix makes the browser give up on the video — both
 * elements report `playing`, then the video pauses itself with no error and
 * nothing in the page having called pause. Two synchronised media elements
 * were the fragile part; a proxy plus a mux replaces them with one.
 */
export async function ensureProxy(p: Project, work: string) {
  const out = join(work, `${stem(p.source)}.proxy.mp4`);
  if (existsSync(out)) return out;
  const src = p.outputs.master ?? p.source;
  // +faststart moves the moov atom to the FRONT. Without it the index sits
  // after 30 MB of media, and the browser plays only what it happens to have
  // buffered and then stops — about ten seconds in, with no error.
  const args = (venc: string[]) => [
    '-v', 'error', '-i', src, '-map', '0:v:0', '-an',
    '-vf', 'scale=960:-2', '-r', '30', ...venc,
    '-movflags', '+faststart', out, '-y'
  ];
  // Hardware first; it is the difference between one minute and ten.
  let r = await run('ffmpeg', args(['-c:v', 'h264_videotoolbox', '-b:v', '2500k']),
                    { timeout: 1_800_000 });
  if (!existsSync(out)) {
    r = await run('ffmpeg', args(['-c:v', 'libx264', '-crf', '26', '-preset', 'veryfast']),
                  { timeout: 3_600_000 });
  }
  if (!existsSync(out)) throw new Error(`proxy failed:\n${r.err.slice(-800)}`);
  return out;
}

/** Proxy picture + the assembled mix, as ONE file. Video is stream-copied, so
    this costs seconds per rebuild rather than a re-encode. */
export async function muxPreview(proxy: string, audio: string, out: string) {
  // NOT `-shortest`. The picture proxy and the mix are not the same length —
  // the proxy came out 418.27s against a 420.44s mix here — and `-shortest`
  // resolves that by CUTTING THE SOUND, silently removing the last two seconds
  // of the video's narration from the only thing the operator ever listens to.
  // Letting the audio run past the end of the picture costs a held last frame
  // and keeps every word.
  const r = await run('ffmpeg', ['-v', 'error', '-i', proxy, '-i', audio,
                                 '-map', '0:v:0', '-map', '1:a:0',
                                 '-c:v', 'copy', '-c:a', 'aac', '-b:a', '160k',
                                 '-movflags', '+faststart',
                                 out, '-y'], { timeout: 900_000 });
  if (!existsSync(out)) throw new Error(`mux failed:\n${r.err.slice(-800)}`);
  return out;
}

/** Edit the whole script for flow. One line in, one line out, in order —
    every line is anchored to a moment in the picture. */
export async function consistencyPass(lines: string[], work: string, model: string) {
  const inp = join(work, 'script.in.json');
  const out = join(work, 'script.flow.json');
  await writeFile(inp, JSON.stringify({ lines }));
  const r = await run(PY, [join(NARRATE, 'lab/consistency_pass.py'), inp,
                           '-o', out, '--model', model, '--host', OLLAMA, '--lexicon', writeLexicon()],
                      { cwd: NARRATE, timeout: 1_800_000 });
  if (!existsSync(out)) throw new Error(`consistency pass failed:\n${r.err.slice(-1200)}`);
  return JSON.parse(await readFile(out, 'utf8')) as
    { lines: string[]; changed: number[]; rejected: string[] };
}
