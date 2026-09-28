import { json } from '@sveltejs/kit';
import { studioSettings } from '$lib/server/settings';
import { join } from 'node:path';
import { existsSync, readFileSync, mkdirSync } from 'node:fs';
import { spawn } from 'node:child_process';
import { NARRATE, PY_TTS, DATA } from '$lib/server/config';
import { getVoice, runsOf } from '$lib/server/voices';
import { startJob, getJob, note, stopJob } from '$lib/server/jobs';
import { holdChild, releaseChild, killChild } from '$lib/server/run';

/**
 * Score checkpoints against each other.
 *
 * Auditioned one at a time they all sound like the speaker - which is true,
 * and is how a checkpoint that had forgotten how to stop was promoted and
 * filled a whole video with noise. Every fault that actually gets reported is
 * intermittent and shows up ACROSS lines, so the only honest comparison
 * generates several lines several times each and counts what goes wrong.
 */
const slot = (id: string) => `compare:${id}`;


const state = (j: ReturnType<typeof getJob>) =>
  j ? { status: j.status, step: j.step, log: j.log.slice(-30), error: j.error,
        rows: (j as any).rows ?? [],
        // How many checkpoints there are to do, so a page that finds a run
        // already in progress can say "3 of 7" instead of showing three rows
        // and nothing else. Each one reloads the model, so this takes long
        // enough that silence reads as broken.
        done: ((j as any).rows ?? []).length,
        total: (j as any).total ?? 0,
        run: (j as any).run ?? '',
        seconds: Math.round(((j.endedAt ?? Date.now()) - j.startedAt) / 1000) }
    : { status: 'idle', rows: [], done: 0, total: 0, run: '' };

/** Whatever the run has written so far, whoever started it. */
function fromFile(id: string) {
  const f = join(DATA, 'work', '_compare', id, 'progress.jsonl');
  if (!existsSync(f)) return { rows: [], total: 0, stopped: '' };
  const rows: any[] = [];
  let total = 0;
  let stopped = '';
  for (const line of readFileSync(f, 'utf8').split('\n')) {
    const t = line.trim();
    if (!t.startsWith('{')) continue;
    try {
      const m = JSON.parse(t);
      if (m.total) total = m.total;
      else if (m.stopped) stopped = m.stopped;
      else if (m.summary) return { rows: m.rows ?? rows, total, stopped };
      else if (m.checkpoint && !m.error) rows.push(m);
    } catch { /* a half-written last line is normal while it runs */ }
  }
  return { rows, total, stopped };
}

export const GET = ({ params }) => {
  const j = getJob(slot(params.id));
  const s = state(j);
  // The file is the truth. A job object lives in a module and does not
  // survive a reload; the run does.
  const f = fromFile(params.id);
  return json({ ...s, rows: f.rows.length ? f.rows : s.rows,
                done: f.rows.length || s.done, total: f.total || s.total,
                stopped: f.stopped, run: (j as any)?.run ?? '' });
};
export const DELETE = ({ params }) => {
  killChild(slot(params.id));
  return json(state(stopJob(slot(params.id))));
};


export const POST = async ({ params, request }) => {
  const body = await request.json().catch(() => ({}));
  const voice = getVoice(params.id);
  if (!voice) return json({ error: 'no such voice' }, { status: 404 });

  // A SPREAD across the run, not the tail of it.
  //
  // This defaulted to the last six checkpoints, which is the "newest is best"
  // assumption every other part of this file exists to argue against - and it
  // is wrong here specifically: both earlier runs bottomed out around step
  // 200, and taking the tail excluded that whole region by construction.
  //
  // `latest` is dropped because it is a pointer to one of the others, and
  // scoring the same weights twice under two names wastes nine minutes and
  // makes the table look like it disagrees with itself.
  // ONE RUN at a time, the newest unless told otherwise.
  //
  // Sampling across every run compares different TRAINING DATA, not different
  // checkpoints: run 1 saw 32 minutes of clips mined from screencasts, run 3
  // saw 54 minutes of purpose-recorded reading. A step from the first has
  // nothing useful to say about a step from the third, and it costs nine
  // minutes to say it.
  const runs = runsOf(params.id);
  const run = (body?.run && runs.find((r) => r.run === body.run)) || runs[runs.length - 1];
  const all: string[] = run
    // step_0000000 is the save taken before the first optimiser step: it IS
    // stock, and stock is always scored, so scoring it costs a slot to learn
    // nothing.
    ? run.steps.filter((s: string) => s.startsWith('step_') && !/^step_0+$/.test(s))
                .map((s: string) => `${run.dir}/${s}`)
    : [];
  // EVERY checkpoint in the run, earliest first, and let patience decide when
  // to stop. Six spread across the run skipped the 200s, which is the stretch
  // that has twice held the best one.
  //
  // Ascending order is what makes stopping legitimate: a run improves and then
  // stops improving, so a stall late in the walk means something, where the
  // same gap between two checkpoints picked at random means nothing.
  all.sort();
  const picked: string[] = Array.isArray(body?.checkpoints) && body.checkpoints.length
    ? body.checkpoints : ['stock', ...all];
  // 0 = score the lot.
  const patience: number = Number.isInteger(body?.patience)
    ? Math.max(0, Math.min(99, body.patience)) : 3;
  const lines: string[] = Array.isArray(body?.lines) && body.lines.length
    // Four of the studio's test lines: scoring runs every line on every seed
    // for every checkpoint, so the list is kept short here.
    ? body.lines.slice(0, 8) : studioSettings().testLines.slice(0, 4);
  const seeds: number[] = Array.isArray(body?.seeds) && body.seeds.length
    ? body.seeds.slice(0, 5) : [11, 22, 33];

  const out = join(DATA, 'work', '_compare', params.id);
  mkdirSync(out, { recursive: true });
  // Progress goes to a FILE, not down a pipe.
  //
  // A pipe is drained only while somebody is listening, and the listener here
  // lives in a module that is replaced on every edit. When that happened the
  // child kept writing until the OS buffer filled and then blocked: eighty-
  // eight minutes elapsed, eight of CPU, holding the card and still reporting
  // itself as running. Reading a file cannot wedge the thing writing it, and
  // a reader that arrives late still sees everything.
  const progress = join(out, 'progress.jsonl');

  const job = startJob(slot(params.id), 'train', async (j) => {
    (j as any).rows = [];
    (j as any).total = picked.length;
    (j as any).run = run?.run ?? '';
    return await new Promise<string>((resolve, reject) => {
      const p = spawn(PY_TTS, [join(NARRATE, 'lab/compare_ckpts.py'),
                               '--voice-ref', voice.reference,
                               '--out', out,
                               '--progress', progress,
                               '--lines', ...lines,
                               '--seeds', ...seeds.map(String),
                               '--patience', String(patience),
                               '--checkpoints', ...picked],
                      // stdout DISCARDED on purpose: progress is in the file,
                      // and an undrained pipe is what wedged this twice.
                      // NOTHING piped. stdout was closed first and the wedge
                      // simply moved to stderr, which model loading fills with
                      // progress bars. The child writes everything it has to
                      // say into files.
                      { cwd: NARRATE, stdio: 'ignore' });
      if (p.pid) holdChild(slot(params.id), p.pid, 'compare');
      let lastErr = '';
      // The job's own view of progress also comes from the file, so nothing
      // depends on this process being listened to.
      const tick = setInterval(() => {
        const f = fromFile(params.id);
        (j as any).rows = f.rows;
        (j as any).total = f.total || (j as any).total;
        if (f.rows.length) note(j, `${f.rows.length} of ${f.total || '?'} scored`);
      }, 5000);
      p.on('close', (code) => {
        clearInterval(tick);
        releaseChild(slot(params.id));
        const f = fromFile(params.id);
        (j as any).rows = f.rows;
        if (code === 0) resolve(`scored ${f.rows.length}`);
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
