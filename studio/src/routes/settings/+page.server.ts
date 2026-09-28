import { studioSettings } from '$lib/server/settings';
import { ollamaModels } from '$lib/server/pipeline';

export const load = async () => ({ settings: studioSettings(), models: await ollamaModels() });
