import { json } from '@sveltejs/kit';
import { randomUUID } from 'node:crypto';
import { mkdirSync, statSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { saveProject } from '$lib/server/projects';
import { startBuild } from '$lib/server/build';
import { MEDIA } from '$lib/server/config';
import { VIDEO, safeName, receive } from '$lib/server/uploads';
import type { Project } from '$lib/types';
import { newProjectSettings } from '$lib/server/settings';

/**
 * Take the video itself, not a path to it.
 *
 * Pasting an absolute filesystem path works on the machine the studio happens
 * to be running on and nowhere else — there is no `/Users/…/Desktop` on a
 * server, and the operator should not have to know where the process's working
 * directory is. The file is streamed to disk as it arrives rather than read
 * into memory: these are 4K screencasts, ~100 MB for four minutes, and
 * buffering one is how a server falls over on the second concurrent upload.
 */
export const POST = async ({ request, url }) => {
  const { stem, ext } = safeName(url.searchParams.get('filename') ?? 'video.mov');
  if (!VIDEO.has(ext)) {
    return json({ error: `${ext || 'that'} is not a video I can read — try .mov or .mp4` },
                { status: 400 });
  }
  const id = randomUUID().slice(0, 8);
  const dir = join(MEDIA, id);
  mkdirSync(dir, { recursive: true });
  const dest = join(dir, stem + ext);
  const failed = await receive(request.body, dest);
  if (failed) {
    rmSync(dir, { recursive: true, force: true });
    return json({ error: failed }, { status: failed.startsWith('upload failed') ? 500 : 400 });
  }

  const p: Project = {
    id,
    name: (url.searchParams.get('name') ?? '').trim() || stem,
    source: dest,
    createdAt: new Date().toISOString(),
    state: 'building',
    settings: newProjectSettings(),
    segments: [],
    outputs: {}
  };
  saveProject(p);

  startBuild(p);

  return json({ id, name: p.name, size: statSync(dest).size, building: true });
};

/*
 * DEPLOYING THIS: adapter-node rejects any request body over 512 KB by
 * default, so every upload here would fail with a 413 the moment this leaves a
 * dev server — and it is invisible in development, because Vite does not apply
 * the limit. `npm start` sets BODY_SIZE_LIMIT=Infinity for that reason; a real
 * deployment behind nginx needs `client_max_body_size` raised to match.
 */
