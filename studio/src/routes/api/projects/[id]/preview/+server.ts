import { json } from '@sveltejs/kit';
import { writeFile } from 'node:fs/promises';
import { readdirSync, unlinkSync, statSync } from 'node:fs';
import { join, basename, extname } from 'node:path';
import { getProject, updateProject, workDir } from '$lib/server/projects';
import { NARRATE, PY } from '$lib/server/config';
import { run } from '$lib/server/run';
import { assemblePlan, ensureProxy, muxPreview } from '$lib/server/pipeline';

/**
 * One continuous track: the original recording with every rendered line
 * swapped in. Nothing is cut, so the video plays against it unchanged and the
 * operator hears the substitution in context rather than as fragments.
 */
export const POST = async ({ params, request }) => {
  const p = getProject(params.id);
  if (!p) return json({ error: 'not found' }, { status: 404 });
  const work = workDir(p.id);
  const plan = await assemblePlan(p, work);
  const planPath = join(work, 'preview.plan.json');
  await writeFile(planPath, JSON.stringify(plan));
  const out = join(work, 'preview.wav');
  const r = await run(PY, [join(NARRATE, 'lab/assemble.py'), planPath, '-o', out],
                      { cwd: NARRATE, timeout: 600_000 });
  if (r.code !== 0) return json({ error: r.err.slice(-1200) }, { status: 500 });
  try {
    const built = JSON.parse(r.out.trim().split('\n').pop() ?? '{}');
    const ts = Date.now();
    // Hand the BROWSER a compressed copy, never the 62 MB wav.
    //
    // The picture is 4096x2178 at 60fps and the page plays it against this
    // track from the same single-threaded dev server. An uncompressed wav
    // starves the video: the audio element fires `waiting` at t=0 and the
    // browser then pauses the video outright — no error, just silence and a
    // dead play button. It survives the first play only because the file is
    // still cached; the next rebuild changes the cache-buster and it stops.
    // AAC at 160k is ~6 MB for five minutes and the contention disappears.
    // The wav stays on disk as the source of truth for peaks and the render.
    // ONE media element, not two. See ensureProxy: a separate <audio> playing
    // against the 4K master made the browser pause the video outright.
    let served = out;
    try {
      const proxy = await ensureProxy(p, work);
      served = await muxPreview(proxy, out, join(work, `preview.${ts}.mp4`));
      // Keep the PREVIOUS mix as well as the new one. A rebuild takes ~15s and
      // the page is very likely still streaming the old file while it runs;
      // deleting it mid-flight 404s the element's next range request, which
      // stops playback dead and leaves the page silent. Two generations is
      // enough to cover the handover, and older ones are nobody's business.
      const mine = readdirSync(work)
        .filter((f) => /^preview\.\d+\.mp4$/.test(f))
        .sort((a, b) => statSync(join(work, b)).mtimeMs - statSync(join(work, a)).mtimeMs);
      for (const f of mine.slice(2)) {
        try { unlinkSync(join(work, f)); } catch { /* fine */ }
      }
    } catch (e) {
      // Fall back to the bare wav rather than losing the preview entirely.
      served = out;
    }
    // Remember the mix this was assembled from, so a reload can tell whether
    // the file on disk still matches the script or is already behind it.
    const sig = await request.json().then((b) => b?.sig ?? '').catch(() => '');
    // Same rule: assembling took a while, so write only the preview record
    // onto a freshly read project rather than stamping a stale copy back.
    updateProject(p.id, (fresh) => { fresh.preview = { path: served, sig, ts }; });
    return json({ ...built, path: served, ts });
  } catch {
    return json({ error: r.out.slice(-500) || r.err.slice(-500) }, { status: 500 });
  }
};
