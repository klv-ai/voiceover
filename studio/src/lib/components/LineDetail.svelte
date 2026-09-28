<script lang="ts">
  import { Button, Range, Toggle, Select } from 'flowbite-svelte';
  import { signature, blockLabel, type Project, type Segment } from '$lib/types';

  let {
    seg,
    project,
    busy,
    onsynth,
    onseek,
    oncontext,
    onsave,
    previewReady,
    nudge
  }: {
    /** Edited IN PLACE. Not bindable on purpose: a two-way binding to a
        computed array index writes back through a setter that re-resolves the
        index when it fires, which put one line's text into the next one. */
    seg: Segment;
    project: Project;
    busy: string;
    onsynth: (s: Segment) => Promise<void> | void;
    onseek: () => void;
    oncontext: (s: Segment) => Promise<void> | void;
    /** Persist the project. The script edit box is the ONLY place in this app
        where the operator types something they cannot get back, so it may not
        rely on some later action happening to save for it. */
    onsave: () => Promise<void> | void;
    previewReady: boolean;
    /** How far this take can move before it would collide with its neighbours. */
    nudge: { min: number; max: number };
  } = $props();

  let playing = $state('');
  const media = (p?: string) => (p ? `/api/media?path=${encodeURIComponent(p)}` : '');
  const fmt = (s: number) => `${Math.floor(s / 60)}:${(s % 60).toFixed(1).padStart(4, '0')}`;

  const dirty = $derived(seg.say !== seg.sayOriginal);
  const words = $derived(seg.say.trim().split(/\s+/).filter(Boolean).length);
  const rises = $derived(seg.terminalStep != null && seg.terminalStep > 0.4);
  const stale = $derived(!!seg.audio && seg.renderedFrom !== signature(seg, project.settings));

  function autosize(node: HTMLTextAreaElement) {
    const fit = () => { node.style.height = 'auto'; node.style.height = `${node.scrollHeight + 2}px`; };
    fit();
    node.addEventListener('input', fit);
    return { destroy: () => node.removeEventListener('input', fit) };
  }

  async function playOne(which: 'take' | 'original') {
    if (which === 'take' && (stale || !seg.audio)) await onsynth(seg);
    playing = which;
    try {
      let src = seg.audio;
      if (which === 'original') {
        const r = await fetch(`/api/projects/${project.id}/segment/${seg.id}/original`, { method: 'POST' });
        src = (await r.json()).path;
      }
      if (!src) return;
      const a = new Audio(media(src) + `&v=${Date.now()}`);
      await new Promise<void>((res) => { a.onended = () => res(); a.onerror = () => res(); a.play(); });
    } finally { playing = ''; }
  }

  /** A new seed. Prosody varies per generation more than any slider does — the
      same line measured -3.9 to +2.2 semitones across three seeds — so when a
      line lands wrong, drawing again is usually the fix. */
  const reroll = () => { seg.seed = Math.floor(Math.random() * 1_000_000); onsynth(seg); };

  /**
   * Persist an edited line.
   *
   * `bind:value` writes into the project object in memory and NOTHING ELSE.
   * Rewriting a line therefore looked saved, survived clicking around, and was
   * gone on the next load — or worse, was overwritten by another tab whose
   * copy of the script was older. A text box with no save is a text box that
   * loses work, so: on blur, and a second after typing stops.
   */
  let saveState = $state<'clean' | 'typing' | 'saving' | 'saved'>('clean');
  let timer: ReturnType<typeof setTimeout> | null = null;

  async function commit() {
    if (timer) { clearTimeout(timer); timer = null; }
    if (saveState === 'clean' || saveState === 'saved') return;
    saveState = 'saving';
    await onsave();
    saveState = 'saved';
  }

  const idx = $derived(project.segments.findIndex((x) => x.id === seg.id));
  const next = $derived(project.segments[idx + 1] ?? null);
  const takeLen = $derived(seg.duration ?? (seg.end - seg.start));
  /** Seconds between the end of this take and the start of the next one.
      Negative means it runs into it, which is audible as the line being cut
      off mid-word. */
  const gap = $derived(next
    ? (next.start + (next.offset ?? 0))
      - (seg.start + (seg.offset ?? 0) + takeLen * (seg.stretch ?? 1))
    : 0);

  /**
   * Has this block's LENGTH been changed from what the audio is?
   *
   * Two separate things make a block longer or shorter than its take: a
   * stretch, which alters the audio, and held room, which does not. They had a
   * reset each — and `stretch` had none at all — so putting a block back the
   * way it was meant knowing which of them had been nudged. A block's size is
   * one idea to the operator, and a fumbled drag is one mistake.
   */
  const sized = $derived((seg.stretch ?? 1) !== 1 || (seg.hold ?? 0) > 0.005);

  /** Back to exactly the length of the audio. Position is left alone: where a
      line sits and how long it is are different decisions, and undoing one
      must not undo the other. */
  function resetLength() {
    seg.stretch = 1;
    seg.hold = 0;
    saveState = 'typing';
    commit();
  }

  /** Keep `d` more seconds after this line, up to whatever is free before the
      next one. Grows THIS block; the neighbour does not move. */
  function room(d: number) {
    const own = takeLen * (seg.stretch ?? 1);
    const free = next
      ? (next.start + (next.offset ?? 0)) - (seg.start + (seg.offset ?? 0)) - own - 0.05
      : Infinity;
    seg.hold = Math.round(Math.max(0, Math.min(Math.max(0, free), (seg.hold ?? 0) + d)) * 100) / 100;
    saveState = 'typing';
    commit();
  }

  function typed() {
    saveState = 'typing';
    if (timer) clearTimeout(timer);
    timer = setTimeout(commit, 1000);
  }

  /** "s031" -> "31". The id carries the number the operator counts in. */
  const track = $derived(blockLabel(seg));
</script>

<div class="rounded-lg border p-4 {seg.drop ? 'border-red-900/50 bg-red-950/10'
                                    : seg.locked ? 'border-emerald-900/60 bg-emerald-950/20'
                                    : 'border-gray-800 bg-gray-950'}">
  <div class="mb-3 flex flex-wrap items-center gap-2 text-[11px] text-gray-500">
    <!-- Its number, first and always. Without it a line can only be referred to
         by quoting its words back, which is unworkable across thirty-seven of
         them. Taken from the id, which is already 1-based and zero-padded, so
         it cannot drift out of step with a filtered or reordered view. -->
    <span class="rounded bg-gray-800 px-1.5 py-0.5 font-mono text-gray-300"
          title="track {track} — id {seg.id}">{track}</span>
    <button class="font-mono hover:text-indigo-400" onclick={onseek}>{fmt(seg.start)}</button>
    <span>·</span><span>{(seg.end - seg.start).toFixed(2)}s recorded</span>
    {#if seg.duration && !seg.locked}
      <span class="text-indigo-400">→ {seg.duration.toFixed(2)}s rendered · {seg.wpm} wpm</span>
    {/if}
    {#if rises}
      <span class="rounded bg-amber-900/70 px-1.5">ends +{seg.terminalStep?.toFixed(1)} st ↑ reads as a question</span>
    {:else if seg.terminalStep != null && !seg.locked}
      <span class="rounded bg-gray-800 px-1.5">ends {seg.terminalStep.toFixed(1)} st</span>
    {/if}
    {#if sized}
      <button class="rounded bg-amber-900/60 px-1.5 text-amber-200 hover:bg-amber-800"
              title="this block is not the length of its audio — put it back"
              onclick={resetLength}>
        resized {(seg.stretch ?? 1) !== 1 ? `${((seg.stretch ?? 1) * 100).toFixed(0)}%` : ''}{(seg.hold ?? 0) > 0.005 ? ` +${(seg.hold ?? 0).toFixed(2)}s` : ''} · reset
      </button>
    {/if}
    {#if stale}<span class="rounded bg-indigo-900/70 px-1.5">changed since rendering</span>{/if}
    {#if seg.cleaned}<span class="rounded bg-sky-900/60 px-1.5">rewritten</span>{/if}
    {#if dirty && !seg.cleaned}<span class="rounded bg-amber-900/60 px-1.5">edited</span>{/if}
    <div class="ml-auto flex items-center gap-3">
      <Toggle bind:checked={seg.locked} size="small"><span class="text-[11px]">keep my voice</span></Toggle>
      <!-- Leaving a line out is a per-line decision, so it belongs on the line.
           It was only available in the script list, which meant scrolling away
           from the block being judged in order to drop it. -->
      <button class="rounded px-2 py-0.5 text-[11px] transition
                     {seg.drop ? 'bg-red-900/70 text-red-200 hover:bg-red-800'
                               : 'bg-gray-800 text-gray-400 hover:bg-gray-700'}"
              title={seg.drop
                ? 'this line is left out of the video — click to put it back'
                : 'leave this line out: no take, no recording, and the render glides through its space'}
              onclick={() => { seg.drop = !seg.drop; saveState = 'typing'; commit(); }}>
        {seg.drop ? 'left out' : 'leave out'}
      </button>
    </div>
  </div>

  {#if seg.drop}
    <!-- Say it plainly. Everything below still works — the take can be played,
         the text edited, the timing moved — and none of it reaches the video,
         which is not something to leave the operator to infer from a pill
         going grey somewhere else on the page. -->
    <p class="mb-3 rounded border border-red-900/70 bg-red-950/30 p-2 text-[11px] text-red-200">
      <strong>Track {track} is left out of the video.</strong>
      Its space is free for the lines either side to move into, and the render
      glides straight through it. Nothing below affects the mix until you put
      it back.
    </p>
  {/if}

  {#if seg.heard && seg.heard !== seg.say}
    <p class="mb-2 text-[11px] italic text-gray-600">transcribed: {seg.heard}</p>
  {/if}

  <textarea use:autosize bind:value={seg.say} disabled={seg.locked} rows="2"
            oninput={typed} onblur={commit}
            onkeydown={(e) => { if ((e.metaKey || e.ctrlKey) && e.key === 's') { e.preventDefault(); commit(); } }}
            class="w-full resize-none overflow-hidden rounded-lg border border-gray-800 bg-gray-900 p-3
                   text-sm leading-relaxed text-gray-100 focus:border-indigo-600 focus:ring-0
                   disabled:opacity-50"></textarea>

  <!-- Say what happened to the words. "There is no save option" is a fair
       complaint about a box that silently keeps or silently drops what you
       typed; the state has to be on screen either way. -->
  <div class="mt-1 flex h-5 items-center gap-2 text-[10px]">
    {#if saveState === 'typing'}
      <span class="text-amber-400">unsaved</span>
      <button class="rounded border border-amber-700 px-1.5 text-amber-300 hover:bg-amber-950/50"
              onclick={commit}>Save now</button>
      <span class="text-gray-600">saves on its own a second after you stop, or when you click away</span>
    {:else if saveState === 'saving'}
      <span class="text-gray-500">saving…</span>
    {:else if saveState === 'saved'}
      <span class="text-emerald-500">saved</span>
    {/if}
  </div>

  <div class="mt-3 flex flex-wrap items-center gap-2">
    <Button size="xs" color={stale || !seg.audio ? 'primary' : 'alternative'}
            disabled={!!busy || seg.locked || playing === 'take'} onclick={() => playOne('take')}>
      {playing === 'take' ? 'Playing…' : busy === `synth:${seg.id}` ? 'Rendering…'
        : stale || !seg.audio ? 'Render & play' : 'Play this take'}
    </Button>
    <Button size="xs" color="alternative" disabled={playing === 'original'} onclick={() => playOne('original')}>
      {playing === 'original' ? 'Playing…' : 'Play the original'}
    </Button>
    <Button size="xs" color="alternative" disabled={!!busy || seg.locked} onclick={reroll}>Draw again</Button>
    <Button size="xs" color="purple" disabled={!!busy || !seg.audio || seg.locked}
            onclick={() => oncontext(seg)}
            title="play the assembled track from just before this line">
      {previewReady ? 'Hear in context' : 'Build mix & hear'}
    </Button>
    {#if dirty}
      <Button size="xs" color="alternative"
              onclick={() => { seg.say = seg.sayOriginal; seg.cleaned = false; }}>Revert text</Button>
    {/if}
    <span class="ml-auto text-[11px] text-gray-600">{words} words</span>
  </div>

  {#if !seg.locked && seg.audio}
    <div class="mt-3 border-t border-gray-800 pt-3">
      <div class="mb-1 flex items-center gap-2 text-[11px] text-gray-400">
        <span>Timing</span>
        <span class="font-mono {(seg.offset ?? 0) === 0 ? 'text-gray-500' : 'text-indigo-400'}">
          {(seg.offset ?? 0) > 0 ? '+' : ''}{(seg.offset ?? 0).toFixed(2)}s
        </span>
        {#if (seg.offset ?? 0) !== 0}
          <button class="text-gray-500 underline hover:text-gray-300"
                  onclick={() => { seg.offset = 0; saveState = 'typing'; commit(); }}>reset</button>
        {/if}
        <span class="ml-auto font-mono text-gray-600">
          room {nudge.min.toFixed(2)} … +{Math.max(0, nudge.max).toFixed(2)}s
        </span>
      </div>
      <input type="range" class="w-full accent-indigo-500"
             min={nudge.min} max={Math.max(nudge.min, nudge.max)} step="0.02"
             value={seg.offset ?? 0}
             oninput={(e) => { seg.offset = +(e.currentTarget as HTMLInputElement).value;
                               saveState = 'typing'; }}
             onchange={commit} />
      <p class="mt-1 text-[10px] text-gray-600">
        Slide left to take up dead space before this line, right to lengthen the
        pause. Bounded by the lines either side, so takes can never overlap.
      </p>
    </div>
  {/if}

  {#if next}
    <!-- The right-edge drag, as a number.
         A short line is a narrow pill — track 25 is three seconds of a
         four-minute video — and hunting for an eight-pixel handle beside its
         neighbour is not an edit, it is a fight. Same operation, typed. -->
    <div class="mt-2 rounded-lg border border-gray-800 bg-gray-900/40 p-2">
      <div class="flex items-center gap-2 text-[11px]">
        <span class="text-gray-400">Room this line keeps</span>
        <span class="font-mono {(seg.hold ?? 0) > 0 ? 'text-indigo-400' : 'text-gray-500'}">
          {(seg.hold ?? 0).toFixed(2)}s
        </span>
        <span class="text-gray-600">of {Math.max(0, gap).toFixed(2)}s free before track
          {blockLabel(next)}</span>
        {#if (seg.hold ?? 0) > 0}
          <button class="text-gray-500 underline hover:text-gray-300"
                  onclick={() => { seg.hold = 0; saveState = 'typing'; commit(); }}>reset</button>
        {/if}
        <span class="ml-auto flex items-center gap-1">
          <button class="rounded border border-gray-700 px-2 py-0.5 hover:bg-gray-800"
                  onclick={() => room(-0.1)}>−0.1s</button>
          <button class="rounded border border-gray-700 px-2 py-0.5 hover:bg-gray-800"
                  onclick={() => room(+0.1)}>+0.1s</button>
          <button class="rounded border border-gray-700 px-2 py-0.5 hover:bg-gray-800"
                  onclick={() => room(+0.5)}>+0.5s</button>
        </span>
      </div>
      <p class="mt-1 text-[10px] leading-relaxed text-gray-600">
        The block keeps this much space after the words, and the render will
        not glide the picture through it. The take itself is untouched —
        nothing is time-stretched.
        {#if gap < 0}
          <span class="text-rose-400">This line currently runs {(-gap).toFixed(2)}s into the next one.</span>
        {/if}
      </p>
    </div>
  {/if}

  <p class="mt-2 text-[11px] leading-relaxed text-gray-500">
    <strong class="text-gray-400">Hear in context</strong> plays the assembled
    track from a moment before this line, so you judge it against the lines
    either side rather than in isolation. Rendering a line folds it into that
    track automatically.
    <br />
    <strong class="text-gray-400">Draw again</strong> re-rolls the random seed and re-renders.
    The model's phrasing varies more between seeds than between any two slider
    positions — the same sentence measured −3.9 to +2.2 semitones across three
    draws — so when a line lands wrong, drawing again is usually the quicker fix.
  </p>

  {#if !seg.locked}
    <div class="mt-4 grid gap-4 border-t border-gray-800 pt-4 sm:grid-cols-2">
      <div>
        <div class="mb-1 text-[11px] text-gray-400">
          Rate · {seg.over?.targetWpm ?? project.settings.targetWpm} wpm
          {#if seg.over?.targetWpm}<span class="text-indigo-400">(overridden)</span>{/if}
        </div>
        <Range min="140" max="240" step="5"
               value={seg.over?.targetWpm ?? project.settings.targetWpm}
               onchange={(e) => (seg.over = { ...seg.over, targetWpm: +(e.target as HTMLInputElement).value })} />
        <p class="mt-1 text-[10px] text-gray-600">Below ~170 it drawls rather than slows.</p>
      </div>
      <div>
        <div class="mb-1 text-[11px] text-gray-400">Sentence ending</div>
        <Select size="sm" value={seg.over?.terminal ?? 'none'}
                onchange={(e) => (seg.over = { ...seg.over, terminal: (e.target as HTMLSelectElement).value as 'none' | 'fall' })}
                items={[{ value: 'none', name: 'as generated' }, { value: 'fall', name: 'force a falling close' }]} />
        <p class="mt-1 text-[10px] text-gray-600">Use when the badge above says it rises.</p>
      </div>
      {#if seg.over && Object.keys(seg.over).length}
        <div class="sm:col-span-2">
          <Button size="xs" color="alternative" onclick={() => (seg.over = undefined)}>
            Clear this line's overrides
          </Button>
        </div>
      {/if}
    </div>
  {/if}
</div>
