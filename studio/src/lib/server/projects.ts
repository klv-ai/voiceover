import { mkdirSync, readFileSync, writeFileSync, readdirSync, existsSync, appendFileSync, renameSync } from 'node:fs';
import { join } from 'node:path';
import { DATA, MEDIA, PROJECTS, WORK } from './config';
import { safeId } from './paths';
import { DEFAULT_SETTINGS } from '$lib/types';
import type { Project } from '$lib/types';

mkdirSync(PROJECTS, { recursive: true });
mkdirSync(WORK, { recursive: true });

export const workDir = (id: string) => {
  if (!safeId(id)) throw new Error(`bad project id: ${id}`);
  const d = join(WORK, id);
  mkdirSync(d, { recursive: true });
  return d;
};

export function listProjects(): Project[] {
  if (!existsSync(PROJECTS)) return [];
  return readdirSync(PROJECTS)
    .filter((f) => f.endsWith('.json'))
    .map((f) => JSON.parse(readFileSync(join(PROJECTS, f), 'utf8')) as Project)
    .sort((a, b) => b.createdAt.localeCompare(a.createdAt));
}

export function getProject(id: string): Project | null {
  if (!safeId(id)) return null;
  const p = join(PROJECTS, `${id}.json`);
  if (!existsSync(p)) return null;
  const doc = JSON.parse(readFileSync(p, 'utf8')) as Project;
  // A project saved before a setting existed comes back MISSING that setting,
  // which reads as false and silently disables the feature for every project
  // made until today. Fill in only what is absent; anything the operator has
  // actually set, including an explicit false, is left alone.
  doc.settings = { ...DEFAULT_SETTINGS, ...(doc.settings ?? {}) };
  return doc;
}

/**
 * Re-read, mutate, save — for anything that takes time.
 *
 * A route that loads the project, spends a minute synthesising, then saves its
 * in-memory copy writes back the settings AS THEY WERE A MINUTE AGO. That is
 * how the voice reference kept reverting mid-experiment: the render under test
 * was undoing the setting it was supposed to be testing. Anything slower than
 * an instant must re-read immediately before it writes and touch only its own
 * fields.
 */
export function updateProject(id: string, mutate: (p: Project) => void) {
  const fresh = getProject(id);
  if (!fresh) return null;
  mutate(fresh);
  return saveProject(fresh);
}

export function saveProject(p: Project) {
  let prev: Project | null = null;
  try { prev = getProject(p.id); } catch { /* a new project, or unreadable: nothing to compare */ }

  // A DELETED project: its file is gone but this copy has been saved before
  // (a brand-new project has no rev). A render or build that finished after
  // the delete would otherwise write it back — a project with no files behind
  // it, in the list again.
  if (!prev && (p.rev ?? 0) > 0 && !existsSync(join(PROJECTS, `${p.id}.json`))) return p;

  // A STALE COPY: read before somebody else saved, and now being written back
  // over their work. Several routes read the project, wait minutes on a model,
  // then save what they read — and a project that had finished building came
  // back marked "building", unopenable from the index, with a final video
  // already made. Two guards, and a record of who did it so the route can be
  // fixed rather than guessed at.
  if (prev && (prev.rev ?? 0) > (p.rev ?? 0)) {
    // A finished build is never undone by an old copy.
    if (p.state === 'building' && prev.state && prev.state !== 'building') {
      p.state = prev.state;
      if (prev.state !== 'failed') delete p.buildError;
    }
    try {
      const who = (new Error().stack || '').split('\n').slice(2, 7).map((l) => l.trim()).join(' | ');
      const keys = Object.keys({ ...prev, ...p }).filter((k) =>
        k !== 'rev' && JSON.stringify((prev as any)[k]) !== JSON.stringify((p as any)[k]));
      appendFileSync(join(PROJECTS, 'stale.log'),
        `${new Date().toISOString()} ${p.id} (${p.name}) saved from rev ${p.rev ?? 0} over rev ${prev.rev}; ` +
        `differs in: ${keys.join(', ') || 'nothing'}\n  ${who}\n\n`);
    } catch { /* logging must never break a save */ }
  }

  // Always above what is on disk, so the NEXT stale copy is still recognised
  // as one even after this one was written.
  p.rev = Math.max(p.rev ?? 0, prev?.rev ?? 0) + 1;
  // Write whole or not at all: a reader arriving mid-write found half a file,
  // could not parse it, and answered "not found".
  const file = join(PROJECTS, `${p.id}.json`);
  const tmp = `${file}.${process.pid}.tmp`;
  writeFileSync(tmp, JSON.stringify(p, null, 2));
  renameSync(tmp, file);
  return p;
}

export const TRASH = join(DATA, 'trash');

/**
 * Take a project out of the studio: its file, its work (takes, reads,
 * previews, renders) and anything uploaded for it. MOVED to data/trash, not
 * erased — a deleted project can be put back by moving the three pieces
 * home, and a recording lost to one wrong click has happened here before.
 * A video the project only pointed at, outside the studio's own media, is
 * never touched.
 */
export function deleteProject(id: string) {
  const p = getProject(id);
  if (!p) return null;
  const stamp = new Date().toISOString().replace(/[-:]/g, '').slice(0, 15);
  const dest = join(TRASH, `${id}-${stamp}`);
  mkdirSync(dest, { recursive: true });
  // The file first: from that moment the project is gone from the list and
  // every re-read of it answers "not found".
  renameSync(join(PROJECTS, `${id}.json`), join(dest, 'project.json'));
  for (const [from, name] of [[join(WORK, id), 'work'], [join(MEDIA, id), 'media']] as const)
    if (existsSync(from)) renameSync(from, join(dest, name));
  return { name: p.name, trashed: dest };
}
