import { json } from '@sveltejs/kit';
import { getProject, updateProject, workDir } from '$lib/server/projects';
import { cannotRender, makeRoom, synthChecked } from '$lib/server/pipeline';
import { signature } from '$lib/types';

export const POST = async ({ params }) => {
  const p = getProject(params.id);
  if (!p) return json({ error: 'not found' }, { status: 404 });
  const seg = p.segments.find((s) => s.id === params.sid);
  if (!seg) return json({ error: 'no such segment' }, { status: 404 });
  const why = cannotRender(p);
  if (why) return json({ error: why }, { status: 400 });
  try {
    const r = await synthChecked(seg, p, workDir(p.id));
    // Re-read before writing: this request has been generating for a minute
    // and its copy of the settings is that old.
    const saved = updateProject(p.id, (fresh) => {
      const s = fresh.segments.find((x) => x.id === seg.id);
      if (!s) return;
      s.audio = r.path;
      s.duration = r.duration;
      s.wpm = r.wpm;
      s.terminalStep = r.terminal_step;
      if (r.seed != null) s.seed = r.seed;
      s.renderedFrom = signature({ ...s, say: seg.say, seed: s.seed }, fresh.settings);
      // Keep what the checker found. It ran either way; discarding it is what
      // left a bad line indistinguishable from a good one in the list.
      s.check = { ok: r.verified !== false, score: r.score ?? null,
                  heard: r.heard, at: new Date().toISOString() };
      // The take may have changed length; make sure it still fits.
      makeRoom(fresh);
    });
    const out = saved?.segments.find((x) => x.id === seg.id) ?? seg;
    // Tell the page when the take is not the line, so it can be re-rolled
    // rather than discovered on playback.
    return json({ segment: out, verified: r.verified !== false, heard: r.heard, score: r.score });
  } catch (e: any) {
    return json({ error: e.message }, { status: 500 });
  }
};
