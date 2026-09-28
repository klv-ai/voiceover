import { json } from '@sveltejs/kit';
import { join } from 'node:path';
import { existsSync } from 'node:fs';
import { getProject, workDir } from '$lib/server/projects';
import { clipOriginal } from '$lib/server/pipeline';

/** The speaker's own audio for this line — what "keep real" will actually use. */
export const POST = async ({ params }) => {
  const p = getProject(params.id);
  if (!p) return json({ error: 'not found' }, { status: 404 });
  const seg = p.segments.find((s) => s.id === params.sid);
  if (!seg) return json({ error: 'no such segment' }, { status: 404 });
  const out = join(workDir(p.id), `orig_${seg.id}.wav`);
  if (!existsSync(out)) await clipOriginal(p.source, seg.start, seg.end, out);
  return json({ path: out });
};
