import { json } from '@sveltejs/kit';
import { saveStudioSettings, studioSettings } from '$lib/server/settings';
import { verifyWorker } from '$lib/server/run';

export const GET = () => json(studioSettings());

export const PUT = async ({ request }) => {
  const patch = await request.json().catch(() => null);
  if (!patch || typeof patch !== 'object') return json({ error: 'send the settings as JSON' }, { status: 400 });
  const saved = saveStudioSettings(patch);
  // The take checker reads the must-say names once, when it starts.
  verifyWorker.stop('studio settings changed');
  return json(saved);
};
