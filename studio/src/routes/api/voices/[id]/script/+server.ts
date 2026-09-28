import { json } from '@sveltejs/kit';
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { run } from '$lib/server/run';
import { NARRATE, PY, OLLAMA } from '$lib/server/config';
import { scriptOf } from '$lib/server/voices';
import { studioSettings } from '$lib/server/settings';

/** The read script belongs to ONE voice: two speakers training at once must
    not be handed each other's passages, or each other's place in them. */
export const GET = ({ params }) => {
  const f = scriptOf(params.id);
  if (!existsSync(f)) return json({ script: null });
  try {
    return json({ script: JSON.parse(readFileSync(f, 'utf8')) });
  } catch {
    return json({ script: null });
  }
};

export const POST = async ({ params, request }) => {
  const body = await request.json().catch(() => ({}));
  const language = (body?.language || 'English').toString().slice(0, 40);
  // Never the rewriter's default. That one is a local 12b chosen for a job
  // that runs per line; this runs once, and sending it to the card costs the
  // voice model its memory.
  const model = (body?.model || studioSettings().rewriteModel).toString();
  const passages = Math.min(120, Math.max(4, Number(body?.passages) || 60));
  // Names the model will get wrong on sight — brands, surnames, acronyms.
  // They go into the SCRIPT so the fine-tune hears them in this speaker's own
  // mouth; a substitution table at synthesis time only papers over a model
  // that never heard the word.
  const learn = (body?.wordsToLearn || '').toString().slice(0, 300);

  const f = scriptOf(params.id);
  mkdirSync(dirname(f), { recursive: true });
  const out = join(dirname(f), 'read_script.new.json');
  const r = await run(PY, [join(NARRATE, 'lab/read_script.py'),
                           '-o', out, '--model', model, '--ollama', OLLAMA,
                           '--language', language, '--passages', String(passages),
                           ...(learn ? ['--words-to-learn', learn] : [])],
                      { cwd: NARRATE, timeout: 900_000 });
  if (!existsSync(out)) {
    return json({ error: `could not write a script:\n${r.err.slice(-800)}` },
                { status: 500 });
  }
  const d = JSON.parse(readFileSync(out, 'utf8'));
  if (d.error) return json({ error: d.error }, { status: 500 });

  // Adding passages must never cost the reader what they have already read.
  let merged = d;
  if (existsSync(f) && !body?.replace) {
    try {
      const old = JSON.parse(readFileSync(f, 'utf8'));
      if (old?.passages?.length) {
        const seen = new Set(old.passages.map((p: any) => p.text.slice(0, 60)));
        const add = d.passages.filter((p: any) => !seen.has(p.text.slice(0, 60)));
        merged = { ...old,
                   passages: [...old.passages, ...add].map((p: any, i: number) =>
                     ({ ...p, id: `p${String(i + 1).padStart(3, '0')}` })) };
        merged.words = merged.passages.reduce(
          (n: number, p: any) => n + p.text.split(/\s+/).length, 0);
        merged.minutes_at_160wpm = Math.round(merged.words / 16) / 10;
      }
    } catch { /* a corrupt old script must not lose the new one */ }
  }
  writeFileSync(f, JSON.stringify(merged, null, 2));
  return json({ script: merged });
};

export const PATCH = async ({ params, request }) => {
  const body = await request.json().catch(() => ({}));
  const f = scriptOf(params.id);
  if (!existsSync(f)) return json({ error: 'no script yet' }, { status: 404 });
  const d = JSON.parse(readFileSync(f, 'utf8'));
  const done = new Set<string>(Array.isArray(body?.done) ? body.done : []);
  d.passages = (d.passages ?? []).map((p: any) =>
    ({ ...p, read: done.has(p.id) ? true : !!p.read }));
  writeFileSync(f, JSON.stringify(d, null, 2));
  return json({ script: d });
};
