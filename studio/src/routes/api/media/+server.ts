import { createReadStream, existsSync, statSync } from 'node:fs';
import { basename, extname } from 'node:path';
import { Readable } from 'node:stream';
import { DATA } from '$lib/server/config';
import { listProjects } from '$lib/server/projects';
import { real, within } from '$lib/server/paths';

const TYPES: Record<string, string> = {
  '.mp4': 'video/mp4', '.mov': 'video/quicktime', '.m4v': 'video/x-m4v',
  '.wav': 'audio/wav', '.m4a': 'audio/mp4'
};

/**
 * Only what the studio made or was given: anything in its data folder, or a
 * project's own source video wherever that lives. This used to serve ANY path
 * it was asked for — ?path=~/.ssh/id_ed25519 included.
 */
function servable(path: string): boolean {
  if (within(DATA, path)) return true;
  const want = real(path);
  return listProjects().some((p) => !!p.source && real(p.source) === want);
}

/** Range-capable file server, so <video> can seek. */
export const GET = async ({ url, request }) => {
  const path = url.searchParams.get('path');
  if (!path || !existsSync(path)) return new Response('not found', { status: 404 });
  if (!servable(path)) return new Response('not a studio file', { status: 403 });
  const size = statSync(path).size;
  const type = TYPES[extname(path).toLowerCase()] ?? 'application/octet-stream';
  // The browser names a download after the last segment of the URL, and every
  // file here is served from "/api/media" — so every render arrived in the
  // operator's downloads folder as "media.mp4", overwriting the last one. The
  // `download` attribute on the anchor cannot fix it either: this is a
  // different path from the page, so only the server may name the file.
  const named: Record<string, string> = url.searchParams.get('download')
    ? { 'Content-Disposition': `attachment; filename="${basename(path).replace(/["\\]/g, '')}"` }
    : {};
  const range = request.headers.get('range');
  if (range) {
    const m = /bytes=(\d*)-(\d*)/.exec(range);
    const start = m && m[1] ? parseInt(m[1]) : 0;
    // Serve a rendered video WHOLE.
    //
    // The cap exists for the 4K/60 source, which is 285 MB here: streaming it
    // in one response starves everything else on a single-threaded dev server,
    // and the audio element fires `waiting` at t=0. But the threshold sat at
    // 48 MB, and a finished render is 52 MB — just over it — so the one file
    // the operator actually watches end to end was the one being handed out in
    // 8 MB pieces. Playback stopped dead at byte 48,000,000, six chunks in,
    // which is 4:21 of a 4:54 video: no error, no ended event, just a pause.
    //
    // Renders and previews are tens of megabytes and belong on the whole-file
    // path; only the master source needs breaking up.
    const chunk = size <= 128_000_000 ? size : 16_000_000;
    const end = m && m[2] ? parseInt(m[2]) : Math.min(start + chunk, size - 1);
    return new Response(Readable.toWeb(createReadStream(path, { start, end })) as any, {
      status: 206,
      headers: {
        'Content-Range': `bytes ${start}-${end}/${size}`,
        'Accept-Ranges': 'bytes',
        'Content-Length': String(end - start + 1),
        'Content-Type': type,
        ...named
      }
    });
  }
  return new Response(Readable.toWeb(createReadStream(path)) as any, {
    headers: { 'Content-Length': String(size), 'Content-Type': type, 'Accept-Ranges': 'bytes', ...named }
  });
};
