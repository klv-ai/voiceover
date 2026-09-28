import { json } from '@sveltejs/kit';
import { join } from 'node:path';
import { getProject, saveProject, workDir } from '$lib/server/projects';
import { cleanScript } from '$lib/server/pipeline';

export const POST = async ({ params }) => {
  const p = getProject(params.id);
  if (!p) return json({ error: 'not found' }, { status: 404 });
  try {
    const work = workDir(p.id);
    const doc = await cleanScript(join(work, 'requests.json'), work, p.settings.cleanModel);
    const byId = new Map<string, any>((doc.requests ?? []).map((r: any) => [r.id, r]));
    // only replace text the operator has not edited by hand
    p.segments = p.segments.map((s) => {
      const r = byId.get(s.id);
      if (!r || s.say !== s.sayOriginal) return s;
      return { ...s, say: r.say, sayMachine: r.say, cleaned: r.say !== s.sayOriginal };
    });
    return json(saveProject(p));
  } catch (e: any) {
    return json({ error: e.message }, { status: 500 });
  }
};
