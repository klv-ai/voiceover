import { json } from '@sveltejs/kit';
import { getProject, saveProject, workDir } from '$lib/server/projects';
import { consistencyPass } from '$lib/server/pipeline';
import { editedByHand } from '$lib/server/script';

/**
 * A flow pass over the whole script.
 *
 * Destructive to the text, so the previous wording is kept per line: the page
 * shows which lines moved and offers them back one at a time.
 */
export const POST = async ({ params }) => {
  const p = getProject(params.id);
  if (!p) return json({ error: 'not found' }, { status: 404 });
  const unlocked = p.segments.filter((s) => !s.locked);
  if (!unlocked.length) return json({ error: 'every line is set to keep your voice' }, { status: 400 });
  try {
    const work = workDir(p.id);
    const r = await consistencyPass(p.segments.map((s) => s.say), work, p.settings.cleanModel);
    if (r.lines.length !== p.segments.length) {
      return json({ error: `the model returned ${r.lines.length} lines for ${p.segments.length}` }, { status: 500 });
    }
    const changed: string[] = [];
    // Read BEFORE the loop writes anything: which lines were rewritten by hand.
    const mine = editedByHand(p);
    p.segments.forEach((s, i) => {
      // A line kept as your own voice is not up for rewriting — and nor is a
      // line you have rewritten yourself. The whole script still goes to the
      // model, so your wording shapes how the lines around it read.
      if (s.locked || mine[i] || r.lines[i].trim() === s.say.trim()) return;
      s.sayBeforeFlow = s.say;
      s.say = r.lines[i];
      // Only where the pass actually wrote. A line it left alone may be the
      // operator's own wording, and recording that as machine text would
      // hand it to the polish pass as a draft to rewrite.
      s.sayMachine = r.lines[i];
      changed.push(s.id);
    });
    return json({ project: saveProject(p), changed, rejected: r.rejected });
  } catch (e: any) {
    return json({ error: e.message }, { status: 500 });
  }
};
