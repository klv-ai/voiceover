import { json } from '@sveltejs/kit';
import { listVoices, createVoice, removeVoice } from '$lib/server/voices';

export const GET = async () => json({ voices: await listVoices() });

/** A new, empty voice, named. Reading, training and choosing its sample all
    happen on the voice's own page. */
export const POST = async ({ request }) => {
  const { name } = await request.json().catch(() => ({}));
  if (!String(name ?? '').trim()) return json({ error: 'give the voice a name' }, { status: 400 });
  return json({ voice: createVoice(String(name)) });
};

/** Moved to data/trash with everything recorded and trained for it. */
export const DELETE = async ({ url }) => {
  const id = url.searchParams.get('id');
  if (!id) return json({ error: 'which voice?' }, { status: 400 });
  if (!removeVoice(id)) return json({ error: 'no such voice' }, { status: 404 });
  return json({ voices: await listVoices() });
};
