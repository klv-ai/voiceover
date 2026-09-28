/**
 * A one-slot job register, per project.
 *
 * The final render re-encodes the picture and runs a two-pass loudness
 * measurement, so it takes minutes, not seconds. Holding an HTTP request open
 * that long gives the operator a spinner and no idea whether anything is
 * happening — so the work runs detached and the page asks how it is going.
 */
export type Job = {
  id: string;
  project: string;
  /** Which long operation this is, so the page can label it correctly. */
  kind: 'render' | 'synth-all' | 'build' | 'train';
  step: string;
  status: 'running' | 'done' | 'error' | 'stopped';
  log: string[];
  error?: string;
  output?: string;
  /** Batch work: how far through, for a progress bar rather than a spinner. */
  done?: number;
  total?: number;
  /** Set by stopJob. Long loops check it between items and bail cleanly —
      a batch of forty renders is twenty minutes, so it has to be abandonable
      without killing the server or losing the takes already finished. */
  cancel?: boolean;
  startedAt: number;
  endedAt?: number;
};

const byProject = new Map<string, Job>();

export const getJob = (project: string) => byProject.get(project) ?? null;

/** Every job still running — so work that needs the whole GPU can see what
    already holds it rather than finding out by running out of memory. */
export const runningJobs = () => [...byProject.values()].filter((j) => j.status === 'running');

export function stopJob(project: string) {
  const j = byProject.get(project);
  if (j?.status === 'running') { j.cancel = true; j.step = 'stopping…'; }
  return j ?? null;
}

export function startJob(
  project: string,
  kind: Job['kind'],
  work: (j: Job) => Promise<string>,
  /** Called if the work throws, so a caller can record the failure somewhere
      that outlives this register — jobs are in memory and a reload loses them,
      which is fine for a render and not fine for a project that never built. */
  onFail?: (j: Job) => void
): Job {
  const running = byProject.get(project);
  if (running?.status === 'running') return running;
  const job: Job = {
    id: Math.random().toString(36).slice(2, 10),
    project, kind, step: 'starting', status: 'running', log: [], startedAt: Date.now()
  };
  byProject.set(project, job);
  work(job)
    .then((output) => { job.output = output; job.status = job.cancel ? 'stopped' : 'done'; })
    .catch((e) => {
      job.error = String(e?.message ?? e);
      job.status = 'error';
      onFail?.(job);
    })
    .finally(() => { job.endedAt = Date.now(); });
  return job;
}

export function note(job: Job, step: string, detail = '') {
  job.step = step;
  job.log.push(detail ? `${step} — ${detail}` : step);
  if (job.log.length > 200) job.log.splice(0, job.log.length - 200);
}
