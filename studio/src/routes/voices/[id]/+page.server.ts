import { error } from '@sveltejs/kit';
import { statSync, existsSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { getVoice, measure, runsOf, latestOf, sessionsOf } from '$lib/server/voices';
import { ollamaModels } from '$lib/server/pipeline';
import { studioSettings } from '$lib/server/settings';

export const load = async ({ params }) => {
  const voice = getVoice(params.id);
  if (!voice) throw error(404, 'no such voice');

  const dir = sessionsOf(params.id);
  const paths = existsSync(dir)
    ? readdirSync(dir).filter((f) => f.endsWith('.wav')).map((f) => join(dir, f)).sort()
    : [];
  const stats = await measure(paths);
  return {
    voice,
    latest: latestOf(params.id),
    runs: runsOf(params.id),
    models: await ollamaModels(),
    // Coined names first: they are the ones a voice model gets wrong on sight.
    defaultModel: studioSettings().rewriteModel,
    names: [...new Set([...studioSettings().coined, ...studioSettings().terms])].slice(0, 8),
    sessions: paths.map((p) => ({
      path: p,
      name: p.split('/').pop(),
      recordedAt: new Date(statSync(p).mtimeMs).toISOString(),
      stats: stats[p]
    }))
  };
};
