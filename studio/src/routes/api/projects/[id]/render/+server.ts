import { json } from '@sveltejs/kit';
import { writeFile } from 'node:fs/promises';
import { join, basename, extname } from 'node:path';
import { getProject, saveProject, workDir } from '$lib/server/projects';
import { NARRATE, PY } from '$lib/server/config';
import { run } from '$lib/server/run';
import { renderFinal, assemblePlan } from '$lib/server/pipeline';
import { startJob, getJob, note } from '$lib/server/jobs';

/** How the render is going. Polled while it runs. */
export const GET = ({ params }) => {
  const j = getJob(params.id);
  if (!j) return json({ status: 'idle' });
  return json({
    status: j.status, kind: j.kind, step: j.step, log: j.log, error: j.error, output: j.output,
    done: j.done, total: j.total,
    seconds: Math.round(((j.endedAt ?? Date.now()) - j.startedAt) / 1000)
  });
};

export const POST = async ({ params }) => {
  const p = getProject(params.id);
  if (!p) return json({ error: 'not found' }, { status: 404 });
  if (!p.segments.some((s) => s.audio || s.locked))
    return json({ error: 'nothing to render yet — synthesise some lines first' }, { status: 400 });

  const work = workDir(p.id);
  const job = startJob(p.id, 'render', async (j) => {
    const step = (s: string, d = '') => note(j, s, d);

    // Assemble from scratch rather than trusting whatever preview.wav happens
    // to be on disk: the placements the retimer works from have to describe
    // the SAME audio that ends up in the file.
    step('assembling the mix');
    const plan = await assemblePlan(p, work);
    const planPath = join(work, 'render.plan.json');
    await writeFile(planPath, JSON.stringify(plan));
    const wav = join(work, 'render.wav');
    const r = await run(PY, [join(NARRATE, 'lab/assemble.py'), planPath, '-o', wav],
                        { cwd: NARRATE, timeout: 900_000 });
    if (r.code !== 0) throw new Error(r.err.slice(-1200) || 'assemble failed');
    const asm = JSON.parse(r.out.trim().split('\n').pop() ?? '{}');
    step('assembling the mix', `${asm.placed?.length ?? 0} lines on a ${asm.duration}s timeline`);

    const out = await renderFinal(p, work, asm.placed, wav, step);

    const fresh = getProject(p.id);
    if (fresh) saveProject({ ...fresh, outputs: { ...fresh.outputs, final: out } });
    step('done', out);
    return out;
  });

  return json({ status: job.status, step: job.step, id: job.id });
};
