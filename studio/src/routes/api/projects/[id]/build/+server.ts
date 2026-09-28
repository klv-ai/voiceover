import { json } from '@sveltejs/kit';
import { existsSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { getJob } from '$lib/server/jobs';
import { getProject, updateProject } from '$lib/server/projects';
import { startBuild } from '$lib/server/build';
import { partsDir } from '$lib/server/uploads';

/**
 * How the first build of a project is going.
 *
 * Falls back to the project's own recorded state when there is no job: the
 * register is in memory, so a dev-server reload loses it while the project on
 * disk still knows perfectly well whether it finished.
 */
export const GET = ({ params }) => {
  const j = getJob(params.id);
  if (j && j.kind === 'build') {
    return json({
      status: j.status, step: j.step, log: j.log, error: j.error,
      done: j.done ?? 0, total: j.total ?? 2
    });
  }
  const p = getProject(params.id);
  if (!p) return json({ status: 'error', error: 'not found' }, { status: 404 });
  if (p.state === 'failed') return json({ status: 'error', error: p.buildError ?? 'build failed' });
  if (p.state === 'building') return json({ status: 'running', step: 'building', done: 0, total: 2, log: [] });
  return json({ status: 'done', step: 'ready', done: 2, total: 2,
                log: [`${p.segments.length} lines — ready`] });
};

/**
 * Try a build that failed again, from what is already on the server — the
 * uploaded video, or the recordings of a joined one. Nothing is sent twice:
 * four recordings are a gigabyte, and a failure after they arrived is no
 * reason to ask for them again.
 */
export const POST = ({ params }) => {
  const p = getProject(params.id);
  if (!p) return json({ error: 'not found' }, { status: 404 });
  if (getJob(p.id)?.status === 'running') return json({ id: p.id, name: p.name, building: true });
  if (p.state !== 'failed') {
    return json({ error: 'only a project that failed to build can be tried again' }, { status: 409 });
  }
  const dir = partsDir(p.id);
  const parts = existsSync(dir)
    ? readdirSync(dir).filter((f) => /^\d{2}-/.test(f)).sort().map((f) => join(dir, f))
    : [];
  if (parts.length < 2 && !existsSync(p.source)) {
    return json({ error: 'the video is not on the server any more — add it again' }, { status: 410 });
  }
  updateProject(p.id, (fresh) => { fresh.state = 'building'; delete fresh.buildError; });
  startBuild({ ...p, state: 'building' }, parts.length >= 2 ? parts : undefined);
  return json({ id: p.id, name: p.name, building: true });
};
