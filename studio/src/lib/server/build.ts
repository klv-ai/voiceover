import { existsSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { updateProject, workDir } from './projects';
import { analyze, readAnalysis, buildScript } from './pipeline';
import { startJob, note } from './jobs';
import { run } from './run';
import { NARRATE, PY } from './config';
import type { Project, Segment } from '$lib/types';

/**
 * Turn a new project's video into something editable: transcribe, find the
 * fillers and stumbles, cut the script into lines. None of that is a decision,
 * so it runs on its own and the page watches it.
 *
 * `parts`: recordings to join into `p.source` first, in order — a video
 * recorded in sittings. Joining re-encodes (see lab/join.py for why a copy
 * join is wrong), so it takes about as long as the video plays.
 */
export function startBuild(p: Project, parts?: string[]) {
  const id = p.id;
  return startJob(id, 'build', async (j) => {
    const steps = parts?.length ? 3 : 2;
    j.total = steps; j.done = 0;
    if (parts?.length) {
      // join.py writes the video and this record of it only once the join is
      // verified, so both being there means a retry can skip the join.
      const record = p.source + '.join.json';
      let d: any = null;
      if (existsSync(p.source) && existsSync(record)) {
        try { d = JSON.parse(readFileSync(record, 'utf8')); } catch { d = null; }
        if (d) note(j, `already joined ${parts.length} recordings`);
      }
      if (!d) {
        note(j, `joining ${parts.length} recordings`, 'about as long as the video plays');
        const r = await run(PY, [join(NARRATE, 'lab/join.py'), p.source, ...parts], { cwd: NARRATE });
        if (r.code !== 0) {
          const why = r.err.trim().split('\n').filter(Boolean).pop() ?? '';
          throw new Error(why || 'could not join the recordings');
        }
        d = JSON.parse(r.out.trim().split('\n').pop() ?? '{}');
      }
      // Part names without the order prefix the upload gave them.
      const joined = (d.parts ?? []).map((x: any) => ({ ...x, name: String(x.name).replace(/^\d{2}-/, '') }));
      updateProject(id, (fresh) => { fresh.parts = joined; });
      j.done++;
      note(j, `joined into ${Math.round(d.duration ?? 0)}s of video`);
    }

    note(j, 'listening to the video');
    const work = workDir(id);
    const analysis = await analyze(p.source, work);
    const d = (await readAnalysis(analysis)) ?? {};
    updateProject(id, (fresh) => {
      fresh.analysis = analysis;
      fresh.metrics = d.metrics;
      fresh.duration = d.source?.duration;
    });
    j.done++;
    note(j, `${Math.round(d.source?.duration ?? 0)}s of video, `
          + `${d.metrics?.hard_fillers ?? 0} filler(s), ${d.metrics?.stutters ?? 0} repeat(s)`);

    note(j, 'writing the script');
    const doc = await buildScript(analysis, work, p.settings.voiceRef);
    const segments: Segment[] = (doc.requests ?? []).map((r: any) => ({
      id: r.id, start: r.span[0], end: r.span[1],
      heard: r.heard ?? '', say: r.say, sayOriginal: r.say,
      cleaned: false, locked: false
    }));
    updateProject(id, (fresh) => { fresh.segments = segments; fresh.state = 'ready'; });
    j.done = steps;
    note(j, `${segments.length} lines — ready`);
    return id;
  }, (j) => {
    updateProject(id, (fresh) => { fresh.state = 'failed'; fresh.buildError = j.error; });
  });
}
