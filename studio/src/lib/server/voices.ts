import { existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync, rmSync, statSync, renameSync } from 'node:fs';
import { safeId } from './paths';
import { join } from 'node:path';
import { spawn } from 'node:child_process';
import { DATA, NARRATE, PY } from './config';
import { run } from './run';
import { listProjects, TRASH } from './projects';

/**
 * A voice is one speaker: the reads recorded for it, the fine-tunes trained on
 * them, the checkpoint currently promoted, and the short sample handed to the
 * model on every line. Everything a voice is made of lives in its own folder,
 * so two speakers never share a recording, a run or a sample.
 */
export const VOICES = join(DATA, 'voices');

mkdirSync(VOICES, { recursive: true });

export type Voice = {
  id: string;
  name: string;
  createdAt: string;
  /** `project` is a project's own pooled read, listed so it can be picked. */
  kind: 'corpus' | 'project';
  /** The sample handed to the model on every line. Empty until one has been
      found and adopted. */
  reference: string;
  /** Clips mined from projects' recordings, when the voice was built that way. */
  corpus?: {
    dir: string;
    clips: number;
    words: number;
    /** Speech in the transcribed clips — what a local fine-tune can learn
        from, because only these have text attached. */
    minutes: number;
    /** Everything a commercial clone would be fed, continuous reads included.
        This is the number to compare against the thirty-minute floor; the clip
        total alone understates it by however much unscripted reading has been
        recorded. */
    trainingMinutes: number;
    joined: { path: string; seconds: number }[];
  };
  minSnr?: number;
  refSeconds?: number;
  report?: unknown;
  /** Measured on the reference itself, so two voices can be compared without
      listening to both. */
  stats?: Profile;
};

export type Profile = {
  seconds?: number;
  floor_db?: number;
  speech_db?: number;
  snr_db?: number;
  peak?: number;
  clip_ms?: number;
  /** Digital silence: a pooled reference is a concatenation of takes, and each
      take begins with the encoder's priming silence, so the reference can be
      several percent holes. The level meter cannot show this — it reads a hole
      as a magnificent noise floor. */
  zero_pct?: number;
  holes?: number;
  hole_s?: number;
};

const MEASURE = join(NARRATE, 'lab/measure_audio.py');
const metaOf = (id: string) => join(VOICES, id, 'voice.json');

/** Profile any wav — duration, level, noise floor — for display. */
export async function measure(paths: string[]): Promise<Record<string, Profile>> {
  const real = paths.filter((p) => p && existsSync(p));
  if (!real.length) return {};
  try {
    const r = await run(PY, [MEASURE, ...real], { cwd: NARRATE, timeout: 120_000 });
    const rows = JSON.parse(r.out.trim().split('\n').pop() ?? '[]') as (Profile & { path: string })[];
    return Object.fromEntries(rows.map((row) => [row.path, row]));
  } catch {
    return {};
  }
}

export function readVoices(): Voice[] {
  if (!existsSync(VOICES)) return [];
  return readdirSync(VOICES)
    .filter((d) => existsSync(metaOf(d)))
    .map((d) => JSON.parse(readFileSync(metaOf(d), 'utf8')) as Voice)
    .sort((a, b) => b.createdAt.localeCompare(a.createdAt));
}

/**
 * Every reference available to a project, whether or not it was built here.
 *
 * The project pools are listed alongside the built voices on purpose: the
 * point of this screen is to A/B them, and a comparison you have to go and
 * find the other half of is one nobody runs.
 */
export async function listVoices(): Promise<Voice[]> {
  const built = readVoices();
  const pools: Voice[] = listProjects()
    .map((p) => ({ p, ref: join(DATA, 'work', p.id, 'voice_ref.wav') }))
    .filter(({ ref }) => existsSync(ref))
    .map(({ p, ref }) => ({
      id: `project:${p.id}`,
      name: `${p.name} — its own read`,
      createdAt: new Date(statSync(ref).mtimeMs).toISOString(),
      kind: 'project' as const,
      reference: ref
    }));
  const all = [...built, ...pools];
  const stats = await measure(all.map((v) => v.reference).filter(Boolean));
  for (const v of all) if (v.reference) v.stats = stats[v.reference];
  return all;
}

export const getVoice = (id: string) =>
  readVoices().find((v) => v.id === id) ?? null;

/** Take a voice out of the lab — MOVED to data/trash, like a project, never
    erased: its reads are somebody speaking for an hour, and its runs are a
    night of training each. */
export function removeVoice(id: string) {
  if (!safeId(id) || !existsSync(metaOf(id))) return false;
  const stamp = new Date().toISOString().replace(/[-:]/g, '').slice(0, 15);
  mkdirSync(TRASH, { recursive: true });
  renameSync(join(VOICES, id), join(TRASH, `voice-${id}-${stamp}`));
  return true;
}

const slug = (name: string) =>
  name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 40)
  || 'voice';

/**
 * A new, empty voice. Everything else happens on its own page: record reads,
 * train, promote a checkpoint, then find and adopt a sample.
 */
export function createVoice(name: string): Voice {
  const base = slug(name);
  let id = base;
  for (let n = 2; existsSync(metaOf(id)); n++) id = `${base}-${n}`;
  mkdirSync(join(VOICES, id), { recursive: true });
  const voice: Voice = {
    id,
    name: name.trim() || id,
    createdAt: new Date().toISOString(),
    kind: 'corpus',
    reference: ''
  };
  writeFileSync(metaOf(id), JSON.stringify(voice, null, 2));
  return voice;
}

/**
 * Every fine-tune sitting next to a voice.
 *
 * A LoRA is not a voice — it is a change to the model that generates one — so
 * it is listed separately rather than folded into listVoices(). The training
 * run writes a checkpoint per interval and the useful one is rarely the last:
 * loss/stop tripled over this run while loss/diff stood still, so a later
 * checkpoint sounds like the speaker and has forgotten how to stop. All of
 * them are offered, newest last, because choosing between them is the job.
 */
export function listLoras(): { id: string; name: string; path: string }[] {
  if (!existsSync(VOICES)) return [];
  const out: { id: string; name: string; path: string }[] = [];
  for (const voice of readdirSync(VOICES)) {
    const base = join(VOICES, voice, 't5-voxcpm');
    if (!existsSync(base)) continue;
    // EVERY run, not just the first. A second training run writes `ckpt2`,
    // and scanning only `ckpt` made the better checkpoint unreachable from the
    // one screen that exists to choose between them.
    for (const runDir of readdirSync(base).filter((d) => d.startsWith('ckpt')).sort()) {
      const ck = join(base, runDir);
      for (const step of readdirSync(ck).sort()) {
        const dir = join(ck, step);
        if (!existsSync(join(dir, 'lora_config.json'))) continue;
        out.push({ id: `${voice}/${runDir}/${step}`,
                   name: `${voice} — ${runDir} ${step}`, path: dir });
      }
    }
  }
  return out;
}

/** Where this voice keeps the reads recorded for it. */
export const sessionsOf = (id: string) => join(VOICES, id, 'sessions');

/** The script this voice is being read, and the reader's place in it. */
export const scriptOf = (id: string) => join(VOICES, id, 'read_script.json');

/** Every training run this voice has had, newest run last. */
export function runsOf(id: string) {
  const base = join(VOICES, id, 't5-voxcpm');
  if (!existsSync(base)) return [];
  return readdirSync(base)
    .filter((d) => d.startsWith('ckpt'))
    .sort()
    .map((run) => {
      const dir = join(base, run);
      const steps = readdirSync(dir)
        .filter((s) => existsSync(join(dir, s, 'lora_config.json')))
        .sort();
      return { run, dir, steps };
    })
    .filter((r) => r.steps.length);
}

/**
 * Which checkpoint this voice currently IS.
 *
 * Deliberately a pointer rather than "the newest", because the newest is
 * usually wrong: across the first run here the stop head degraded threefold
 * while the timbre improved, so the last checkpoint sounded most like the
 * speaker and had forgotten how to stop. Promotion is a judgement, and a
 * judgement has to be recorded somewhere.
 */
export function latestOf(id: string): string | null {
  try {
    const v = JSON.parse(readFileSync(metaOf(id), 'utf8'));
    return v?.latest && existsSync(v.latest) ? v.latest : null;
  } catch {
    return null;
  }
}

export function promote(id: string, checkpoint: string) {
  if (!existsSync(metaOf(id))) return false;
  const v = JSON.parse(readFileSync(metaOf(id), 'utf8'));
  v.latest = checkpoint || '';
  v.promotedAt = new Date().toISOString();
  writeFileSync(metaOf(id), JSON.stringify(v, null, 2));
  return true;
}
