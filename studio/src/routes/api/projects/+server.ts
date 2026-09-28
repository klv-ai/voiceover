import { json } from '@sveltejs/kit';
import { listProjects } from '$lib/server/projects';

export const GET = async () => json(listProjects());
