import { listVoices } from '$lib/server/voices';

export const load = async () => ({ voices: await listVoices() });
