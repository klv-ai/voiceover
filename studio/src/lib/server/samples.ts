import { copyFileSync, existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { spawn } from 'node:child_process';
import { join } from 'node:path';
import { NARRATE, PY, PY_TTS, DATA } from './config';
import { getVoice, latestOf, VOICES } from './voices';
import { startJob, getJob, note, runningJobs, type Job } from './jobs';
import { holdCard, holdChild, releaseChild, synthWorker, verifyWorker } from './run';
import { studioSettings } from './settings';

/**
 * Find the best clone sample for a voice — the whole method, as a job.
 *
 * The sample decides the background, the pace and the steadiness of every
 * line the clone speaks, and it was got wrong twice before it was got right:
 * a screencast cut ticked (56 clicks a minute copied into the output), and the
 * cleanest Voice Lab reads slowed every render to their read-off-a-page pace.
 * What finally worked was to stop judging samples and judge what the clone
 * MAKES of them:
 *
 *   1. build candidates from every recording (build_sample.py): clean, at the
 *      presenting pace, tight in pitch and level — scrubbed as they are written
 *   2. add the sample in use now, which may well be the one to keep
 *   3. generate the same lines with the same seeds from each, through the
 *      voice's promoted checkpoint, and measure clicks, pace, wandering and
 *      wrong words (score_samples.py)
 *   4. let the operator LISTEN to the leaders and adopt one — the step that
 *      made this work, and the one thing not automated
 *
 * Runs after a checkpoint is promoted, since a sample is only good for the
 * weights it is paired with.
 */

export const slot = (id: string) => `samples:${id}`;
const stageRoot = (id: string) => join(DATA, 'work', '_samples', id);
const historyOf = (id: string) => join(VOICES, id, 'sample_history');


/** The newest search's folder, if there is one. */
export function latestStage(id: string): string | null {
  const root = stageRoot(id);
  if (!existsSync(root)) return null;
  const runs = readdirSync(root).filter((d) => /^\d{8}T\d{6}$/.test(d)).sort();
  return runs.length ? join(root, runs[runs.length - 1]) : null;
}

/** Whatever the scorer has written, whoever started it. */
export function readSearch(id: string) {
  const stage = latestStage(id);
  if (!stage) return { stage: null, rows: [], built: {}, done: false };
  const rows: any[] = [];
  let done = false;
  const f = join(stage, 'progress.jsonl');
  if (existsSync(f)) {
    for (const line of readFileSync(f, 'utf8').split('\n')) {
      const t = line.trim();
      if (!t.startsWith('{')) continue;
      try {
        const m = JSON.parse(t);
        if (m.summary) done = true;
        else if (m.sample) rows.push(m);
      } catch { /* a half-written last line is normal while it runs */ }
    }
  }
  let built: any = {};
  try { built = JSON.parse(readFileSync(join(stage, 'samples.json'), 'utf8')); } catch { /* not yet */ }
  // One line of each candidate to listen to: the same line and seed for all,
  // so the ear compares samples and nothing else.
  for (const r of rows) r.listen = join(stage, 'takes', `${r.sample}_1_101.wav`);
  // Set-aside samples last (too many wrong words), then by score. The scorer
  // owns the rule; see score_row in score_samples.py.
  rows.sort((a, b) => Number(!!a.gated) - Number(!!b.gated) || a.score - b.score);
  return { stage, rows, built, done };
}

/** A render, a training run or another scorer holds the card. */
function cardBusy(except: string): string | null {
  for (const j of runningJobs()) {
    if (j.project === except) continue;
    if (['render', 'synth-all', 'train'].includes(j.kind)) return `${j.kind} running (${j.project})`;
  }
  return null;
}

function spawnLogged(cmd: string, args: string[], key: string): Promise<number> {
  return new Promise((resolve) => {
    // NOTHING piped: an undrained pipe is what wedged the checkpoint scorer
    // twice. The children write what they have to say into files.
    const p = spawn(cmd, args, { cwd: NARRATE, stdio: 'ignore' });
    if (p.pid) holdChild(key, p.pid, 'samples');
    p.on('close', (code) => { releaseChild(key); resolve(code ?? 1); });
  });
}

export function startSampleSearch(id: string, why = 'asked for'): Job | { error: string } {
  const voice = getVoice(id);
  if (!voice) return { error: 'no such voice' };
  const running = getJob(slot(id));
  if (running?.status === 'running') return running;
  const busy = cardBusy(slot(id));
  if (busy) return { error: `the card is busy: ${busy}` };

  const lora = latestOf(id) ?? '';
  const stamp = new Date().toISOString().replace(/[-:]/g, '').slice(0, 15);
  const stage = join(stageRoot(id), stamp);
  mkdirSync(stage, { recursive: true });

  return startJob(slot(id), 'train', async (j) => {
    note(j, `searching for a better sample (${why})`);
    // 1. Candidates from every recording, scrubbed as they are written.
    note(j, 'building candidates from every recording');
    const b = await spawnLogged(PY, [join(NARRATE, 'lab/build_sample.py'),
                                     join(VOICES, id), '-o', stage], slot(id));
    if (b !== 0) throw new Error('could not build candidates');
    // 2. The sample in use now, if there is one, with its scrub, so it
    //    competes as it sounds. Scrubbed copy written AFTER the original, so
    //    it reads as current. A new voice has none: its first sample comes
    //    from this search.
    if (voice.reference && existsSync(voice.reference)) {
      copyFileSync(voice.reference, join(stage, 'sample_current.wav'));
      const txt = voice.reference.replace(/\.wav$/, '.txt');
      if (existsSync(txt)) copyFileSync(txt, join(stage, 'sample_current.txt'));
      const sc = voice.reference.replace(/\.wav$/, '.scrubbed.wav');
      if (existsSync(sc)) copyFileSync(sc, join(stage, 'sample_current.scrubbed.wav'));
    }
    const samples = readdirSync(stage).filter((f) => /^sample_[a-z0-9]+\.wav$/.test(f))
      .map((f) => join(stage, f));
    // 3. Free the card: the studio's own workers hold the model and a
    //    transcriber, and a second copy of the model beside them runs out of
    //    memory. Both come back on the next request.
    synthWorker.stop('freeing the card for sample scoring');
    verifyWorker.stop('freeing the card for sample scoring');
    note(j, `scoring ${samples.length} samples by what the clone makes of them`);
    holdCard(`scoring voice samples for ${id}`);
    let s = 1;
    try {
      s = await spawnLogged(PY_TTS, [join(NARRATE, 'lab/score_samples.py'),
                                     '--samples', ...samples, '--lines', ...studioSettings().testLines,
                                     '--seeds', '101', '202',
                                     ...(lora ? ['--lora', lora] : []),
                                     '--out', join(stage, 'takes'),
                                     '--progress', join(stage, 'progress.jsonl')], slot(id));
    } finally {
      holdCard(null);
    }
    if (s !== 0) {
      let tail = '';
      try { tail = readFileSync(join(stage, 'progress.jsonl.log'), 'utf8').split('\n').filter(Boolean).slice(-3).join(' '); } catch { /* none */ }
      throw new Error(`scoring stopped: ${tail.slice(-300)}`);
    }
    const r = readSearch(id);
    return `best: ${r.rows[0]?.sample?.replace(/^sample_/, '') ?? '—'} — listen before adopting`;
  });
}

/**
 * Make a candidate this voice's sample. The one in use goes to history first,
 * with its scrub, so nothing is ever lost; the candidate's scrub comes along
 * so no render waits minutes for one.
 */
export function adoptSample(id: string, name: string) {
  const voice = getVoice(id);
  const r = readSearch(id);
  if (!voice || !r.stage) return { error: 'nothing to adopt' };
  if (name === 'sample_current') return { ok: true, unchanged: true };
  const src = join(r.stage, `${name}.wav`);
  if (!/^sample_[a-z0-9]+$/.test(name) || !existsSync(src)) return { error: 'no such candidate' };
  const hist = historyOf(id);
  mkdirSync(hist, { recursive: true });
  const stamp = new Date().toISOString().replace(/[-:]/g, '').slice(0, 15);
  // A voice's first sample is written where every later one will be.
  const ref = voice.reference || join(VOICES, id, 'reference.wav');
  const base = ref.replace(/\.wav$/, '');
  for (const ext of ['.wav', '.txt', '.scrubbed.wav'])
    if (existsSync(base + ext)) copyFileSync(base + ext, join(hist, `reference.${stamp}${ext}`));
  copyFileSync(src, ref);
  copyFileSync(join(r.stage, `${name}.txt`), base + '.txt');
  const sc = join(r.stage, `${name}.scrubbed.wav`);
  if (existsSync(sc)) copyFileSync(sc, base + '.scrubbed.wav');   // after the .wav: reads as current
  const row = r.rows.find((x) => x.sample === name);
  const meta = join(VOICES, id, 'voice.json');
  const v = JSON.parse(readFileSync(meta, 'utf8'));
  v.reference = ref;
  v.sample = { from: r.built?.[name.replace(/^sample_/, '')]?.files ?? name,
               scores: row ? { clicks_per_min: row.clicks_per_min, wpm: row.wpm, wpm_sd: row.wpm_sd,
                               pitch_sd_st: row.pitch_sd_st, level_sd_db: row.level_sd_db, wrong: row.wrong } : null,
               search: r.stage, adopted: new Date().toISOString(),
               previous: voice.reference ? `sample_history/reference.${stamp}.wav` : null };
  writeFileSync(meta, JSON.stringify(v, null, 2));
  // The worker may hold the old sample's features; the next line starts clean.
  synthWorker.stop('voice sample changed');
  return { ok: true, previous: v.sample.previous };
}
