import { json } from '@sveltejs/kit';
import { deleteProject, getProject, saveProject } from '$lib/server/projects';
import { getJob } from '$lib/server/jobs';

export const GET = async ({ params }) => {
  const p = getProject(params.id);
  return p ? json(p) : json({ error: 'not found' }, { status: 404 });
};

export const PATCH = async ({ params, request }) => {
  // READ THE PROJECT AFTER THE BODY, NEVER BEFORE IT.
  //
  // `await` yields, and two saves that overlap across that yield both read the
  // same state and both write their own copy of it — so whichever finishes
  // last silently reverts the other. The operator sees a line he has just
  // rewritten go back to its previous wording, and sees that previous wording
  // turn up again in a line he has since moved on from. There must be no
  // suspension point between reading this file and writing it.
  const patch = await request.json();
  const p = getProject(params.id);
  if (!p) return json({ error: 'not found' }, { status: 404 });
  // Settings arrive as a DIFF of what the operator actually touched, never the
  // whole object. Sending the whole object made a page that loaded before some
  // server-side change put every old value back — that is how the voice
  // reference kept reverting mid-render. Rejecting the whole object instead
  // was worse: it silently dropped real edits, so the rate slider snapped back
  // the moment anything else had bumped the revision.
  if (patch.settings) p.settings = { ...p.settings, ...patch.settings };
  if (patch.segments) {
    // MERGE, never replace. The page owns the words and the knobs; the
    // pipeline owns everything it produced. Replacing the array wholesale let
    // any page that loaded before a recording wipe all 37 `human` links the
    // moment it saved — the wavs survived on disk but the project forgot them.
    // Keep this in step with the client's list in +page.svelte. `stretch` was
    // missing here, so every stretch the operator set was accepted by the UI,
    // sent, and silently dropped — the classic whitelist drift, and silent
    // because a rejected field looks exactly like one that was never set.
    const EDITABLE = ['say', 'sayOriginal', 'sayBeforeFlow', 'sayBeforePolish', 'cleaned', 'locked',
                      'drop', 'holdGap', 'offset', 'stretch', 'hold', 'seed', 'over', 'direction'] as const;
    const incoming = new Map((patch.segments as any[]).map((s) => [s.id, s]));
    for (const seg of p.segments) {
      const from = incoming.get(seg.id);
      if (!from) continue;
      for (const k of EDITABLE) {
        if (k in from) (seg as any)[k] = from[k];
      }
    }
  }
  if (patch.name) p.name = patch.name;
  return json(saveProject(p));
};

/** Delete a project — moved to the trash folder, see deleteProject. Not while
    something is still working on it: the work would fail halfway, or finish
    and try to save a project that is no longer there. */
export const DELETE = async ({ params }) => {
  const j = getJob(params.id);
  if (j?.status === 'running') {
    return json({ error: `still ${j.kind === 'build' ? 'building' : 'rendering'} — stop it first, or wait` },
                { status: 409 });
  }
  const done = deleteProject(params.id);
  if (!done) return json({ error: 'not found' }, { status: 404 });
  return json({ deleted: true, ...done });
};
