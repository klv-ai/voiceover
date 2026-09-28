import { json } from '@sveltejs/kit';
import { ollamaModels } from '$lib/server/pipeline';
export const GET = async () => json({ models: await ollamaModels() });
