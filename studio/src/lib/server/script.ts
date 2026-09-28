import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { workDir } from './projects';
import type { Project } from '$lib/types';

const squash = (t?: string) => (t ?? '').replace(/\s+/g, ' ').trim();

/**
 * The text a machine pass last wrote into each line, where it can be known.
 *
 * Recorded on the segment as `sayMachine` from now on. For a script edited
 * before that existed, the flow pass's own output is still on disk — the
 * rewrite's, for any line the flow pass refused — and that is exactly what was
 * handed to the operator. Failing both, the mechanical rewrite the line
 * started from.
 */
export function machineText(p: Project): string[] {
  let flow: string[] | null = null;
  try {
    const work = workDir(p.id);
    const f = JSON.parse(readFileSync(join(work, 'script.flow.json'), 'utf8'));
    const inp = JSON.parse(readFileSync(join(work, 'script.in.json'), 'utf8'));
    if (f.lines?.length === p.segments.length && inp.lines?.length === p.segments.length) {
      const refused = new Set<number>((f.rejected ?? [])
        .map((r: string) => Number(/line (\d+)/.exec(r)?.[1])).filter(Boolean));
      flow = f.lines.map((l: string, i: number) => (refused.has(i + 1) ? inp.lines[i] : l));
    }
  } catch { /* no flow pass on disk */ }
  return p.segments.map((s, i) => s.sayMachine ?? flow?.[i] ?? s.sayOriginal);
}

/** Which lines the operator has rewritten by hand. Machine passes leave these
    alone: a polish offers only a light touch, and the flow pass skips them. */
export function editedByHand(p: Project): boolean[] {
  const m = machineText(p);
  return p.segments.map((s, i) => squash(s.say) !== squash(m[i]));
}
