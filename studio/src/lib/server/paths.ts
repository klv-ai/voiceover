import { existsSync, realpathSync } from 'node:fs';
import { resolve, sep } from 'node:path';

/**
 * Where a path really is: resolved, and through any symlink, so "../" and a
 * link planted inside the data folder both land where they actually point.
 */
export function real(p: string): string {
  const abs = resolve(p);
  try { return existsSync(abs) ? realpathSync(abs) : abs; } catch { return abs; }
}

/** Is `p` the folder `root` or anything inside it? Compared on real paths,
    never on string prefixes: ".../sessions/../../x" starts with ".../sessions". */
export function within(root: string, p: string): boolean {
  const r = real(root);
  const q = real(p);
  return q === r || q.startsWith(r.endsWith(sep) ? r : r + sep);
}

/**
 * A project, voice or line id that is safe to put in a path: one segment,
 * no separators, not "." or "..". Voice ids include "project:<id>".
 */
const ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$/;
export const safeId = (id: unknown): id is string => typeof id === 'string' && ID.test(id);
