import { json } from '@sveltejs/kit';
import { adoptSample } from '$lib/server/samples';

/** Make a scored candidate this voice's sample. The old one goes to history. */
export const POST = async ({ params, request }) => {
  const { name } = await request.json().catch(() => ({ name: '' }));
  const r = adoptSample(params.id, String(name ?? ''));
  return 'error' in r ? json(r, { status: 400 }) : json(r);
};
