import { json } from '@sveltejs/kit';
import { existsSync, renameSync } from 'node:fs';
import { getProject, saveProject } from '$lib/server/projects';
import { blockAt, blockLabel, cutAt, cutCheck, sectionAt, SLICE_NEAR, mergeCheck, mergedBlock,
         MASK_PRESETS, tidyMask, type Mask, type Segment } from '$lib/types';

/**
 * The blade, the delete, and the undo — cutting sections out of the finished
 * video the way a slice-and-delete does in an editor.
 *
 *   slice    an edit point at `at`. Never inside a block: that would cut
 *            narration in half.
 *   unslice  remove the slice nearest `at`.
 *   cut      delete the section `at` falls in — from the slice before it (or
 *            the start) to the slice after it (or the end). Blocks wholly
 *            inside go with it; a block across either edge refuses the cut.
 *   restore  put back the deleted section containing `at`, and its blocks.
 *
 * Nothing is removed from the recording or the takes: cuts are applied when
 * the final video is made, to picture and sound together.
 */
export const POST = async ({ params, request }) => {
  const body = await request.json().catch(() => ({}));
  const p = getProject(params.id);
  if (!p) return json({ error: 'not found' }, { status: 404 });
  // Blur masks: rectangles over the picture for a stretch of time.
  if (body?.op === 'mask-add' || body?.op === 'mask-update' || body?.op === 'mask-remove')
    return maskOp(p, body);
  // Merging works on blocks, not on a moment.
  if (body?.op === 'merge') return merge(p, Array.isArray(body?.ids) ? body.ids.map(String) : []);
  if (body?.op === 'unmerge') return unmerge(p, String(body?.id ?? ''));
  const at = Number(body?.at);
  const total = p.duration ?? 0;
  if (!Number.isFinite(at) || at < 0 || at > total + 0.01 || total <= 0)
    return json({ error: 'pick a moment on the timeline first' }, { status: 400 });
  p.slices ??= [];
  p.cuts ??= [];

  switch (body?.op) {
    case 'slice': {
      const b = blockAt(p, at);
      if (b) return json({ error: `block ${blockLabel(b)} is playing there — slice in the air around it` }, { status: 409 });
      if (cutAt(p, at)) return json({ error: 'that part is already deleted' }, { status: 409 });
      if (at < 0.05 || at > total - 0.05) return json({ error: 'the start and end are edges already' }, { status: 409 });
      if (!p.slices.some((x) => Math.abs(x - at) < 0.05)) {
        p.slices.push(+at.toFixed(3));
        p.slices.sort((a, b) => a - b);
      }
      break;
    }
    case 'unslice': {
      let best = -1, dist = SLICE_NEAR;
      p.slices.forEach((x, i) => { if (Math.abs(x - at) <= dist) { dist = Math.abs(x - at); best = i; } });
      if (best < 0) return json({ error: 'no slice there' }, { status: 409 });
      const x = p.slices[best];
      // A slice that bounds a deleted section stays: it IS that section's edge.
      if (p.cuts.some((c) => Math.abs(c.from - x) < 0.01 || Math.abs(c.to - x) < 0.01))
        return json({ error: 'that slice is the edge of a deleted section — restore the section first' }, { status: 409 });
      p.slices.splice(best, 1);
      break;
    }
    case 'cut': {
      if (cutAt(p, at)) return json({ error: 'that part is already deleted' }, { status: 409 });
      const [a, b] = sectionAt(p, at, total);
      if (a <= 0.001 && b >= total - 0.001)
        return json({ error: 'slice first — that would delete the whole video' }, { status: 409 });
      const check = cutCheck(p, a, b);
      if ('error' in check) return json({ error: check.error }, { status: 409 });
      for (const s of p.segments) if (check.inside.includes(s.id)) s.drop = true;
      p.cuts.push({ from: a, to: b, dropped: check.inside });
      p.cuts.sort((x, y) => x.from - y.from);
      break;
    }
    case 'restore': {
      const c = cutAt(p, at);
      if (!c) return json({ error: 'nothing deleted there' }, { status: 409 });
      p.cuts = p.cuts.filter((x) => x !== c);
      for (const s of p.segments) if (c.dropped?.includes(s.id)) s.drop = false;
      break;
    }
    default:
      return json({ error: 'unknown operation' }, { status: 400 });
  }
  return json({ project: saveProject(p) });
};

/** Where a block's take is set aside while it is merged into another. */
const aside = (wav: string) => wav.replace(/\.wav$/, '.premerge.wav');
const rawOf = (wav: string) => wav.replace(/\.wav$/, '.raw.wav');

function move(from: string, to: string) {
  for (const [a, b] of [[from, to], [rawOf(from), rawOf(to)]])
    if (existsSync(a)) renameSync(a, b);
}

/**
 * Adjacent blocks become one — a thought the recording split in two, usually
 * around an "um". The joined line has no take yet; each original take is SET
 * ASIDE (renamed), because rendering the merged block writes to the first
 * block's file and would otherwise overwrite the take a split needs back.
 */
function merge(p: any, ids: string[]) {
  const check = mergeCheck(p, ids);
  if ('error' in check) return json({ error: check.error }, { status: 409 });
  const run = check.run as Segment[];
  const merged = mergedBlock(run);
  for (const orig of merged.mergedFrom ?? []) {
    if (orig.audio && existsSync(orig.audio)) {
      move(orig.audio, aside(orig.audio));
      orig.audio = aside(orig.audio);
    }
  }
  const i = p.segments.indexOf(run[0]);
  p.segments.splice(i, run.length, merged);
  return json({ project: saveProject(p), id: merged.id,
                label: `${blockLabel(run[0])}–${blockLabel(run[run.length - 1])}` });
}

/** Split a merged block back into the blocks it came from, takes and all. */
function unmerge(p: any, id: string) {
  const i = p.segments.findIndex((s: Segment) => s.id === id);
  const seg: Segment | undefined = p.segments[i];
  if (!seg?.mergedFrom?.length) return json({ error: 'that block was not merged' }, { status: 409 });
  // The merged take goes: it said the joined line, which no longer exists.
  if (seg.audio && existsSync(seg.audio)) move(seg.audio, seg.audio.replace(/\.wav$/, '.merged.wav'));
  const back = seg.mergedFrom.map((o) => {
    if (o.audio && o.audio.endsWith('.premerge.wav') && existsSync(o.audio)) {
      const home = o.audio.replace(/\.premerge\.wav$/, '.wav');
      move(o.audio, home);
      o.audio = home;
    }
    return o;
  });
  p.segments.splice(i, 1, ...back);
  return json({ project: saveProject(p), ids: back.map((b) => b.id) });
}

/**
 * Blur masks. Stored on the source timeline, in fractions of the frame, so
 * they follow every cut and every glided gap without being told, and land on
 * the same pixels whatever size the picture is shown at.
 *
 *   mask-add     {at, preset}   a new mask from `at`, ten seconds long — or
 *                {at, x, y, w, h} for one drawn on the picture
 *   mask-update  {id, ...}      move, resize, or change when it starts or ends
 *   mask-remove  {id}
 */
function maskOp(p: any, body: any) {
  const total = p.duration ?? 0;
  p.masks ??= [];
  if (body.op === 'mask-add') {
    const at = Number(body.at);
    if (!Number.isFinite(at) || total <= 0) return json({ error: 'pick a moment on the timeline first' }, { status: 400 });
    // A rectangle drawn on the picture, or one of the presets.
    const drawn = ['x', 'y', 'w', 'h'].every((k) => Number.isFinite(Number(body[k])));
    const box = drawn ? { x: Number(body.x), y: Number(body.y), w: Number(body.w), h: Number(body.h) }
                      : (MASK_PRESETS[String(body.preset)] ?? MASK_PRESETS.box);
    let n = p.masks.length + 1;
    while (p.masks.some((m: Mask) => m.id === `m${n}`)) n++;
    const m = tidyMask({ id: `m${n}`, from: at, to: at + 10, x: box.x, y: box.y, w: box.w, h: box.h }, total);
    p.masks.push(m);
    p.masks.sort((a: Mask, b: Mask) => a.from - b.from);
    return json({ project: saveProject(p), id: m.id });
  }
  const i = p.masks.findIndex((m: Mask) => m.id === body.id);
  if (i < 0) return json({ error: 'no such mask' }, { status: 404 });
  if (body.op === 'mask-remove') {
    p.masks.splice(i, 1);
    return json({ project: saveProject(p) });
  }
  const cur = p.masks[i];
  const next: Mask = { ...cur };
  for (const k of ['from', 'to', 'x', 'y', 'w', 'h'] as const)
    if (body[k] !== undefined && Number.isFinite(Number(body[k]))) next[k] = Number(body[k]);
  p.masks[i] = tidyMask(next, total);
  return json({ project: saveProject(p) });
}
