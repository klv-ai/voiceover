import { within } from '$lib/server/paths';
import { json } from '@sveltejs/kit';
import { writeFile } from 'node:fs/promises';
import { existsSync, mkdirSync, readdirSync, unlinkSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { run } from '$lib/server/run';
import { measure, sessionsOf } from '$lib/server/voices';

/** Reads recorded for one voice, kept where the voice keeps its material. */
const dirOf = (id: string) => {
  const d = sessionsOf(id);
  mkdirSync(d, { recursive: true });
  return d;
};

export const GET = async ({ params }) => {
  const dir = dirOf(params.id);
  const paths = readdirSync(dir).filter((f) => f.endsWith('.wav'))
    .map((f) => join(dir, f)).sort();
  const stats = await measure(paths);
  return json({
    sessions: paths.map((p) => ({
      path: p, name: p.split('/').pop(),
      recordedAt: new Date(statSync(p).mtimeMs).toISOString(),
      stats: stats[p]
    }))
  });
};

export const POST = async ({ params, request }) => {
  const buf = Buffer.from(await request.arrayBuffer());
  if (buf.length < 100_000) {
    return json({ error: 'that recording was too short to be useful' }, { status: 400 });
  }
  const dir = dirOf(params.id);
  const stamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
  const raw = join(dir, `${stamp}.webm`);
  const out = join(dir, `${stamp}.wav`);
  await writeFile(raw, buf);
  // Same 48k mono PCM as every take, so the corpus is one consistent format.
  const r = await run('ffmpeg', ['-v', 'error', '-i', raw, '-map', '0:a:0', '-vn',
                                 '-ac', '1', '-ar', '48000', '-c:a', 'pcm_s16le',
                                 out, '-y'], { timeout: 600_000 });
  try { unlinkSync(raw); } catch { /* the wav is what matters */ }
  if (!existsSync(out)) {
    return json({ error: `could not decode the recording:\n${r.err.slice(-400)}` },
                { status: 500 });
  }
  const stats = await measure([out]);
  return json({ path: out, stats: stats[out] });
};

export const DELETE = async ({ params, url }) => {
  const path = url.searchParams.get('path') ?? '';
  const dir = sessionsOf(params.id);
  // Under this voice's own folder, and nowhere else. Say which of the two
  // reasons it failed, because "no such session" for a file plainly listed on
  // screen is how a refused delete looks like a delete that did nothing.
  if (!within(dir, path)) {
    return json({ error: 'that recording does not belong to this voice' },
                { status: 400 });
  }
  if (!existsSync(path)) return json({ error: 'already gone' }, { status: 404 });
  unlinkSync(path);
  return json({ ok: true });
};
