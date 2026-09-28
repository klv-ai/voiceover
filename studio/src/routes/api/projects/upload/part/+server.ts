import { json } from '@sveltejs/kit';
import { randomUUID } from 'node:crypto';
import { mkdirSync, readdirSync, rmSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { getProject } from '$lib/server/projects';
import { VIDEO, BATCH, partsDir, safeName, receive } from '$lib/server/uploads';

/**
 * One recording of a video made in sittings. Send them one at a time, in
 * order (`index` from 1); the first call is given a batch id to send the rest
 * with, and `upload/join` turns the batch into a project.
 *
 * The position is in the filename ("02-take two.mov"), so the order is the
 * order the operator set, whatever the files are called. Sending a position
 * again replaces it — a retry after a dropped connection.
 */
export const POST = async ({ request, url }) => {
  const { stem, ext } = safeName(url.searchParams.get('filename') ?? '');
  if (!VIDEO.has(ext)) {
    return json({ error: `${ext || 'that'} is not a video I can read — try .mov or .mp4` },
                { status: 400 });
  }
  const index = Number(url.searchParams.get('index'));
  if (!Number.isInteger(index) || index < 1 || index > 99) {
    return json({ error: 'which recording is this? (index 1-99)' }, { status: 400 });
  }
  let batch = url.searchParams.get('batch') ?? '';
  if (batch && !BATCH.test(batch)) return json({ error: 'bad batch id' }, { status: 400 });
  if (!batch) batch = randomUUID().slice(0, 8);
  // Never write into a project that already exists.
  if (getProject(batch)) return json({ error: 'that batch is already a project' }, { status: 409 });

  const dir = partsDir(batch);
  mkdirSync(dir, { recursive: true });
  const pos = String(index).padStart(2, '0');
  for (const f of readdirSync(dir)) if (f.startsWith(`${pos}-`)) rmSync(join(dir, f), { force: true });
  const dest = join(dir, `${pos}-${stem}${ext}`);
  const failed = await receive(request.body, dest);
  if (failed) return json({ error: failed }, { status: failed.startsWith('upload failed') ? 500 : 400 });
  return json({ batch, index, size: statSync(dest).size });
};
