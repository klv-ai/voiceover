import { error } from '@sveltejs/kit';
import { getProject } from '$lib/server/projects';
import { ollamaModels } from '$lib/server/pipeline';
import { listVoices, listLoras, latestOf } from '$lib/server/voices';

export const load = async ({ params }) => {
  const project = getProject(params.id);
  if (!project) throw error(404, 'no such project');
  // The voices are listed here so the reference can be CHOSEN rather than
  // typed. It was a free-text path field, which is fine for the person who
  // built the file and useless for picking between two speakers — and a
  // mistyped path fails by silently cloning nobody.
  // Whether the chosen voice actually HAS a promoted checkpoint decides
  // whether "from the model alone" can do anything, and the panel has to be
  // able to say so before a twenty-minute render rather than after.
  return { project, models: await ollamaModels(), voices: await listVoices(),
           loras: listLoras(),
           latest: project.settings.voice ? latestOf(project.settings.voice) : null };
};
