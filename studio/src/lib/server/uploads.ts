import { createWriteStream, existsSync, rmSync, statSync } from 'node:fs';
import { pipeline } from 'node:stream/promises';
import { Readable } from 'node:stream';
import { basename, extname, join } from 'node:path';
import { MEDIA } from './config';

/** A batch of recordings being sent for one project; becomes its id. */
export const BATCH = /^[0-9a-f]{8}$/;
export const partsDir = (batch: string) => join(MEDIA, batch, 'parts');

export const VIDEO = new Set(['.mov', '.mp4', '.m4v', '.mkv', '.webm', '.avi']);

/** Keep a filename that is safe to put in a shell, a URL and a filesystem,
    without inventing a new one the operator will not recognise. */
export function safeName(raw: string) {
  const base = basename(raw).replace(/[/\\]/g, '');
  const ext = extname(base).toLowerCase();
  const stem = base.slice(0, base.length - ext.length)
    .replace(/[^A-Za-z0-9._ -]+/g, '-')
    .replace(/-{2,}/g, '-')
    .trim() || 'video';
  return { stem, ext };
}

/**
 * Stream a request body to `dest` as it arrives, never into memory: these are
 * 4K screencasts, ~100 MB for four minutes, and buffering one is how a server
 * falls over on the second concurrent upload. A half-written or empty file is
 * removed — it would probe as a video and analyse to nonsense.
 */
export async function receive(body: ReadableStream | null, dest: string): Promise<string | null> {
  if (!body) return 'no file in the request';
  try {
    await pipeline(Readable.fromWeb(body as any), createWriteStream(dest));
  } catch (e: any) {
    rmSync(dest, { force: true });
    return `upload failed: ${e?.message ?? e}`;
  }
  if (!existsSync(dest) || statSync(dest).size < 1024) {
    rmSync(dest, { force: true });
    return 'the upload arrived empty';
  }
  return null;
}
