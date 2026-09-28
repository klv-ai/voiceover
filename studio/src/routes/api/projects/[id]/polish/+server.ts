import { json } from '@sveltejs/kit';
import { existsSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { getProject, listProjects, workDir } from '$lib/server/projects';
import { NARRATE, PY, OLLAMA } from '$lib/server/config';
import { studioSettings } from '$lib/server/settings';
import { run } from '$lib/server/run';
import { editedByHand } from '$lib/server/script';

/**
 * A polish pass over the script, as SUGGESTIONS.
 *
 * Nothing here writes a line. The suggestions live beside the project in
 * `polish.json`, each against the exact text it was made for; the page offers
 * them one at a time and a line only changes when one is taken. That is what
 * lets this run over a script the operator is halfway through editing.
 */

const WPS = 3.0;   // the trained voice runs about 180 wpm on its own

const norm = (t?: string) => (t ?? '').replace(/\s+/g, ' ').trim();
const count = (t?: string) => (t ?? '').match(/[A-Za-z0-9']+/g)?.length ?? 0;

function file(id: string) {
  return join(workDir(id), 'polish.json');
}

/** The operator's own before-and-after pairs from every OTHER video. */
function examples(except: string) {
  const out: { said: string; final: string; project: string }[] = [];
  for (const q of listProjects()) {
    if (q.id === except) continue;
    for (const s of q.segments ?? []) {
      if (s.drop || s.locked || !s.sayOriginal || !s.say) continue;
      if (norm(s.say) === norm(s.sayOriginal)) continue;
      out.push({ said: norm(s.sayOriginal), final: norm(s.say), project: q.name || q.id });
    }
  }
  return out;
}


export const GET = ({ params }) => {
  const f = file(params.id);
  if (!existsSync(f)) return json({ suggestions: [], checks: {}, statuses: {} });
  return json(JSON.parse(readFileSync(f, 'utf8')));
};

export const POST = async ({ params }) => {
  const p = getProject(params.id);
  if (!p) return json({ error: 'not found' }, { status: 404 });
  if (!p.segments.length) return json({ error: 'no script yet' }, { status: 400 });

  const edited = editedByHand(p);
  const end = p.duration ?? (p.segments.at(-1)!.end + 2);
  const lines = p.segments.map((s, i) => {
    const next = p.segments[i + 1]?.start ?? end;
    return {
      id: s.id,
      // Unique per line, which block digits are not once a block is added
      // ("s012a" shares 12). The page matches suggestions by id, not by this.
      n: i + 1,
      said: norm(s.sayOriginal),
      now: norm(s.say),
      edited: edited[i],
      // TIME to the next block, in words at the voice's pace — what actually
      // limits a line. Anything past it pushes every later line late.
      budget: Math.max(3, Math.floor((next - s.start) * WPS)),
      // A draft is not made longer than what was said.
      cap: Math.max(3, count(s.sayOriginal)),
      // Kept as the operator's own recording, or left out: not spoken by the
      // voice, so not polished — but still context for its neighbours.
      skip: !!(s.locked || s.drop)
    };
  });

  const work = workDir(p.id);
  const inp = join(work, 'polish.in.json');
  const out = join(work, 'polish.out.json');
  // The studio's own words: the names to spell exactly, and what the
  // transcriber writes instead of them.
  const st = studioSettings();
  writeFileSync(inp, JSON.stringify({ lines, examples: examples(p.id), glossary: st.terms,
                                      coined: st.coined, aliases: st.aliases }));
  const r = await run(PY, [join(NARRATE, 'lab/polish_script.py'), inp, '-o', out,
                           '--model', p.settings.cleanModel || st.rewriteModel,
                           '--host', OLLAMA],
                      { cwd: NARRATE, timeout: 1_800_000 });
  if (!existsSync(out)) return json({ error: `polish failed:\n${r.err.slice(-1200)}` }, { status: 500 });
  const doc = { ...JSON.parse(readFileSync(out, 'utf8')), statuses: {}, summary: r.out.trim().split('\n').pop() };
  writeFileSync(file(p.id), JSON.stringify(doc, null, 1));
  return json(doc);
};

/** Record what the operator did with a suggestion: `accepted` or `dismissed`. */
export const PATCH = async ({ params, request }) => {
  const body = await request.json();
  const f = file(params.id);
  if (!existsSync(f)) return json({ error: 'no polish pass yet' }, { status: 404 });
  const doc = JSON.parse(readFileSync(f, 'utf8'));
  doc.statuses ??= {};
  const ids: string[] = Array.isArray(body?.ids) ? body.ids : body?.id ? [body.id] : [];
  const status = body?.status === 'accepted' || body?.status === 'dismissed' ? body.status : null;
  if (!status || !ids.length) return json({ error: 'need ids and a status' }, { status: 400 });
  for (const id of ids) doc.statuses[id] = status;
  writeFileSync(f, JSON.stringify(doc, null, 1));
  return json(doc);
};
