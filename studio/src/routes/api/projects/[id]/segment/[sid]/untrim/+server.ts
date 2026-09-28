import { json } from '@sveltejs/kit';
import { copyFileSync, existsSync } from 'node:fs';
import { getProject, updateProject } from '$lib/server/projects';
import { makeRoom, probeDuration, rawOf } from '$lib/server/pipeline';

/**
 * Put back the whole take — undo the end trim — without rendering again.
 *
 * Every take keeps its untouched original beside it (.raw.wav). The tail trim
 * used to cut where the transcriber said the last word ended, which is early:
 * thirteen takes in use were cut while the sound was still at speech level,
 * the end of the last word clipped. The original is the whole line.
 *
 * If the take is longer than its space, it simply runs on: later blocks are
 * pushed along, never the take shortened.
 */
export const POST = async ({ params }) => {
  const p = getProject(params.id);
  if (!p) return json({ error: 'not found' }, { status: 404 });
  const seg = p.segments.find((s) => s.id === params.sid);
  if (!seg?.audio) return json({ error: 'this block has no take yet' }, { status: 400 });
  const raw = rawOf(seg.audio);
  if (!existsSync(raw)) return json({ error: 'there is no untrimmed original for this take' }, { status: 409 });
  const was = (await probeDuration(seg.audio)) ?? 0;
  const whole = (await probeDuration(raw)) ?? 0;
  if (whole - was < 0.05) return json({ segment: seg, restored: 0, note: 'nothing was trimmed from this take' });

  copyFileSync(raw, seg.audio);
  const dur = await probeDuration(seg.audio);

  const saved = updateProject(p.id, (fresh) => {
    const s = fresh.segments.find((x) => x.id === seg.id);
    if (!s) return;
    if (dur) s.duration = dur;
    makeRoom(fresh);
  });
  return json({ segment: saved?.segments.find((x) => x.id === seg.id) ?? seg,
                restored: Math.round((whole - was) * 100) / 100 });
};
