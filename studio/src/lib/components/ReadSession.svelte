<script lang="ts">
  import { untrack } from 'svelte';
  import { Button, Alert } from 'flowbite-svelte';

  /**
   * Record one long, unbroken read.
   *
   * Nothing about this is per-line, on purpose. The corpus assembled from the
   * existing projects is twenty minutes and a trained clone wants thirty, and
   * the cheap way to close that is ten minutes of reading anything — not more
   * of the per-block recording this whole effort exists to stop doing.
   *
   * Nobody ever hears these files. They are not narration; they are the model
   * listening to the voice.
   */
  let { sessions = [], models = [], voiceId, defaultModel = 'gemma4:12b', names = [], onsaved }: {
    sessions?: any[];
    models?: string[];
    /** The studio's writing model (Settings), preselected. */
    defaultModel?: string;
    /** The studio's product names (Settings), offered as words to learn. */
    names?: string[];
    /** Whose voice this reading is for. */
    voiceId: string;
    onsaved?: () => void;
  } = $props();

  const api = $derived(`/api/voices/${voiceId}/script`);
  /** Recordings go where the voice keeps them. */
  const sessionApi = $derived(`/api/voices/${voiceId}/session`);

  /** The script to read, and the reader's place in it.
   *
   * "Read anything for ten minutes" is a worse instruction than it sounds:
   * material chosen on the spot is read hesitantly, and a transcript has to be
   * GUESSED at afterwards by the same transcriber whose mistakes we spend the
   * rest of the pipeline correcting. A generated script is read fluently and
   * arrives with its own exact text.
   *
   * Thirty minutes is not one sitting, so where the reader got to is stored on
   * the server, not held in this component. */
  let script = $state<any>(null);
  let at = $state(0);
  let making = $state(false);
  let lang = $state('English');
  /** Which model writes the script: the studio's writing model unless picked
      here. It runs once per script, and a local model shares the card with
      the voice model while it does. */
  let model = $state(untrack(() => defaultModel));
  /** Names a TTS model gets wrong on sight. They belong in the script, not in
      a substitution table: a fine-tune learns a word by hearing the speaker
      say it, and cannot learn one it was never given. */
  let learn = $state(untrack(() => names.join(', ')));

  const WANT_MIN = 30;
  const readWords = $derived(
    (script?.passages ?? []).filter((p: any) => p.read)
      .reduce((n: number, p: any) => n + p.text.split(/\s+/).length, 0));
  /** Minutes of material already read, from the SCRIPT rather than the clock:
      a reader who paused for coffee should not be credited for it. */
  const readMin = $derived(Math.round(readWords / 16) / 10);
  const current = $derived((script?.passages ?? [])[at] ?? null);

  async function loadScript() {
    try {
      const d = await fetch(api).then((r) => r.json());
      script = d.script;
      const first = (script?.passages ?? []).findIndex((p: any) => !p.read);
      at = first < 0 ? 0 : first;
    } catch { /* no script yet is the normal first state */ }
  }
  $effect(() => { if (!script) loadScript(); });

  async function makeScript(more = false) {
    making = true;
    try {
      const d = await fetch(api, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ language: lang, model, wordsToLearn: learn,
                               passages: more ? 20 : 60 })
      }).then((r) => r.json());
      if (d.error) { error = d.error; return; }
      script = d.script;
      const first = (script?.passages ?? []).findIndex((p: any) => !p.read);
      at = first < 0 ? 0 : first;
    } catch (e: any) {
      error = e.message;
    } finally {
      making = false;
    }
  }

  /** Mark this passage read and move on. Marking is deliberate rather than
      automatic: only the reader knows whether that take was worth keeping. */
  async function markRead() {
    if (!current) return;
    const done = (script.passages ?? []).filter((p: any) => p.read)
      .map((p: any) => p.id);
    done.push(current.id);
    try {
      const d = await fetch(api, {
        method: 'PATCH',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ done })
      }).then((r) => r.json());
      if (d.script) script = d.script;
    } catch { /* the mark is a convenience; the audio is the thing */ }
    at = Math.min(at + 1, (script?.passages?.length ?? 1) - 1);
  }

  let phase = $state<'idle' | 'recording' | 'saving'>('idle');
  let error = $state('');
  let secs = $state(0);
  /** Loudness right now, so a dead microphone is obvious in the first second
      rather than ten minutes later. */
  let level = $state(0);

  let stream: MediaStream | null = null;
  let rec: MediaRecorder | null = null;
  let chunks: Blob[] = [];
  let tick: ReturnType<typeof setInterval> | null = null;
  let audioCtx: AudioContext | null = null;
  let raf = 0;

  const WANT = 10 * 60;
  const clock = (s: number) =>
    `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`;
  const have = $derived(
    sessions.reduce((n: number, s: any) => n + (s.stats?.seconds ?? 0), 0)
  );

  function meter(src: MediaStream) {
    audioCtx = new AudioContext();
    const node = audioCtx.createAnalyser();
    node.fftSize = 1024;
    audioCtx.createMediaStreamSource(src).connect(node);
    const buf = new Float32Array(node.fftSize);
    const read = () => {
      node.getFloatTimeDomainData(buf);
      let sum = 0;
      for (const v of buf) sum += v * v;
      level = Math.min(1, Math.sqrt(sum / buf.length) * 6);
      raf = requestAnimationFrame(read);
    };
    read();
  }

  /** Whether this page may ask for a microphone at all.
   *
   * `navigator.mediaDevices` is undefined outside a SECURE CONTEXT, so
   * reaching the studio at http://192.168.x.x:5199 throws "cannot read
   * properties of undefined" the moment record is pressed - which reads as a
   * broken recorder rather than the browser refusing an insecure origin. The
   * fix is to open the studio as localhost (over an SSH tunnel when it runs
   * on another machine), and saying so is worth more than the error ever was. */
  const canRecord = $derived(
    typeof navigator !== 'undefined' && !!navigator.mediaDevices?.getUserMedia);

  async function start() {
    error = '';
    if (!navigator.mediaDevices?.getUserMedia) {
      error = 'This page cannot reach a microphone because it is not a secure '
            + 'context. Open the studio as http://localhost:5199 — through an SSH '
            + 'tunnel if it runs on another machine — and the browser will allow it.';
      return;
    }
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        // Identical to the per-line recorder: the pipeline levels the voice
        // itself and WANTS the room, so nothing is allowed to pre-process it.
        audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false }
      });
    } catch (e: any) {
      error = e.name === 'NotAllowedError'
        ? 'The browser blocked the microphone. Allow it for this page and try again.'
        : e.message;
      return;
    }
    chunks = [];
    secs = 0;
    rec = new MediaRecorder(stream, { mimeType: 'audio/webm' });
    rec.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
    // A slice per second: a ten-minute blob held entirely in memory until the
    // stop event is the shape that loses long recordings.
    rec.start(1000);
    meter(stream);
    tick = setInterval(() => (secs += 1), 1000);
    phase = 'recording';
  }

  async function stop() {
    if (phase !== 'recording' || !rec) return;
    phase = 'saving';
    if (tick) { clearInterval(tick); tick = null; }
    cancelAnimationFrame(raf);
    level = 0;

    const mine = chunks;
    await new Promise<void>((done) => {
      rec!.addEventListener('stop', () => done(), { once: true });
      rec!.stop();
    });
    stream?.getTracks().forEach((t) => t.stop());
    stream = null;
    audioCtx?.close();
    audioCtx = null;

    try {
      const r = await fetch(sessionApi, {
        method: 'POST',
        body: new Blob(mine, { type: 'audio/webm' })
      });
      const d = await r.json();
      if (!r.ok || d.error) throw new Error(d.error ?? `${r.status}`);
      onsaved?.();
    } catch (e: any) {
      error = e.message;
    }
    phase = 'idle';
    secs = 0;
  }

  async function drop(path: string) {
    if (!confirm('Delete this read?')) return;
    const r = await fetch(`${sessionApi}?path=${encodeURIComponent(path)}`,
                          { method: 'DELETE' });
    // A refused delete has to say so. Silence here is how a recording the
    // operator had already given up on stayed on screen.
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      error = d.error ?? `could not delete that recording (${r.status})`;
      return;
    }
    onsaved?.();
  }
</script>

<div class="rounded-lg border border-gray-800 bg-gray-950 p-4">
  <h2 class="text-sm font-semibold">Read a script, to train the voice</h2>
  <p class="mt-1 text-[11px] leading-relaxed text-gray-500">
    Nobody ever hears these recordings; they are the model listening to you.
    Thirty minutes is what turns a model that imitates a clip of you into one
    that IS you — with 32 minutes of mined clips, taking the clip away made the
    voice drift, which is the same thing measured from the other side.
    Stop and come back whenever; your place is kept.
  </p>

  {#if !script}
    <div class="mt-3 flex flex-wrap items-center gap-2">
      <input class="w-36 rounded border border-gray-800 bg-gray-900 px-2 py-1 text-xs
                    text-gray-100 focus:border-indigo-600 focus:ring-0"
             bind:value={lang} placeholder="English" />
      <select class="rounded border border-gray-800 bg-gray-900 px-2 py-1 text-xs
                     text-gray-100 focus:border-indigo-600 focus:ring-0"
              bind:value={model}>
        {#each [...new Set([model, ...models])] as m}
          <option value={m}>{m}</option>
        {/each}
      </select>
      <Button size="xs" color="primary" disabled={making}
              onclick={() => makeScript(false)}>
        {making ? 'Writing…' : 'Write me something to read'}
      </Button>
      <span class="text-[11px] text-gray-500">
        about thirty minutes of varied passages
      </span>
      <div class="w-full">
        <input class="w-full rounded border border-gray-800 bg-gray-900 px-2 py-1
                      text-xs text-gray-100 focus:border-indigo-600 focus:ring-0"
               bind:value={learn}
               placeholder="product names, surnames, acronyms" />
        <p class="mt-1 text-[10px] leading-relaxed text-gray-500">
          Names the model will get wrong on sight. They are written into a
          quarter of the passages so the fine-tune hears you say them — which
          is the only way it learns a word. Capped at a quarter, because a
          corpus of brand names teaches the cadence of a brochure.
        </p>
      </div>
    </div>
  {:else}
    <div class="mt-3">
      <div class="mb-1.5 flex items-baseline justify-between text-[11px]">
        <span class="text-gray-400">
          {readMin} of {WANT_MIN} minutes read
          <span class="text-gray-600">
            · passage {at + 1} of {script.passages.length}
            {#if script.language && script.language !== 'English'}· {script.language}{/if}
          </span>
        </span>
        <button class="text-gray-500 underline hover:text-gray-300 disabled:opacity-40"
                disabled={making} onclick={() => makeScript(true)}>
          {making ? 'writing…' : 'add more passages'}
        </button>
      </div>
      <div class="h-1.5 w-full overflow-hidden rounded bg-gray-800">
        <div class="h-full bg-indigo-500 transition-[width]"
             style="width:{Math.min(100, Math.round(readMin / WANT_MIN * 100))}%"></div>
      </div>

      {#if current}
        <div class="mt-3 rounded border border-gray-800 bg-gray-900/60 p-3">
          <div class="mb-1 text-[10px] uppercase tracking-wide text-gray-600">
            {current.title}
          </div>
          <p class="text-[15px] leading-relaxed text-gray-100">{current.text}</p>
        </div>
        <div class="mt-2 flex flex-wrap items-center gap-2 text-[11px]">
          <button class="text-gray-500 underline hover:text-gray-300 disabled:opacity-30"
                  disabled={at === 0} onclick={() => (at = Math.max(0, at - 1))}>back</button>
          <button class="text-gray-500 underline hover:text-gray-300 disabled:opacity-30"
                  disabled={at >= script.passages.length - 1}
                  onclick={() => (at = at + 1)}>skip</button>
          <button class="rounded bg-emerald-900/50 px-2 py-0.5 font-medium text-emerald-300
                         transition hover:bg-emerald-800/60"
                  onclick={markRead}>read it · next</button>
          <span class="text-gray-600">
            mark it once you have read it into a recording you kept
          </span>
        </div>
      {:else}
        <p class="mt-3 text-xs text-emerald-400">
          Every passage is marked read. Add more, or build the voice.
        </p>
      {/if}
    </div>
  {/if}

  {#if error}<Alert color="red" class="mt-3 text-xs">{error}</Alert>{/if}

  <div class="mt-3 flex items-center gap-3">
    {#if phase === 'recording'}
      <button
        class="flex h-12 w-12 shrink-0 items-center justify-center rounded-full
               bg-red-600 text-white transition hover:bg-red-500"
        aria-label="Stop recording" onclick={stop}>
        <span class="block h-4 w-4 rounded-sm bg-white"></span>
      </button>
    {:else}
      <button
        class="flex h-12 w-12 shrink-0 items-center justify-center rounded-full border
               border-gray-600 transition hover:border-red-500 disabled:opacity-40"
        aria-label="Start recording" disabled={phase === 'saving' || !canRecord}
        title={canRecord ? 'Start recording'
                         : 'Not a secure context — open over the SSH tunnel'}
        onclick={start}>
        <span class="block h-5 w-5 rounded-full bg-red-500"></span>
      </button>
    {/if}

    <div class="min-w-0 flex-1">
      {#if phase === 'recording'}
        <div class="font-mono text-lg tabular-nums text-gray-100">{clock(secs)}</div>
        <div class="mt-1 h-1.5 w-full overflow-hidden rounded bg-gray-800">
          <div class="h-full bg-emerald-500 transition-[width] duration-100"
               style="width:{Math.round(level * 100)}%"></div>
        </div>
        <p class="mt-1 text-[11px] text-gray-500">
          {secs >= WANT ? 'That is plenty — stop whenever you like.'
                        : `${clock(WANT - secs)} to go for a useful ten minutes`}
        </p>
      {:else if phase === 'saving'}
        <p class="text-sm text-gray-300">Saving…</p>
      {:else}
        {#if !canRecord}
          <p class="text-sm text-amber-400">The browser will not give this page a microphone.</p>
          <p class="text-[11px] leading-relaxed text-gray-500">
            It is not a secure context. Open the studio as
            <code class="text-gray-300">http://localhost:5199</code> — through
            an SSH tunnel (<code class="text-gray-300">ssh -L 5199:localhost:5199 you@host</code>)
            if it runs on another machine.
          </p>
        {:else}
        <p class="text-sm text-gray-300">Ready when you are.</p>
        <p class="text-[11px] text-gray-500">
          Same microphone, same room, same distance as your takes.
        </p>
        {/if}
      {/if}
    </div>
  </div>

  {#if sessions.length}
    <div class="mt-4 border-t border-gray-800 pt-3">
      <div class="mb-2 flex items-baseline justify-between text-[11px]">
        <span class="text-gray-400">{sessions.length} read{sessions.length === 1 ? '' : 's'}</span>
        <span class="text-emerald-400">{(have / 60).toFixed(1)} min recorded</span>
      </div>
      <ul class="space-y-2">
        {#each sessions as s (s.path)}
          <li class="rounded border border-gray-800 bg-gray-900/40 p-2">
            <div class="flex items-baseline justify-between gap-2 text-[11px]">
              <span class="truncate text-gray-300">{s.name}</span>
              <span class="shrink-0 text-gray-500">
                {((s.stats?.seconds ?? 0) / 60).toFixed(1)} min
                {#if s.stats?.snr_db}· SNR {s.stats.snr_db.toFixed(0)} dB{/if}
              </span>
            </div>
            <audio class="mt-1 h-8 w-full" controls preload="none"
                   src={`/api/media?path=${encodeURIComponent(s.path)}`}></audio>
            <button class="mt-1 text-[10px] text-gray-600 underline hover:text-red-400"
                    onclick={() => drop(s.path)}>delete</button>
          </li>
        {/each}
      </ul>
      <p class="mt-2 text-[11px] text-gray-600">
        Training uses every read recorded here.
      </p>
    </div>
  {/if}
</div>
