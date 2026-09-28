import { json } from '@sveltejs/kit';
import { existsSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { getProject, saveProject } from '$lib/server/projects';
import { startBuild } from '$lib/server/build';
import { MEDIA } from '$lib/server/config';
import { BATCH, partsDir, safeName } from '$lib/server/uploads';
import type { Project } from '$lib/types';
import { newProjectSettings } from '$lib/server/settings';

/**
 * Make a project from the recordings sent to `upload/part`: joined into one
 * video in the order they were sent, then built as any other project is.
 * The recordings themselves are kept beside the joined video.
 */
export const POST = async ({ request }) => {
  const { batch, name } = await request.json().catch(() => ({}));
  if (!BATCH.test(batch ?? '')) return json({ error: 'bad batch id' }, { status: 400 });
  if (getProject(batch)) return json({ error: 'that batch is already a project' }, { status: 409 });
  const dir = partsDir(batch);
  const parts = existsSync(dir) ? readdirSync(dir).filter((f) => /^\d{2}-/.test(f)).sort() : [];
  if (parts.length < 2) return json({ error: 'send at least two recordings to join' }, { status: 400 });

  const first = parts[0].replace(/^\d{2}-/, '');
  const title = String(name ?? '').trim() || first.replace(/\.[^.]+$/, '');
  const p: Project = {
    id: batch,
    name: title,
    source: join(MEDIA, batch, `${safeName(title + '.mov').stem}.mov`),
    createdAt: new Date().toISOString(),
    state: 'building',
    settings: newProjectSettings(),
    segments: [],
    outputs: {},
    parts: parts.map((f) => ({ name: f.replace(/^\d{2}-/, ''), start: 0, duration: 0 }))
  };
  saveProject(p);
  startBuild(p, parts.map((f) => join(dir, f)));
  return json({ id: p.id, name: p.name, parts: parts.length, building: true });
};
