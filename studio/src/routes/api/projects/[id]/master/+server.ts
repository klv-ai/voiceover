import { json } from '@sveltejs/kit';
import { getProject, saveProject, workDir } from '$lib/server/projects';
import { master } from '$lib/server/pipeline';

export const POST = async ({ params }) => {
  const p = getProject(params.id);
  if (!p) return json({ error: 'not found' }, { status: 404 });
  try {
    p.outputs.master = await master(p.source, workDir(p.id));
    return json(saveProject(p));
  } catch (e: any) {
    return json({ error: e.message }, { status: 500 });
  }
};
