import { json } from '@sveltejs/kit';
import { getJob, stopJob } from '$lib/server/jobs';
import { killChild } from '$lib/server/run';
import { getVoice } from '$lib/server/voices';
import { readSearch, slot, startSampleSearch } from '$lib/server/samples';

/** The sample search: its progress, its results, and the sample in use. */
export const GET = ({ params }) => {
  const j = getJob(slot(params.id));
  const voice: any = getVoice(params.id);
  return json({
    status: j?.status ?? 'idle', step: j?.step ?? '', error: j?.error ?? '',
    seconds: j ? Math.round(((j.endedAt ?? Date.now()) - j.startedAt) / 1000) : 0,
    ...readSearch(params.id),
    current: voice?.sample ?? null
  });
};

export const POST = async ({ params }) => {
  const r = startSampleSearch(params.id);
  if ('error' in r && typeof r.error === 'string' && !('status' in r))
    return json({ error: r.error }, { status: 409 });
  return json({ ok: true });
};

export const DELETE = ({ params }) => {
  killChild(slot(params.id));
  stopJob(slot(params.id));
  return json({ ok: true });
};
