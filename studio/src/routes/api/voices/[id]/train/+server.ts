import { json } from '@sveltejs/kit';
import { existsSync, readdirSync, readFileSync, mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { spawn } from 'node:child_process';
import { NARRATE, PY_TTS } from '$lib/server/config';
import { VOICES } from '$lib/server/voices';
import { startJob, getJob, note, stopJob } from '$lib/server/jobs';
import { holdChild, releaseChild, killChild } from '$lib/server/run';

/**
 * Train this voice, from the page that owns it.
 *
 * Every step of this has been done by hand at least once, and each hand pass
 * was a chance to cut the wrong sessions, point at the wrong corpus, or sync
 * a script to the box and forget the config beside it. It belongs behind one
 * button, with its progress where the person who pressed it can see it.
 */
const slot = (id: string) => `train:${id}`;

/**
 * The trainer process, so stopping can actually stop it.
 *
 * `stopJob` sets a cancel flag, which works for a loop that checks it between
 * lines. A training run is one long child process that checks nothing, so the
 * flag turned the button into a label reading "stopping…" while the GPU kept
 * going for another forty minutes. Whatever spawns a child has to keep hold
 * of it.
 */


const state = (j: ReturnType<typeof getJob>) =>
  j ? { status: j.status, step: j.step, log: j.log.slice(-40), error: j.error,
        seconds: Math.round(((j.endedAt ?? Date.now()) - j.startedAt) / 1000) }
    : { status: 'idle' };

export const GET = ({ params }) => json(state(getJob(slot(params.id))));
export const DELETE = ({ params }) => {
  // Kill the child itself, not just the job's flag: a training run is one
  // long process that checks nothing, so a cancel flag turns the button into
  // a label reading "stopping…" while the card keeps going.
  killChild(slot(params.id));
  return json(state(stopJob(slot(params.id))));
};

export const POST = async ({ params, request }) => {
  const body = await request.json().catch(() => ({}));
  const dir = join(VOICES, params.id);
  if (!existsSync(dir)) return json({ error: 'no such voice' }, { status: 404 });

  // The base model's snapshot in the Hugging Face cache, wherever that is.
  const hub = process.env.HF_HUB_CACHE
    ?? join(process.env.HF_HOME ?? join(process.env.HOME ?? '', '.cache/huggingface'), 'hub');
  const snaps = join(hub, 'models--openbmb--VoxCPM2', 'snapshots');
  const base = existsSync(snaps)
    ? join(snaps, readdirSync(snaps)[0] ?? '')
    : 'openbmb/VoxCPM2';

  const progress = join(dir, 't5-voxcpm', 'progress.jsonl');
  mkdirSync(join(dir, 't5-voxcpm'), { recursive: true });

  const job = startJob(slot(params.id), 'train', async (j) => {
    return await new Promise<string>((resolve, reject) => {
      const p = spawn(PY_TTS, [join(NARRATE, 'lab/train_voice.py'),
                               '--voice', dir, '--base', base,
                               '--python', PY_TTS,
                               '--progress', progress,
                               '--steps', String(Math.min(2000, Math.max(100,
                                 Number(body?.steps) || 600)))],
                      // stdout DISCARDED: progress is in the file, and an
                      // undrained pipe is what wedged the scorer twice.
                      // NOTHING piped: see the scorer. Progress and errors
                      // both go to files the endpoint reads.
                      { cwd: NARRATE, detached: true, stdio: 'ignore' });
      if (p.pid) holdChild(slot(params.id), p.pid, 'train');
      let lastErr = '';
      const tick = setInterval(() => {
        if (!existsSync(progress)) return;
        for (const line of readFileSync(progress, 'utf8').split('\n')) {
          const t = line.trim();
          if (!t.startsWith('{')) continue;
          try {
            const m = JSON.parse(t);
            const txt = m.phase === 'step' ? m.line
              : m.phase === 'corpus'
                ? `corpus: ${m.clips} clips, ${m.minutes} min (${m.read_minutes} read)`
              : m.phase === 'split' ? `holding out ${m.val} clips`
              : m.phase === 'training' ? `training ${m.run}, ${m.steps} steps`
              : m.phase === 'failed' ? `failed at ${m.where}: ${m.error ?? ''}`
              : m.phase;
            if (txt && j.log[j.log.length - 1] !== txt) note(j, txt);
          } catch { /* a half-written last line is normal */ }
        }
      }, 5000);
      p.on('close', (code) => {
        clearInterval(tick);
        releaseChild(slot(params.id));
        if (code === 0) resolve('trained');
        else {
          // The child's own output is beside its progress file, so a failure
          // can still be explained without a pipe.
          try {
            const tail = readFileSync(progress + '.log', 'utf8').split('\n')
              .filter(Boolean).slice(-4).join(' ');
            lastErr = tail.slice(-400);
          } catch { /* no log is not worse than no pipe */ }
          reject(new Error(lastErr || `stopped (exit ${code})`));
        }
      });
    });
  });
  return json(state(job));
};
