import { json } from '@sveltejs/kit';
import { promote } from '$lib/server/voices';
import { startSampleSearch } from '$lib/server/samples';

/**
 * Say which checkpoint this voice IS.
 *
 * Not "the newest": across the first run here loss/stop tripled while the
 * timbre kept improving, so the last checkpoint sounded most like the speaker
 * and had lost its sense of an ending. Which one to trust is a judgement made
 * by listening, and this is where that judgement is written down.
 */
export const POST = async ({ params, request }) => {
  const { checkpoint } = await request.json().catch(() => ({ checkpoint: '' }));
  if (!promote(params.id, checkpoint ?? ''))
    return json({ error: 'no such voice' }, { status: 404 });
  // A sample is only good for the weights it is paired with, so a new
  // checkpoint starts a search for the sample that suits it. It ends at the
  // operator's ear: candidates are scored, nothing is adopted without a click.
  // If the card is busy it simply does not start; the page offers it by hand.
  const search = checkpoint ? startSampleSearch(params.id, 'a new checkpoint was promoted') : null;
  return json({ ok: true, latest: checkpoint ?? '',
                sampleSearch: search && 'error' in search ? search.error : search ? 'started' : '' });
};
