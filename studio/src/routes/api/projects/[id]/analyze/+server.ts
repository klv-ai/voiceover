import { json } from '@sveltejs/kit';
import { getProject, saveProject, workDir } from '$lib/server/projects';
import { analyze, readAnalysis } from '$lib/server/pipeline';

export const POST = async ({ params }) => {
  const p = getProject(params.id);
  if (!p) return json({ error: 'not found' }, { status: 404 });
  try {
    const work = workDir(p.id);
    p.analysis = await analyze(p.source, work);
    const d = (await readAnalysis(p.analysis)) ?? {};
    p.metrics = d.metrics;
    p.duration = d.source?.duration;
    return json(saveProject(p));
  } catch (e: any) {
    return json({ error: e.message }, { status: 500 });
  }
};
