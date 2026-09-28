<script lang="ts">
  import { Button, Alert } from 'flowbite-svelte';
  import { invalidateAll } from '$app/navigation';
  import ReadSession from '$lib/components/ReadSession.svelte';

  /**
   * One voice, and everything it is made of.
   *
   * The lab was a single workbench: one shared folder of recordings, one read
   * script, one corpus. That holds while there is one speaker and breaks the
   * moment there are two — and two is the point. A voice owns its readings,
   * its training runs, and the judgement about which checkpoint it currently
   * IS, because that judgement cannot be derived: across the first run here
   * the newest checkpoint sounded most like the speaker and had forgotten how
   * to stop.
   */
  let { data } = $props();
  let error = $state('');
  let busy = $state('');

  /** The training run, polled while it is going. Thirty minutes of reading
      deserves to be watched rather than guessed at. */
  let job = $state<any>({ status: 'idle' });
  let poll: ReturnType<typeof setInterval> | null = null;

  async function readJob() {
    try {
      job = await fetch(`/api/voices/${data.voice.id}/train`).then((r) => r.json());
    } catch { /* a missed poll is not worth a message */ }
    // Adopt a run that was already going, for the same reason as the scorer:
    // polling that only starts on a button press leaves a reloaded page
    // frozen mid-run, which looks exactly like a job that has died.
    if (job.status === 'running' && !poll) poll = setInterval(readJob, 4000);
    if (job.status !== 'running' && poll) { clearInterval(poll); poll = null; invalidateAll(); }
  }
  $effect(() => {
    readJob();
    return () => { if (poll) clearInterval(poll); };
  });

  async function train() {
    error = '';
    busy = 'train';
    try {
      job = await fetch(`/api/voices/${data.voice.id}/train`, {
        method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ steps: 600 })
      }).then((r) => r.json());
      if (job.error) error = job.error;
      else if (!poll) poll = setInterval(readJob, 4000);
    } catch (e: any) { error = e.message; } finally { busy = ''; }
  }

  async function stopTraining() {
    await fetch(`/api/voices/${data.voice.id}/train`, { method: 'DELETE' });
    readJob();
  }

  /* ---- the bench: hear it say something ---- */
  let benchText = $state('');
  let benchAbout = $state('');
  let benchDirection = $state('');
  let benchCkpt = $state<string | null>(null);
  /** Match what a project will actually do. The bench used to clone from the
      reference with no pace target while a project narrated from the model
      alone at 190 wpm, so the same line sounded right here and wrong in the
      video - and the difference read as the render corrupting something. */
  /** ASSIST is the sample of the real recording handed to the model.
   *
   * The bench used to add it silently, which meant it was never playing the
   * voice - it was playing the voice plus a crutch, and the crutch does most
   * of the work. That is why every checkpoint sounded alike here: the sample
   * was carrying the accent, the mic distance and the room, and whatever the
   * training had actually learned was underneath it.
   *
   * So OFF by default. This panel plays what the model IS; ticking the box
   * plays what a render with assist will sound like. */
  let benchAssist = $state(false);
  let benchWpm = $state(0);
  let benchOut = $state<any>(null);
  let saying = $state(false);

  async function say() {
    saying = true;
    error = '';
    benchOut = null;
    try {
      const d = await fetch(`/api/voices/${data.voice.id}/say`, {
        method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({
          text: benchText, about: benchAbout,
          direction: benchDirection,
          noAssist: !benchAssist, targetWpm: benchWpm,
          checkpoint: benchCkpt === null ? undefined : benchCkpt
        })
      }).then((r) => r.json());
      if (d.error) error = d.error;
      else benchOut = d;
    } catch (e: any) { error = e.message; } finally { saying = false; }
  }

  const media = (p: string) => `/api/media?path=${encodeURIComponent(p)}`;

  /** Scoring several checkpoints at once, because one line each cannot tell
      them apart - every one of them sounds like him on one line, including
      the one that filled a video with noise. */
  let cmp = $state<any>({ status: 'idle', rows: [] });
  /** Which training run to score. Runs saw different CORPORA — the first 32
      minutes of mined clips, the latest 54 of purpose-recorded reading — so
      mixing them compares training data rather than checkpoints. */
  let cmpRun = $state('');
  /** Stop after this many checkpoints in a row that beat nothing. 0 = score
      the whole run. Three, because a sweep that went 44, 27, 31, 24, 52, 44
      would have quit at the 31 and missed the 24. */
  let cmpAll = $state(false);
  let cmpPoll: ReturnType<typeof setInterval> | null = null;

  async function readCmp() {
    try {
      cmp = await fetch(`/api/voices/${data.voice.id}/compare`).then((r) => r.json());
    } catch { /* a missed poll is not worth a message */ }
    // Pick up a run that was already going. Polling only started after the
    // button was pressed, so reloading the page during a forty-minute scoring
    // run left it frozen on whatever rows had landed - which is
    // indistinguishable from it having died.
    if (cmp.status === 'running' && !cmpPoll) cmpPoll = setInterval(readCmp, 4000);
    if (cmp.status !== 'running' && cmpPoll) { clearInterval(cmpPoll); cmpPoll = null; }
  }

  $effect(() => {
    readCmp();
    return () => { if (cmpPoll) clearInterval(cmpPoll); };
  });

  async function compare() {
    error = '';
    busy = 'compare';
    try {
      cmp = await fetch(`/api/voices/${data.voice.id}/compare`, {
        method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ run: cmpRun || undefined,
                               patience: cmpAll ? 0 : 3 })
      }).then((r) => r.json());
      if (cmp.error) error = cmp.error;
      else if (!cmpPoll) cmpPoll = setInterval(readCmp, 4000);
    } catch (e: any) { error = e.message; } finally { busy = ''; }
  }

  const mins = (s: number | undefined) => (s == null ? '—' : `${(s / 60).toFixed(1)} min`);
  const readMinutes = $derived(
    (data.sessions ?? []).reduce((n: number, s: any) => n + (s.stats?.seconds ?? 0), 0) / 60);
  const corpusMinutes = $derived(data.voice?.corpus?.minutes ?? 0);
  /** Enough READ material to be worth a run. Mined clips do not count toward
      this: thirty-two minutes of them was already trained in, and taking the
      reference away still made the voice drift. */
  const enough = $derived(readMinutes >= 25);

  async function promote(checkpoint: string) {
    busy = checkpoint;
    error = '';
    try {
      const r = await fetch(`/api/voices/${data.voice.id}/promote`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ checkpoint })
      });
      const d = await r.json();
      if (d.error) error = d.error;
      else { await invalidateAll(); await readSmp(); }   // promoting starts a sample search
    } catch (e: any) {
      error = e.message;
    } finally {
      busy = '';
    }
  }

  /** The sample search: candidates built from every recording of this voice,
      scored by what the clone MAKES of them, adopted only after listening.
      It starts by itself when a checkpoint is promoted. */
  let smp = $state<any>({ status: 'idle', rows: [] });
  let smpPoll: ReturnType<typeof setInterval> | null = null;
  async function readSmp() {
    try {
      smp = await fetch(`/api/voices/${data.voice.id}/samples`).then((r) => r.json());
    } catch { /* a missed poll is not worth a message */ }
    if (smp.status === 'running' && !smpPoll) smpPoll = setInterval(readSmp, 5000);
    if (smp.status !== 'running' && smpPoll) { clearInterval(smpPoll); smpPoll = null; }
  }
  $effect(() => {
    readSmp();
    return () => { if (smpPoll) clearInterval(smpPoll); };
  });
  async function findSample() {
    error = '';
    const d = await fetch(`/api/voices/${data.voice.id}/samples`, { method: 'POST' }).then((r) => r.json());
    if (d.error) error = d.error;
    await readSmp();
  }
  async function adoptSample(name: string) {
    busy = name;
    error = '';
    try {
      const d = await fetch(`/api/voices/${data.voice.id}/samples/adopt`, {
        method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ name })
      }).then((r) => r.json());
      if (d.error) error = d.error;
      await readSmp();
    } finally {
      busy = '';
    }
  }
</script>

<svelte:head><title>{data.voice.name} — Voice lab</title></svelte:head>

<div class="mx-auto max-w-4xl space-y-4 p-4">
  <div class="flex items-baseline gap-3">
    <a href="/voices" class="text-xs text-gray-500 underline hover:text-gray-300">Voice lab</a>
    <h1 class="text-xl font-semibold">{data.voice.name}</h1>
    <span class="font-mono text-[10px] text-gray-600">{data.voice.id}</span>
  </div>

  {#if error}<Alert color="red" class="text-xs">{error}</Alert>{/if}

  <!-- What this voice has to learn from, in the one unit that decides whether
       a fine-tune can stand on its own: minutes. -->
  <div class="rounded-lg border border-gray-800 bg-gray-950 p-4">
    <h2 class="text-sm font-semibold">Material</h2>
    <div class="mt-2 grid grid-cols-3 gap-3 text-xs">
      <div>
        <div class="text-gray-500">mined from projects</div>
        <div class="text-lg tabular-nums text-gray-100">{corpusMinutes.toFixed(1)} min</div>
        <div class="text-[10px] text-gray-600">{data.voice.corpus?.clips ?? 0} clips</div>
      </div>
      <div>
        <div class="text-gray-500">read for this voice</div>
        <div class="text-lg tabular-nums text-gray-100">{readMinutes.toFixed(1)} min</div>
        <div class="text-[10px] text-gray-600">{data.sessions.length} sittings</div>
      </div>
      <div>
        <div class="text-gray-500">reference</div>
        <div class="text-lg tabular-nums text-gray-100">
          {data.voice.stats?.seconds ? `${Math.round(data.voice.stats.seconds)}s` : '—'}
        </div>
        <div class="text-[10px] text-gray-600">
          {data.voice.stats?.snr_db ? `${data.voice.stats.snr_db.toFixed(0)} dB SNR` : ''}
        </div>
      </div>
    </div>
    <p class="mt-2 text-[10px] leading-relaxed text-gray-500">
      Mined clips bias a model toward a speaker; they do not make it one.
      Measured here: with 32 minutes of mined material trained in, taking the
      reference clip away made pitch consistency WORSE, 7.4 to 11.5. Read
      material is the part that closes that, because it is one sitting, one
      distance, and a script we already know the words to.
    </p>
  </div>

  <ReadSession sessions={data.sessions} models={data.models}
               defaultModel={data.defaultModel} names={data.names}
               voiceId={data.voice.id} onsaved={() => invalidateAll()} />

  <!-- Every run, every checkpoint, and which one this voice currently is. -->
  <div class="rounded-lg border border-gray-800 bg-gray-950 p-4">
    <div class="flex items-center justify-between">
      <h2 class="text-sm font-semibold">Training</h2>
      {#if job.status === 'running'}
        <!-- A bare underlined word next to a running job reads as a caption,
             not a control. It was clicked by accident once, which is the
             review. -->
        <Button size="xs" color="red" onclick={stopTraining}>Stop training</Button>
      {:else}
        <Button size="xs" color={enough ? 'primary' : 'alternative'}
                disabled={!!busy || job.status === 'running'}
                onclick={train}>
          {busy === 'train' ? 'Starting…' : 'Train this voice'}
        </Button>
      {/if}
    </div>

    {#if !enough && job.status !== 'running'}
      <p class="mt-1 text-[11px] leading-relaxed text-amber-600/80">
        {readMinutes.toFixed(1)} of about 30 read minutes. You can train now,
        but mined clips do not substitute: thirty-two minutes of them were
        already trained in and the voice still drifted the moment its
        reference clip was removed.
      </p>
    {/if}

    {#if job.status === 'running' || job.log?.length}
      <div class="mt-3 rounded border border-gray-800 bg-black/40 p-2">
        <div class="mb-1 flex items-baseline justify-between text-[10px]">
          <span class="uppercase tracking-wide text-gray-500">
            {job.status}{job.seconds ? ` · ${Math.floor(job.seconds / 60)}m ${job.seconds % 60}s` : ''}
          </span>
          {#if job.error}<span class="text-red-400">{job.error}</span>{/if}
        </div>
        <pre class="max-h-40 overflow-y-auto whitespace-pre-wrap font-mono
                    text-[10px] leading-relaxed text-gray-400">{(job.log ?? []).join('\n')}</pre>
      </div>
    {/if}

    {#if !data.runs.length}
      <p class="mt-1 text-[11px] text-gray-500">
        No training runs yet. Record about thirty minutes first — that is the
        bar where a fine-tune stops imitating a clip and starts being the voice.
      </p>
    {:else}
      <p class="mt-1 text-[11px] leading-relaxed text-gray-500">
        Pick by listening, not by recency. Across the first run here the model
        kept getting better at sounding like the speaker while getting worse at
        knowing when to stop — so the newest checkpoint is usually the wrong one.
      </p>
      {#each data.runs as r}
        <div class="mt-3">
          <div class="mb-1 font-mono text-[10px] uppercase tracking-wide text-gray-500">
            {r.run} · {r.steps.length} checkpoints
          </div>
          <div class="flex flex-wrap gap-1.5">
            {#each r.steps as step}
              {@const path = `${r.dir}/${step}`}
              {@const isLatest = data.latest === path}
              <button
                class="rounded border px-2 py-1 font-mono text-[11px] transition
                       {isLatest
                         ? 'border-emerald-600 bg-emerald-950/50 text-emerald-300'
                         : 'border-gray-800 text-gray-400 hover:border-indigo-600 hover:text-gray-200'}"
                disabled={busy === path}
                onclick={() => promote(isLatest ? '' : path)}>
                {step.replace('step_', '')}{isLatest ? ' · in use' : ''}
              </button>
            {/each}
          </div>
        </div>
      {/each}
      <p class="mt-3 text-[10px] text-gray-600">
        {data.latest ? 'Click the one in use to go back to stock weights.'
                     : 'No checkpoint promoted — projects using this voice run on stock weights.'}
      </p>
    {/if}
  </div>

  <!-- Several at once, scored. One at a time they all sound right. -->
  <div class="rounded-lg border border-gray-800 bg-gray-950 p-4">
    <div class="flex items-center justify-between">
      <h2 class="text-sm font-semibold">Compare checkpoints</h2>
      {#if cmp.status === 'running'}
        <span class="flex items-center gap-2 text-[11px] text-gray-400">
          <span class="h-2 w-2 animate-pulse rounded-full bg-indigo-400"></span>
          scoring {cmp.done ?? 0} of {cmp.total || '?'}
          {#if cmp.run}· {cmp.run}{/if}
          {#if cmp.seconds}· {Math.floor(cmp.seconds / 60)}m{/if}
          <button class="underline hover:text-gray-200"
                  onclick={() => fetch(`/api/voices/${data.voice.id}/compare`,
                                       { method: 'DELETE' }).then(readCmp)}>stop</button>
        </span>
      {:else}
        <div class="flex items-center gap-2">
          <select bind:value={cmpRun}
                  class="rounded border border-gray-800 bg-gray-900 px-2 py-1
                         text-xs text-gray-100">
            <option value="">newest run</option>
            {#each data.runs as r}
              <option value={r.run}>{r.run} · {r.steps.length} checkpoints</option>
            {/each}
          </select>
          <label class="flex items-center gap-1 text-xs text-gray-400">
            <input type="checkbox" bind:checked={cmpAll} class="accent-gray-500" />
            score every one
          </label>
          <Button size="xs" disabled={!!busy || !data.runs.length} onclick={compare}>
            {busy === 'compare' ? 'Starting…' : 'Score them'}
          </Button>
        </div>
      {/if}
    </div>
    <p class="mt-1 text-[11px] leading-relaxed text-gray-500">
      Four lines, three seeds each, through stock and then every checkpoint of
      ONE run in order, earliest first — stopping once three in a row have
      failed to beat the best so far. Stopping at the first one that scores
      worse would quit too soon: a real sweep ran 44, 27, 31, 24, 52, 44, and
      the 31 is a dip on the way to the best of the run. Runs are not compared
      with each other — they saw different corpora, so a step from the first
      has nothing useful to say about a step from the latest. A checkpoint is not better for sounding good once — it is
      better for failing less often over many tries, and every fault you have
      reported was intermittent.
    </p>
    <p class="mt-1 text-[11px] leading-relaxed text-gray-500">
      Scored with <strong class="text-gray-300">no assist</strong>, because
      that is what the training produced. With a voice sample handed over,
      every checkpoint sounds and scores alike — the sample carries the accent
      and the room, and whatever was learned sits underneath it.
    </p>

    {#if cmp.stopped && cmp.status !== 'running'}
      <p class="mt-2 text-[11px] text-amber-300/80">
        Stopped at {cmp.stopped} — three in a row failed to beat the best, so
        the rest of the run was not scored. Tick “score every one” to sweep it
        all the way to the end.
      </p>
    {/if}

    {#if cmp.status === 'running' && cmp.total}
      <div class="mt-2 h-1.5 w-full overflow-hidden rounded bg-gray-800">
        <div class="h-full bg-indigo-500 transition-[width]"
             style="width:{Math.round((cmp.done ?? 0) / cmp.total * 100)}%"></div>
      </div>
      <p class="mt-1 text-[10px] text-gray-500">
        Twelve takes per checkpoint, about a minute and a quarter each — so a
        fourteen-step run is under twenty minutes, and usually less because it
        stops once it stalls. Rows appear as they finish.
      </p>
    {/if}
    {#if cmp.error}
      <p class="mt-2 text-[11px] text-red-400">{cmp.error}</p>
    {/if}

    {#if cmp.rows?.length}
      <div class="mt-3 overflow-x-auto">
        <table class="w-full text-left text-[11px]">
          <thead class="text-gray-500">
            <tr>
              <th class="py-1 pr-3 font-normal">checkpoint</th>
              <th class="py-1 pr-3 font-normal" title="said something other than the line">wrong</th>
              <th class="py-1 pr-3 font-normal" title="still making noise after the last word">hot tail</th>
              <th class="py-1 pr-3 font-normal" title="how much the VOICE moves between takes">pitch</th>
              <th class="py-1 pr-3 font-normal" title="how much the ROOM moves between takes">floor</th>
              <th class="py-1 pr-3 font-normal" title="how much the PACE moves between takes">pace</th>
              <th class="py-1 pr-3 font-normal">hear</th>
              <th class="py-1 font-normal"></th>
            </tr>
          </thead>
          <tbody>
            {#each [...cmp.rows].sort((a: any, b: any) => a.score - b.score) as r, i}
              <tr class="border-t border-gray-800/60 {i === 0 ? 'bg-emerald-950/20' : ''}">
                <td class="py-1.5 pr-3 font-mono text-gray-200">
                  {r.checkpoint}{i === 0 ? ' ★' : ''}
                </td>
                <td class="py-1.5 pr-3 {r.wrong ? 'text-red-400' : 'text-gray-500'}">
                  {r.wrong}/{r.takes}
                </td>
                <td class="py-1.5 pr-3 {r.hot_tail ? 'text-red-400' : 'text-gray-500'}">
                  {r.hot_tail}
                </td>
                <td class="py-1.5 pr-3 text-gray-400">{r.pitch_sd ?? '—'}</td>
                <td class="py-1.5 pr-3 text-gray-400">{r.floor_sd ?? '—'}</td>
                <td class="py-1.5 pr-3 text-gray-400">{r.wpm_sd ?? '—'}</td>
                <td class="py-1.5 pr-3">
                  {#if r.sample}
                    <audio controls preload="none" src={media(r.sample)}
                           class="h-7 w-40"></audio>
                  {/if}
                </td>
                <td class="py-1.5">
                  {#if r.path && data.latest !== r.path}
                    <button class="text-indigo-400 underline hover:text-indigo-300"
                            onclick={() => promote(r.path)}>use</button>
                  {:else if r.path}
                    <span class="text-emerald-400">in use</span>
                  {/if}
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
        <p class="mt-2 text-[10px] leading-relaxed text-gray-600">
          Lower is better in every column. ★ is the best total — but twelve
          takes each is a small sample: one wrong against two is close to a
          coin flip, and the score weights faults heavily, so a candidate can
          top the table on luck while having the worst voice consistency in
          it. Read the columns, not the ranking, and listen to the top two —
          a stutter changes no words and nothing here can see one.
        </p>
      </div>
    {/if}
  </div>

  <!-- Hear it. Checkpoints chosen on loss curves are how a model that had
       forgotten how to stop got promoted and filled a video with noise the
       metrics could not see. -->
  <!-- The clone copies its sample: the voice, and also the sample's background
       clicks and its pace. Chosen here by what the clone produces from it. -->
  <div class="rounded-lg border border-gray-800 bg-gray-950 p-4">
    <div class="flex items-center justify-between">
      <h2 class="text-sm font-semibold">Voice sample</h2>
      {#if smp.status === 'running'}
        <span class="flex items-center gap-2 text-[11px] text-gray-400">
          <span class="h-2 w-2 animate-pulse rounded-full bg-indigo-400"></span>
          {smp.step || 'working'}
          {#if smp.rows?.length}· {smp.rows.length} scored{/if}
          · {Math.floor((smp.seconds ?? 0) / 60)}m
          <button class="underline hover:text-gray-200"
                  onclick={() => fetch(`/api/voices/${data.voice.id}/samples`, { method: 'DELETE' }).then(readSmp)}>stop</button>
        </span>
      {:else}
        <!-- Candidates are cut from this voice's reads, which training
             splits into clips — so a voice needs a run before it has any. -->
        <Button size="xs" disabled={!!busy || (!data.voice.reference && !data.runs.length)}
                onclick={findSample}>
          {data.voice.reference ? 'Find a better sample' : 'Find a sample'}
        </Button>
      {/if}
    </div>
    <p class="mt-1 text-xs leading-relaxed text-gray-500">
      The clone copies the sample it is given — its background clicks and its
      pace, not only the voice. This builds candidates from every recording of
      this voice, scores each by what the clone actually produces through the
      promoted checkpoint, and puts them here to listen to. It starts by itself
      when a checkpoint is promoted, takes about fifteen minutes, and holds the
      card while it scores. Nothing changes until you choose.
    </p>
    {#if smp.current}
      <p class="mt-2 text-[11px] text-gray-400">
        In use: {typeof smp.current.from === 'string' ? smp.current.from : `${smp.current.from?.length ?? 0} clips from your recordings`}
        {#if smp.current.scores}· {smp.current.scores.clicks_per_min} clicks/min · {smp.current.scores.wpm} wpm{/if}
        {#if smp.current.adopted}· since {String(smp.current.adopted).slice(0, 10)}{/if}
      </p>
    {/if}
    {#if smp.status === 'error' && smp.error}<p class="mt-2 text-xs text-red-400">{smp.error}</p>{/if}
    {#if smp.rows?.length}
      <table class="mt-3 w-full text-left text-xs">
        <thead class="text-[10px] uppercase tracking-wide text-gray-500">
          <tr>
            <th class="py-1">sample</th><th>wrong</th><th>clicks/min</th><th>pace</th>
            <th>pace wander</th><th>pitch wander</th><th>level wander</th><th>listen</th><th></th>
          </tr>
        </thead>
        <tbody>
          {#each smp.rows as r, k}
            <tr class="border-t border-gray-800 {smp.done && k === 0 ? 'text-emerald-200' : 'text-gray-300'}">
              <td class="py-1.5 font-mono">
                {r.sample.replace(/^sample_/, '')}{#if smp.done && k === 0} ★{/if}
                {#if r.sample === 'sample_current'}<span class="font-sans text-gray-500"> (in use)</span>{/if}
              </td>
              <td class={r.gated ? 'text-red-400' : ''}
                  title={r.gated ? 'set aside: said the wrong words in more than one take in five' : ''}>
                {r.wrong}/{r.takes}{#if r.gated} ✕{/if}
              </td>
              <td>{r.clicks_per_min}</td>
              <td>{r.wpm}</td>
              <td>{r.wpm_sd}</td>
              <td>{r.pitch_sd_st}</td>
              <td>{r.level_sd_db}</td>
              <td><audio controls preload="none" src={media(r.listen)} class="h-7 w-44"></audio></td>
              <td>
                {#if smp.done && r.sample !== 'sample_current'}
                  <Button size="xs" color="alternative" disabled={!!busy}
                          onclick={() => adoptSample(r.sample)}>Use this sample</Button>
                {/if}
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
      <p class="mt-2 text-[10px] leading-relaxed text-gray-500">
        ★ scores best on what you hear in every take: clicks, distance from
        your presenting pace (about 185), and how much pace, pitch and level
        wander. Wrong words don't rank samples — fourteen takes can't tell them
        apart, and a render retries a wrong take anyway — but a sample wrong in
        more than one take in five is set aside (✕). Each player is the same
        line and seed, so only the sample differs. Your ear has the last word;
        the sample in use is kept in history when you switch.
      </p>
    {/if}
  </div>

  <div class="rounded-lg border border-gray-800 bg-gray-950 p-4">
    <h2 class="text-sm font-semibold">Hear this voice</h2>
    <p class="mt-1 text-[11px] leading-relaxed text-gray-500">
      Type a line, or give a subject and let the writer produce one. Compare a
      checkpoint against stock before promoting it — a loss curve cannot hear
      a click in a pause.
    </p>
    <p class="mt-1 text-[11px] leading-relaxed text-gray-500">
      This plays the model ALONE, which is what the training actually
      produced. <strong class="text-gray-300">Voice sample assist</strong>
      hands it a few seconds of the real recording to imitate — that carries
      your accent, your distance from the microphone and your room, and it
      does most of the work. With it on, every checkpoint sounds alike,
      because you are mostly hearing the sample.
    </p>

    <div class="mt-3 space-y-2">
      <textarea rows="2" bind:value={benchText}
                placeholder="Something for it to say…"
                class="w-full resize-none rounded border border-gray-800 bg-gray-900 p-2
                       text-sm text-gray-100 focus:border-indigo-600 focus:ring-0"></textarea>
      <div class="flex flex-wrap items-center gap-2">
        <input bind:value={benchAbout} placeholder="…or a subject to write about"
               class="min-w-[12rem] flex-1 rounded border border-gray-800 bg-gray-900 px-2 py-1
                      text-xs text-gray-100 focus:border-indigo-600 focus:ring-0" />
        <input bind:value={benchDirection} placeholder="direction, e.g. enthusiastic"
               class="min-w-[10rem] flex-1 rounded border border-gray-800 bg-gray-900 px-2 py-1
                      text-xs text-gray-100 focus:border-indigo-600 focus:ring-0" />
      </div>
      <div class="flex flex-wrap items-center gap-2 text-[11px]">
        <select bind:value={benchCkpt}
                class="rounded border border-gray-800 bg-gray-900 px-2 py-1 text-xs text-gray-100">
          <option value={null}>the promoted one</option>
          <option value="">stock weights</option>
          {#each data.runs as r}
            {#each r.steps as step}
              <option value={`${r.dir}/${step}`}>{r.run} {step.replace('step_', '')}</option>
            {/each}
          {/each}
        </select>
        <label class="flex items-center gap-1 text-gray-400"
               title="Hand the model a few seconds of the real recording to imitate">
          <input type="checkbox" class="accent-indigo-500" bind:checked={benchAssist} />
          voice sample assist
        </label>
        <label class="flex items-center gap-1 text-gray-400">
          pace
          <input type="number" min="0" max="260" step="10" bind:value={benchWpm}
                 class="w-16 rounded border border-gray-800 bg-gray-900 px-1 py-0.5
                        text-xs text-gray-100" />
        </label>
        <Button size="xs" color="primary" disabled={saying} onclick={say}>
          {saying ? 'Generating…' : 'Say it'}
        </Button>
      </div>
    </div>

    {#if benchOut}
      <div class="mt-3 rounded border border-gray-800 bg-gray-900/50 p-3">
        <p class="text-xs leading-relaxed text-gray-200">{benchOut.text}</p>
        <audio controls src={media(benchOut.path)} class="mt-2 w-full"></audio>
        <div class="mt-1 text-[10px] text-gray-500">
          {benchOut.seconds?.toFixed?.(1)}s · {benchOut.speechWpm ?? '—'} wpm ·
          {benchOut.mode === 'model alone' ? 'no assist' : 'with assist'} ·
          pace {benchOut.targetWpm || 'off'} ·
          {benchOut.usedCheckpoint
            ? benchOut.usedCheckpoint.split('/').slice(-2).join(' ')
            : 'stock weights'}
        </div>
      </div>
    {/if}
  </div>
</div>
