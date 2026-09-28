import { json } from '@sveltejs/kit';
import { existsSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { getProject, saveProject, workDir } from '$lib/server/projects';
import { placeAdded, blockLabel, type Segment } from '$lib/types';

/**
 * Add a block where the recording has only dead air — a "Thank you" over the
 * closing images, an introduction before the first line, a sentence between
 * two others.
 *
 * It goes ONLY into free space at the moment asked for, with a margin either
 * side, so it can never be laid over a neighbour. A take that renders longer
 * than its gap still pushes what follows later, exactly as any long take
 * does; the page says so when it happens.
 */
export const POST = async ({ params, request }) => {
  const body = await request.json().catch(() => ({}));
  const p = getProject(params.id);
  if (!p) return json({ error: 'not found' }, { status: 404 });
  const at = Number(body?.at);
  const say = String(body?.say ?? '').trim().slice(0, 600);
  const total = p.duration ?? Math.max(0, ...p.segments.map((s) => s.end));
  if (!Number.isFinite(at) || at < 0 || at > total + 0.01)
    return json({ error: 'pick a moment on the timeline first' }, { status: 400 });

  const placed = placeAdded(p, at, total, say);
  if ('error' in placed) return json({ error: placed.error }, { status: 409 });
  const { start, end } = placed;
  const [lo, hi] = placed.gap;

  // Named after the block it follows: s012 -> s012a, s012b ...; before the
  // first block, s000a. Existing ids never change.
  const before = [...p.segments].filter((s) => s.start < start).pop();
  const base = before ? (/^s\d+/.exec(before.id)?.[0] ?? 's000') : 's000';
  const taken = new Set(p.segments.map((s) => s.id));
  let id = '';
  for (const c of 'abcdefghijklmnopqrstuvwxyz') if (!taken.has(base + c)) { id = base + c; break; }
  if (!id) return json({ error: 'too many blocks added in one place' }, { status: 409 });

  const seg: Segment = {
    id, start, end,
    heard: '', say, sayOriginal: '', cleaned: false, locked: false, added: true
  };
  const at_ = p.segments.findIndex((s) => s.start > seg.start);
  p.segments.splice(at_ < 0 ? p.segments.length : at_, 0, seg);
  const saved = saveProject(p);
  return json({ project: saved, id, label: blockLabel(seg),
                room: +(hi - lo).toFixed(2),
                // Past the last block the take may outlast the picture; the
                // render then holds the final frame, which is fine for a sign-off.
                atEnd: hi >= total - 0.01 });
};

/** Remove an ADDED block, and its takes. Blocks from the recording are left
    out with "drop" instead, which keeps their place and can be undone. */
export const DELETE = async ({ params, url }) => {
  const sid = url.searchParams.get('sid') ?? '';
  const p = getProject(params.id);
  if (!p) return json({ error: 'not found' }, { status: 404 });
  const seg = p.segments.find((s) => s.id === sid);
  if (!seg) return json({ error: 'no such block' }, { status: 404 });
  if (!seg.added) return json({ error: 'only an added block can be removed — use drop for the others' }, { status: 400 });
  p.segments = p.segments.filter((s) => s.id !== sid);
  const work = workDir(p.id);
  for (const f of [`seg_${sid}.wav`, `seg_${sid}.raw.wav`])
    if (existsSync(join(work, f))) rmSync(join(work, f), { force: true });
  return json({ project: saveProject(p) });
};
