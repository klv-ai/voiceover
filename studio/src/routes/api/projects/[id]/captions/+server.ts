import { json } from '@sveltejs/kit';
import { existsSync } from 'node:fs';
import { writeFile } from 'node:fs/promises';
import { join, basename, extname } from 'node:path';
import { getProject, workDir } from '$lib/server/projects';
import { NARRATE, PY } from '$lib/server/config';
import { run } from '$lib/server/run';

/**
 * Subtitles for the finished video.
 *
 * Timed against the RENDERED cut, not the source: the glide pass speeds the
 * picture through the dead air between lines, so a line spoken at 3:42 in the
 * recording arrives somewhere earlier in the output, by a different amount for
 * every line. Captions written against the source would drift further out of
 * sync with every gap that was closed.
 *
 * They are generated on demand rather than stored, because the render is the
 * thing they describe: regenerating costs nothing and cannot go stale.
 */
export const GET = async ({ params, url }) => {
  const p = getProject(params.id);
  if (!p) return json({ error: 'not found' }, { status: 404 });
  const fmt = url.searchParams.get('format') === 'srt' ? 'srt' : 'vtt';

  const work = workDir(p.id);
  const plan = join(work, 'glide.plan.json');
  if (!existsSync(plan)) {
    return json({ error: 'render the video first — the captions are timed to that cut' },
                { status: 400 });
  }

  const says = join(work, 'captions.text.json');
  await writeFile(says, JSON.stringify(
    Object.fromEntries(p.segments.filter((s) => !s.drop).map((s) => [s.id, s.say]))
  ));

  const r = await run(PY, [join(NARRATE, 'lab/captions.py'), plan, '--text', says, '--format', fmt],
                      { cwd: NARRATE, timeout: 120_000 });
  if (r.code !== 0 || !r.out.trim()) {
    return json({ error: `could not write the captions:\n${r.err.slice(-600)}` }, { status: 500 });
  }

  const stem = basename(p.outputs.final ?? p.source, extname(p.outputs.final ?? p.source));
  return new Response(r.out, {
    headers: {
      'Content-Type': fmt === 'srt' ? 'application/x-subrip; charset=utf-8' : 'text/vtt; charset=utf-8',
      'Content-Disposition': `attachment; filename="${stem.replace(/["\\]/g, '')}.${fmt}"`
    }
  });
};
