import { listProjects } from '$lib/server/projects';
export const load = async () => ({ projects: listProjects() });
