import { json } from '@sveltejs/kit';
import { join, basename, extname } from 'node:path';
import { getProject, saveProject, workDir } from '$lib/server/projects';
import { cannotRender, makeRoom, synthChecked } from '$lib/server/pipeline';
import { startJob, getJob, stopJob, note } from '$lib/server/jobs';
import { signature } from '$lib/types';

const state = (j: ReturnType<typeof getJob>) =>
  j ? {
    status: j.status, kind: j.kind, step: j.step, log: j.log, error: j.error,
    done: j.done, total: j.total,
    seconds: Math.round(((j.endedAt ?? Date.now()) - j.startedAt) / 1000)
  } : { status: 'idle' };

export const GET = ({ params }) => json(state(getJob(params.id)));

/** Give up on the rest. Everything already rendered is saved and kept. */
export const DELETE = ({ params }) => json(state(stopJob(params.id)));

export const POST = async ({ params, request }) => {
  const p = getProject(params.id);
  if (!p) return json({ error: 'not found' }, { status: 404 });
  const why = cannotRender(p);
  if (why) return json({ error: why }, { status: 400 });

  const body = await request.json().catch(() => ({}));
  const redoAll = !!body?.all;
  const work = workDir(p.id);

  // What actually needs doing: skip lines kept as the original recording, and
  // skip ones already rendered from exactly this text and these knobs.
  const stale = (s: (typeof p.segments)[number]) =>
    redoAll || !s.audio || s.renderedFrom !== signature(s, p.settings);
  const todo = p.segments.filter((s) => !s.locked && !s.drop && stale(s));
  if (!todo.length) return json({ status: 'idle', step: 'everything is already rendered' });

  const job = startJob(p.id, 'synth-all', async (j) => {
    j.total = todo.length;
    j.done = 0;
    // Said once, at the top, where it is read before committing an hour to a
    // render: which processor is doing this. A box that has silently lost its
    // GPU renders ten times slower and looks identical from here, which is
    // exactly how a CPU render got mistaken for the engine being slow.
    let saidWhere = false;
    for (const seg of todo) {
      // Checked between lines, never mid-render: a take that is already being
      // generated is allowed to finish and be saved.
      if (j.cancel) { note(j, `stopped after ${j.done} of ${todo.length}`); break; }
      note(j, `${seg.id}: ${seg.say.slice(0, 60)}`);
      // Re-read each time so edits made while this runs are not clobbered by a
      // stale snapshot, and so a take survives even if the batch is abandoned.
      const cur = getProject(p.id);
      const live = cur?.segments.find((s) => s.id === seg.id);
      if (!cur || !live || live.locked) { j.done++; continue; }
      // One retry. A wedged worker is replaced by the failure itself, so the
      // second attempt gets a fresh process — losing the line to a transient
      // stall would mean hunting for it by hand afterwards.
      for (let attempt = 1; attempt <= 2; attempt++) {
        try {
          const r = await synthChecked(live, cur, work, (m) => note(j, m));
          if (!saidWhere && r.device) {
            saidWhere = true;
            note(j, r.device === 'cpu'
              ? 'WARNING: generating on the CPU — the GPU is not visible to torch'
              : `generating on ${r.device}`);
          }
          live.audio = r.path;
          live.duration = r.duration;
          live.wpm = r.wpm;
          live.speechWpm = r.speech_wpm;
          live.terminalStep = r.terminal_step;
          // The seed that produced what is on disk, so "Draw again" starts
          // from somewhere other than the take just rejected.
          if (r.seed != null) live.seed = r.seed;
          // Keep the verdict ON THE LINE. Noting it in a job log that the
          // page throws away is what made a wrong take indistinguishable from
          // a right one, and left listening to all 143 as the only way to
          // find it.
          live.check = { ok: r.verified !== false, score: r.score ?? null,
                         heard: r.heard, at: new Date().toISOString() };
          if (r.verified === false) {
            note(j, `${seg.id}: kept the best of 3 tries (${r.score}) — "${(r.heard ?? '').slice(0, 50)}"`);
          }
          live.renderedFrom = signature(live, cur.settings);
          makeRoom(cur);
          saveProject(cur);
          break;
        } catch (e: any) {
          note(j, `${seg.id} attempt ${attempt} failed: ${e.message}`);
          if (attempt === 2 || j.cancel) break;
        }
      }
      j.done++;
    }
    j.step = j.cancel ? `stopped — ${j.done} of ${todo.length} done`
                      : `rendered ${j.done} of ${todo.length}`;
    return j.step;
  });

  return json(state(job));
};
