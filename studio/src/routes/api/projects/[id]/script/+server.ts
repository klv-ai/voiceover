import { json } from '@sveltejs/kit';
import { getProject, saveProject, workDir } from '$lib/server/projects';
import { buildScript } from '$lib/server/pipeline';
import type { Segment } from '$lib/types';

export const POST = async ({ params }) => {
  const p = getProject(params.id);
  if (!p) return json({ error: 'not found' }, { status: 404 });
  if (!p.analysis) return json({ error: 'analyze first' }, { status: 400 });
  try {
    const doc = await buildScript(p.analysis, workDir(p.id), p.settings.voiceRef);
    p.segments = (doc.requests ?? []).map(
      (r: any): Segment => ({
        id: r.id,
        start: r.span[0],
        end: r.span[1],
        heard: r.heard ?? '',
        say: r.say,
        sayOriginal: r.say,
        cleaned: false,
        locked: false
      })
    );
    return json(saveProject(p));
  } catch (e: any) {
    return json({ error: e.message }, { status: 500 });
  }
};
