import { spawn, execFileSync } from 'node:child_process';
import { join } from 'node:path';
import { DATA, OLLAMA } from './config';

export type RunResult = { code: number; out: string; err: string };

/** Only the tail of a stream is ever used (for a log line or an error), so
    keep a bounded window instead of concatenating a whole render's chatter. */
const CAP = 256 * 1024;
const clip = (s: string) => (s.length > CAP ? s.slice(-CAP) : s);

/** Everything we have spawned and not yet reaped.
 *
 *  A child is NOT killed when its parent dev server reloads or exits, and a
 *  final render is an ffmpeg re-encode of 4K/60fps video: leave a few of those
 *  running across reloads and the machine is gone. Tracked so shutdown can
 *  take them with it. */
const alive = new Set<ReturnType<typeof spawn>>();

export function run(cmd: string, args: string[], opts: { cwd?: string; timeout?: number } = {}) {
  return new Promise<RunResult>((resolve) => {
    const p = spawn(cmd, args, { cwd: opts.cwd, env: { ...process.env, PYTHONPATH: opts.cwd ?? '' } });
    alive.add(p);
    let out = '', err = '';
    p.stdout.on('data', (d) => (out = clip(out + d)));
    p.stderr.on('data', (d) => (err = clip(err + d)));
    const t = opts.timeout
      ? setTimeout(() => p.kill('SIGKILL'), opts.timeout)
      : null;
    p.on('close', (code) => {
      if (t) clearTimeout(t);
      alive.delete(p);
      resolve({ code: code ?? -1, out, err });
    });
  });
}

/**
 * A long-lived synthesis worker.
 *
 * Loading Chatterbox takes about thirty seconds, which is fine once and
 * intolerable per click. The worker keeps the model resident and answers one
 * JSON request per line, so tweaking a knob and hearing the result is a few
 * seconds rather than a cold start every time.
 */
/**
 * A python process that keeps a model loaded and answers one line of JSON at
 * a time.
 *
 * Two of these run now. Synthesis was always one; verification was not, and
 * paid twenty-five seconds to load Whisper for EVERY take — eighty-three
 * minutes of a hundred-and-forty-three line render spent loading the same
 * model over and over. That is most of the reason a render took two hours
 * rather than the ten minutes of generation it contains, and it made turning
 * the checker off look like the only way to get the time back.
 */
class SynthWorker {
  /** `match` is what a stray of this kind looks like to pkill; `recycle` is
      how many requests a process may serve before it is retired, which exists
      for the Metal allocator wedging after about twenty generations and is not
      needed by every model. */
  constructor(
    private match: string,
    private recycle = 20
  ) {}

  private proc: ReturnType<typeof spawn> | null = null;
  private waiting = new Map<number, {
    resolve: (v: any) => void;
    reject: (e: any) => void;
    timer: ReturnType<typeof setTimeout>;
  }>();
  private seq = 0;
  private buf = '';
  private idle: ReturnType<typeof setTimeout> | null = null;
  ready = false;

  /** Shut the model down after this long with nothing to do. */
  private static IDLE = 15 * 60_000;

  /** A normal line takes 12-35s. Six minutes is generous for a long one and
      still bounds the damage when the worker wedges. Twenty was far too long:
      the batch would have burned twenty minutes PER remaining line. */
  private static TIMEOUT = 6 * 60_000;

  /** Retire the worker after this many lines, BETWEEN requests.
      Freeing the Metal cache per line is the actual fix; this is the net under
      it. A voluntary restart costs one model load (~40s) at a moment when
      nothing is in flight. An involuntary one costs six minutes of timeout,
      the line being worked on, and a retry — and lands in the middle of a
      forty-line batch, which is exactly where it is least welcome. */
  private static RECYCLE_AFTER = 20;
  private served = 0;

  /** Free the model. The next request pays a cold start; a resident model
      that nobody is using costs gigabytes of unified memory. */
  stop(reason = 'stopped') {
    const p = this.proc;
    this.proc = null;
    this.ready = false;
    if (this.idle) { clearTimeout(this.idle); this.idle = null; }
    this.failAll(reason);
    try { p?.kill('SIGKILL'); } catch { /* already gone */ }
  }

  private touch() {
    if (this.idle) clearTimeout(this.idle);
    this.idle = setTimeout(() => this.stop('idle'), SynthWorker.IDLE);
  }

  async start(py: string, script: string) {
    // The voice model and a LOCAL language model do not both fit on this
    // card. A script pass on a project set to a local model (gemma4:12b) left
    // 7.5 GiB loaded in Ollama — it keeps a model five minutes after its last
    // use — and the next line's synthesis ran out of memory with 6.5 GiB of
    // its own. Give the card back to the voice before every line; a cloud
    // model holds no GPU memory and is left alone.
    if (this.match.startsWith('synth_one')) await freeOllama();
    // Something outside the studio's workers has the whole card — a sample
    // search loads its own copy of the model, and one more beside it runs out
    // of memory. Refuse plainly rather than take the search down with us.
    const held = cardHold();
    if (held && !this.proc) throw new Error(`the GPU is in use (${held}) — try again when it finishes`);
    if (this.proc && this.recycle > 0 && this.served >= this.recycle
        && this.waiting.size === 0) {
      this.stop(`retired after ${this.served} lines`);
    }
    if (this.proc) { this.touch(); return; }
    this.served = 0;
    // Sweep strays before spawning. Vite replaces this module on every edit,
    // and the replacement starts with proc = null while the PREVIOUS child is
    // still alive holding Chatterbox and its MPS allocations. Those orphans
    // accumulated one per reload and are close to invisible in RSS, because
    // Metal buffers are unified memory.
    try {
      execFileSync('pkill', ['-f', this.match], { stdio: 'ignore' });
      await new Promise((r) => setTimeout(r, 300));
    } catch { /* nothing to sweep */ }
    this.proc = spawn(py, [script, '--serve'], { stdio: ['pipe', 'pipe', 'pipe'] });
    this.touch();
    this.proc.stdout!.on('data', (d) => {
      this.buf += d.toString();
      let i;
      while ((i = this.buf.indexOf('\n')) >= 0) {
        const line = this.buf.slice(0, i).trim();
        this.buf = this.buf.slice(i + 1);
        if (!line) continue;
        let obj: any;
        try { obj = JSON.parse(line); } catch { continue; }
        if (obj.ready) { this.ready = true; continue; }
        // Match by id. The queue used to be positional, so one lost or extra
        // line offset every later reply by one and a caller waited forever for
        // a take that had already been handed to the wrong promise.
        const w = this.waiting.get(obj.id);
        if (!w) continue;
        this.waiting.delete(obj.id);
        clearTimeout(w.timer);
        obj.error ? w.reject(new Error(obj.error)) : w.resolve(obj);
      }
    });
    // Both handlers close over THIS child and must check they still own the
    // field before clearing it.
    //
    // This is the whole of "attempt 2 failed: synth worker not running". A
    // wedged worker is SIGKILLed and replaced immediately, but the kill is not
    // synchronous: the dead child's `close` arrives a moment AFTER the retry
    // has already spawned its replacement and stored it. The stale handler
    // then set proc = null on a perfectly healthy new process, and the very
    // next send threw. The retry could never succeed — it was being sabotaged
    // by the corpse of the worker it was retrying.
    const mine = this.proc;
    this.proc.on('close', () => {
      if (this.proc !== mine) return;      // already replaced; not ours to clear
      this.proc = null;
      this.ready = false;
      // Whoever was mid-flight is never getting an answer. Say so, rather than
      // leaving the page on a spinner that can no longer end.
      this.failAll('the synthesis worker stopped before this line finished');
    });
    this.proc.on('error', (e) => {
      if (this.proc !== mine) return;
      this.failAll(`synthesis worker: ${e.message}`);
    });
    const t0 = Date.now();
    while (!this.ready && Date.now() - t0 < 300_000) await new Promise((r) => setTimeout(r, 250));
    if (!this.ready) throw new Error('synthesis worker did not come up');
  }

  private failAll(msg: string) {
    for (const [, w] of this.waiting) {
      clearTimeout(w.timer);
      w.reject(new Error(msg));
    }
    this.waiting.clear();
  }

  send(req: Record<string, unknown>) {
    if (!this.proc) throw new Error('synth worker not running');
    if (!this.ready) throw new Error('the synthesis worker is still starting up');
    const id = ++this.seq;
    return new Promise<any>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.waiting.delete(id);
        // The worker is wedged, not slow. Observed on a 14-word line after 25
        // good ones: 0% CPU, flat RSS, no output — an MPS stall. Feeding the
        // next line to the same process just wedges again, so replace it.
        // `stop` rejects anything else outstanding for the same reason.
        this.stop('the synthesis worker stopped responding and was restarted');
        reject(new Error('the synthesis worker stopped responding — restarted, try again'));
      }, SynthWorker.TIMEOUT);
      this.served++;
      this.waiting.set(id, { resolve, reject, timer });
      this.touch();
      this.proc!.stdin!.write(JSON.stringify({ ...req, id }) + '\n');
    });
  }
}

/**
 * Unload whatever Ollama holds in GPU memory, and wait until it has let go.
 * Cheap when there is nothing to free (one local request); never fatal — if
 * Ollama is not running there is nothing to free.
 */
export async function freeOllama() {
  try {
    const loaded = async () => {
      const r = await fetch(`${OLLAMA}/api/ps`, { signal: AbortSignal.timeout(1500) });
      if (!r.ok) return [];
      return ((await r.json()).models ?? []).filter((m: any) => (m.size_vram ?? 0) > 0);
    };
    const held = await loaded();
    if (!held.length) return;
    for (const m of held) {
      await fetch(`${OLLAMA}/api/generate`, {
        method: 'POST', body: JSON.stringify({ model: m.name, keep_alive: 0 }),
        signal: AbortSignal.timeout(15000)
      });
      console.log(`[gpu] unloaded ${m.name} from Ollama (${((m.size_vram ?? 0) / 2 ** 30).toFixed(1)} GiB) for the voice model`);
    }
    for (let i = 0; i < 20 && (await loaded()).length; i++) await new Promise((r) => setTimeout(r, 250));
  } catch { /* no Ollama, or it did not answer: nothing we can free */ }
}

/** Who holds the whole card, if anyone. On globalThis so a module reload
    mid-search cannot forget it. */
const g = globalThis as any;
export const holdCard = (why: string | null) => { g.__studioCardHold = why; };
export const cardHold = (): string | null => g.__studioCardHold ?? null;

export const synthWorker = new SynthWorker('synth_one.py --serve');
/** Whisper does not wedge the way the Metal allocator did, so this one is
    never retired — retiring it would reintroduce the load it exists to
    avoid. */
export const verifyWorker = new SynthWorker('verify_take.py --serve', 0);

// A child of a dev server is NOT reaped when that server reloads or exits, so
// say so explicitly. Without this the worker outlives every edit and every
// restart, and the machine slowly fills up with loaded copies of the model.
const shutdown = () => {
  synthWorker.stop('server shutting down');
  // The transcriber too. Left out, every reload orphaned one holding ~2 GB of
  // the card — enough to run a scoring job out of memory beside the model.
  verifyWorker.stop('server shutting down');
  for (const p of alive) { try { p.kill('SIGKILL'); } catch { /* already gone */ } }
  alive.clear();
};
for (const sig of ['exit', 'SIGINT', 'SIGTERM'] as const) process.once(sig, shutdown);
if (import.meta.hot) import.meta.hot.dispose(shutdown);

/**
 * Long-running children, so a module reload cannot orphan one on the GPU.
 *
 * Vite replaces a route module on every edit, and anything that module was
 * holding in a local Map goes with it - while the process it spawned carries
 * on, still holding twelve gigabytes of card. The next render then fails with
 * "0 bytes free" against a machine that looks idle, which is exactly what
 * happened: a scoring run was orphaned by a sync and a render died on it
 * twenty minutes later.
 *
 * `globalThis` survives the reload. Registering here means the sweep below
 * can find a previous incarnation's children as well as this one's.
 */
const KEEP = '__narrate_children__';
type Kid = { pid: number; kind: string; at: number };
const kids: Map<string, Kid> = (globalThis as any)[KEEP] ??= new Map();

export function holdChild(key: string, pid: number, kind: string) {
  kids.set(key, { pid, kind, at: Date.now() });
}

export function releaseChild(key: string) {
  kids.delete(key);
}

/** Kill whatever is registered under this key, whoever registered it. */
export function killChild(key: string) {
  const k = kids.get(key);
  if (!k) return false;
  try { process.kill(k.pid, 'SIGTERM'); } catch { /* already gone */ }
  kids.delete(key);
  return true;
}

/** Everything still registered, for a UI that wants to say "the card is busy". */
export const heldChildren = () => [...kids.entries()].map(([key, k]) => ({ key, ...k }));
