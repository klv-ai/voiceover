import type { Handle } from '@sveltejs/kit';
import { safeId } from '$lib/server/paths';

/**
 * Two checks every request passes before any route sees it.
 *
 * IDS. Route ids (project, voice, line) go straight into filesystem paths, and
 * SvelteKit decodes "%2F" inside a param — so "..%2F..%2Fsomething" would
 * otherwise reach join() as a path that climbs out of the data folder.
 *
 * OTHER WEBSITES. The studio has no login: it is a local tool. But a page open
 * in the same browser can still POST to it — a request with no content type
 * is a "simple" request that needs no permission, and the routes read the
 * body regardless. That is enough for any website to start GPU jobs, upload
 * until the disk fills, or delete a project. A browser always says where a
 * cross-site request comes from, so anything that changes state must come
 * from the studio's own pages (or from no page at all, like curl).
 */
export const handle: Handle = async ({ event, resolve }) => {
  for (const v of Object.values(event.params ?? {})) {
    if (v !== undefined && !safeId(v)) return new Response('bad id', { status: 400 });
  }
  const m = event.request.method;
  if (m !== 'GET' && m !== 'HEAD' && m !== 'OPTIONS') {
    const origin = event.request.headers.get('origin');
    if (origin && origin !== event.url.origin) {
      return new Response('cross-site request refused', { status: 403 });
    }
  }
  return resolve(event);
};
