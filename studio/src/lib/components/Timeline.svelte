<script lang="ts">
  import { untrack } from 'svelte';
  import { signature, blockLabel, type Project, type Segment, type Mask } from '$lib/types';

  let {
    project,
    duration,
    playhead = 0,
    selectedId = $bindable(),
    track = $bindable(),
    onseek,
    original = null,
    preview = null,
    originalSpan = null,
    previewSpan = null,
    previewStale = false,
    loading = false,
    onnudge,
    onhold,
    onmenu,
    onpick,
    picked = [],
    onmaskselect,
    onmaskdone,
    selectedMask = '',
    gapMin = 0.8
  }: {
    project: Project;
    duration: number;
    playhead?: number;
    selectedId: string;
    /** Which lane you are hearing. Bindable, because the lanes ARE the switch. */
    track: 'original' | 'preview';
    onseek: (t: number) => void;
    original?: number[] | null;
    preview?: number[] | null;
    /** Seconds of audio each peak strip covers, so it can be drawn on the
        timeline's own scale rather than stretched to fill it. */
    originalSpan?: number | null;
    previewSpan?: number | null;
    previewStale?: boolean;
    loading?: boolean;
    /** A pill was dragged to a new offset and let go. */
    onnudge?: (id: string) => void;
    /** A gap was held or released. */
    onhold?: (id: string) => void;
    /** A right-click on the strip: the moment, and where to put a menu. */
    onmenu?: (t: number, x: number, y: number) => void;
    /** A block was clicked; `shift` asks to extend the selection to it. */
    onpick?: (id: string, shift: boolean) => void;
    /** Blocks picked together with shift-click, drawn as one selection. */
    picked?: string[];
    /** A mask's bar was pressed: select it. */
    onmaskselect?: (id: string) => void;
    /** A mask's bar was dragged in time and let go: save its new span. */
    onmaskdone?: (id: string) => void;
    selectedMask?: string;
    gapMin?: number;
  } = $props();

  // A zero or missing duration must not size anything. Falling back to 1
  // second made every block a thousand times too wide and pushed the whole
  // page sideways, which is what a divisor of `duration || 1` buys you.
  const ok = $derived(duration > 0.5);

  /**
   * How much of the video the strip is showing.
   *
   * At 1x the whole thing, which is unusable for fine work: a three-second
   * line in a seven-minute video is eleven pixels wide, and its drag handle is
   * a fraction of that. Zooming in narrows the window rather than stretching
   * the strip, so everything — pills, gaps, the waveform, the playhead —
   * stays on one shared scale.
   */
  /**
   * How far the strip reaches.
   *
   * NOT the video's length. Takes get nudged later and given room, and the
   * last of them can finish after the picture does — two seconds past it here,
   * which put the closing line off the right-hand edge where it was clipped
   * away entirely. Audible, and invisible. The strip has to cover everything
   * it is drawing, so it ends wherever the last block ends.
   */
  const extent = $derived.by(() => {
    let end = duration;
    for (const s of project.segments) {
      if (s.drop) continue;
      const e = placedStart(s) + blockLen(s);
      if (e > end) end = e;
    }
    return end;
  });

  let zoom = $state(1);
  let viewFrom = $state(0);
  const viewSpan = $derived(ok ? extent / zoom : 1);
  const maxFrom = $derived(Math.max(0, extent - viewSpan));

  const clampView = (t: number) => Math.max(0, Math.min(maxFrom, t));

  /** Centre the window on a moment, without running off either end. */
  function centreOn(t: number) {
    viewFrom = clampView(t - viewSpan / 2);
  }

  function setZoom(z: number) {
    const mid = viewFrom + viewSpan / 2;
    zoom = z;
    // `viewSpan` is derived, so compute the new one here rather than reading it
    // back before the effect has run.
    viewFrom = Math.max(0, Math.min(Math.max(0, extent - extent / z), mid - extent / z / 2));
  }

  // Keep the selected line in view when zoomed in. Only when it is actually
  // off screen: nudging it a pixel at a time must not make the strip jump.
  $effect(() => {
    if (zoom === 1 || !ok) return;
    const s = project.segments.find((x) => x.id === selectedId);
    if (!s) return;
    const a = placedStart(s);
    const b = a + blockLen(s);
    if (a < viewFrom + 0.05 || b > viewFrom + viewSpan - 0.05) untrack(() => centreOn((a + b) / 2));
  });

  const fmt = (t: number) =>
    `${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, '0')}`;

  /** A POSITION on the strip: where a moment in the video sits. */
  const pct = (t: number) =>
    (ok ? `${(((t - viewFrom) / viewSpan) * 100).toFixed(4)}%` : '0%');

  /**
   * A LENGTH on the strip: how wide a stretch of time is.
   *
   * Not the same function. A position has the window's start subtracted from
   * it; a length must not, or it shrinks by however far into the video the
   * window has been scrolled — which is why the pills stayed hairlines while
   * the waveform above them zoomed correctly.
   */
  const wide = (dt: number) =>
    (ok ? `${((Math.max(0, dt) / viewSpan) * 100).toFixed(4)}%` : '0%');

  function lineState(s: Segment) {
    if (s.drop) return 'dropped';
    if (s.locked) return 'real';
    if (!s.audio) return 'todo';
    return s.renderedFrom !== signature(s, project.settings) ? 'stale' : 'done';
  }

  const FILL: Record<string, string> = {
    // Left out of the video: still drawn, because the operator needs to see
    // WHERE the space he reclaimed was, but plainly not part of the mix.
    dropped: 'bg-gray-800/40 hover:bg-gray-700/60',
    real: 'bg-emerald-600/70 hover:bg-emerald-500',
    done: 'bg-indigo-600/80 hover:bg-indigo-500',
    stale: 'bg-amber-600/70 hover:bg-amber-500',
    todo: 'bg-gray-700/70 hover:bg-gray-600'
  };

  const rendered = $derived(project.segments.filter((s) => s.audio && !s.locked).length);

  // A dropped line contributes nothing to the mix, so it is not a take: it
  // cannot be dragged, stretched, or collide with anything.
  const isTake = (s: Segment) => !!s.audio && !s.locked && !s.drop;
  /** "s031" -> "31", so a pill can be named out loud. */
  const num = (s: Segment) => blockLabel(s);
  /** A rendered line occupies its TAKE's length at its nudged position — not
      the span of the recording it replaced. Drawing the original span instead
      is what made a pill look full when most of it is now dead air. */
  const takeLen = (s: Segment) => s.duration;
  const placedLen = (s: Segment) => {
    const n = isTake(s) ? takeLen(s) : 0;
    return n ? n * (s.stretch ?? 1) : s.end - s.start;
  };
  const placedStart = (s: Segment) => s.start + (isTake(s) ? (s.offset ?? 0) : 0);
  const placedEnd = (s: Segment) => placedStart(s) + placedLen(s);
  /** Take plus the room it keeps. This is the block's real extent: what it
      occupies, and what the renderer will not glide away. */
  const blockLen = (s: Segment) => placedLen(s) + (isTake(s) ? (s.hold ?? 0) : 0);
  const blockEnd = (s: Segment) => placedStart(s) + blockLen(s);

  /** The same clamp the assembler applies: never past the take already placed
      before this one, never into the next one. Both edges follow where takes
      ACTUALLY sit, not the spans of the recordings they replaced — a line that
      finishes early leaves real room, and the neighbour should be able to use
      it. */
  /**
   * The nearest neighbour that is actually in the video.
   *
   * A line left out contributes nothing to the mix and the render glides
   * straight through its space — so it must not fence anything in. Stopping a
   * drag at a dropped line hands back the space with one click and refuses to
   * let anything use it, which is the opposite of the point.
   */
  function liveAt(i: number, step: -1 | 1) {
    for (let k = i + step; k >= 0 && k < project.segments.length; k += step) {
      if (!project.segments[k].drop) return project.segments[k];
    }
    return null;
  }

  function bounds(i: number) {
    const seg = project.segments[i];
    const prev = liveAt(i, -1);
    const next = liveAt(i, 1);
    const floor = prev ? blockEnd(prev) + 0.02 : 0;
    const ceil = next ? Math.max(placedStart(next), next.start) - 0.02 : extent;
    return { min: floor - seg.start, max: ceil - (seg.start + placedLen(seg)) };
  }

  /** Seconds by which this line runs into the next.
      Pacing to the operator's own read means a take is whatever length it
      needs — sometimes longer than the slot it replaced. That is allowed; it
      just has to be visible, so the collision can be nudged out by hand. */
  const OVERLAP_MIN = 0.06;
  function over(i: number) {
    const next = project.segments[i + 1];
    if (!next || !isTake(project.segments[i]) || project.segments[i].drop) return 0;
    let j = i + 1;
    while (j < project.segments.length && project.segments[j].drop) j++;
    const n = project.segments[j];
    if (!n) return 0;
    const d = blockEnd(project.segments[i]) - Math.max(placedStart(n), 0);
    return d > OVERLAP_MIN ? d : 0;
  }
  const clashes = $derived(
    project.segments.map((s, i) => ({ s, i, d: over(i) })).filter((x) => x.d > 0)
  );

  let drag: { id: string; x0: number; off0: number; moved: boolean } | null = $state(null);
  /** Dragging the RIGHT EDGE gives the line more room instead of moving it. */
  let sizing: { id: string; x0: number; s0: number; moved: boolean } | null = $state(null);

  /** How far this line may stretch before it would reach the next one. */
  function room(i: number) {
    const seg = project.segments[i];
    const next = liveAt(i, 1);
    const ceil = next ? placedStart(next) - 0.05 : extent;
    const natural = takeLen(seg) || (seg.end - seg.start);
    // 1.6x is a drawl, not a pace. The ceiling was set when takes came out too
    // SHORT and the only way to fill a hole was to stretch the words into it;
    // an operator who did that across the opening six lines then heard them
    // played 40-67% slower than he had spoken them. Space is reclaimed by
    // gliding the picture through it downstream, so a take does not have to
    // cover it. Keep what is left for fine adjustment only.
    return Math.max(0.85, Math.min(1.25, (ceil - placedStart(seg)) / Math.max(natural, 0.05)));
  }

  function sizeDown(e: PointerEvent, seg: Segment) {
    if (!isTake(seg) || !ok) return;
    e.stopPropagation();
    try { (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId); } catch { /* ignore */ }
    const j = project.segments.indexOf(seg);
    sizing = {
      id: seg.id, x0: e.clientX, moved: false, s0: seg.stretch ?? 1
    };
  }

  /** Give a line more room: stretch its take into the space, within the
      fine-adjustment range `room` allows. */
  function sizeMove(e: PointerEvent, seg: Segment, i: number) {
    if (!sizing || sizing.id !== seg.id || !strip) return;
    if (Math.abs(e.clientX - sizing.x0) > 3) sizing.moved = true;
    const w = strip.getBoundingClientRect().width || 1;
    const dt = ((e.clientX - sizing.x0) / w) * viewSpan;
    const natural = takeLen(seg) || (seg.end - seg.start);
    const want = sizing.s0 + dt / Math.max(natural, 0.05);
    seg.stretch = Math.round(Math.max(0.7, Math.min(room(i), want)) * 100) / 100;
  }

  function sizeUp(seg: Segment) {
    if (!sizing || sizing.id !== seg.id) return;
    const moved = sizing.moved;
    sizing = null;
    if (!moved) return;
    onnudge?.(seg.id);
  }

  function down(e: PointerEvent, seg: Segment) {
    if (!isTake(seg) || !ok) return;
    // Capture keeps the drag alive if the cursor leaves the pill; a browser
    // that refuses (or a synthetic event) must not break the drag itself.
    try { (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId); } catch { /* ignore */ }
    drag = { id: seg.id, x0: e.clientX, off0: seg.offset ?? 0, moved: false };
  }

  function move(e: PointerEvent, seg: Segment, i: number) {
    if (!drag || drag.id !== seg.id || !strip) return;
    if (Math.abs(e.clientX - drag.x0) > 3) drag.moved = true;
    const w = strip.getBoundingClientRect().width || 1;
    const dt = ((e.clientX - drag.x0) / w) * viewSpan;
    const b = bounds(i);
    seg.offset = Math.round(Math.min(Math.max(drag.off0 + dt, b.min), Math.max(b.min, b.max)) * 100) / 100;
  }

  function up(seg: Segment, shift = false) {
    if (!drag || drag.id !== seg.id) return;
    const moved = drag.moved;
    drag = null;
    // A press that did not travel is still a click: select the line — or,
    // with shift held, extend the selection to it (to merge a run of blocks).
    if (moved) onnudge?.(seg.id);
    else if (shift && onpick) onpick(seg.id, true);
    else { selectedId = seg.id; onseek(seg.start); onpick?.(seg.id, false); }
  }

  /** Dragging a mask's bar in TIME: the whole bar, or one of its ends.
      Pixels become seconds on the strip's own scale, so zooming in gives
      finer control. */
  let mdrag: { id: string; mode: 'move' | 'from' | 'to'; x0: number; f0: number; t0: number; moved: boolean } | null =
    $state(null);
  function mdown(e: PointerEvent, m: Mask, mode: 'move' | 'from' | 'to') {
    e.stopPropagation();
    try { (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId); } catch { /* ignore */ }
    mdrag = { id: m.id, mode, x0: e.clientX, f0: m.from, t0: m.to, moved: false };
    onmaskselect?.(m.id);
  }
  function mmove(e: PointerEvent, m: Mask) {
    if (!mdrag || mdrag.id !== m.id || !strip) return;
    if (Math.abs(e.clientX - mdrag.x0) > 2) mdrag.moved = true;
    const w = strip.getBoundingClientRect().width || 1;
    const dt = ((e.clientX - mdrag.x0) / w) * viewSpan;
    const c = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));
    if (mdrag.mode === 'move') {
      const len = mdrag.t0 - mdrag.f0;
      m.from = +c(mdrag.f0 + dt, 0, Math.max(0, duration - len)).toFixed(2);
      m.to = +(m.from + len).toFixed(2);
    } else if (mdrag.mode === 'from') {
      m.from = +c(mdrag.f0 + dt, 0, m.to - 0.1).toFixed(2);
    } else {
      m.to = +c(mdrag.t0 + dt, m.from + 0.1, duration).toFixed(2);
    }
  }
  function mup(m: Mask) {
    if (!mdrag || mdrag.id !== m.id) return;
    const moved = mdrag.moved;
    mdrag = null;
    if (moved) onmaskdone?.(m.id);
    else onseek(m.from);             // a click: go to where it starts
  }

  let canvasA: HTMLCanvasElement | null = $state(null);
  let canvasB: HTMLCanvasElement | null = $state(null);
  let boxW = $state(0);
  let strip: HTMLDivElement | null = $state(null);

  /** Mirrored peak bars. The lane you are hearing is drawn bright and the
      other dim, so what you see and what comes out of the speakers can never
      disagree without it being obvious. */
  /**
   * Draw a waveform on the SAME time axis as the pills.
   *
   * `span` is how many seconds of audio the peaks actually cover. It is not
   * always the length of the timeline: the assembled mix runs to the end of
   * the last take, the video runs to its own end, and here they differ by
   * 0.25s. Stretching the peaks across the full width silently rescales them
   * by that ratio — 1.00059 — which is nothing at the start and a quarter of a
   * second by the finish, so the waveform slides gradually out from under the
   * blocks it is supposed to describe.
   */
  function paint(c: HTMLCanvasElement | null, peaks: number[] | null, color: string,
                 span?: number | null) {
    if (!c) return;
    const w = c.clientWidth, h = c.clientHeight;
    if (!w || !h) return;
    const dpr = window.devicePixelRatio || 1;
    c.width = Math.round(w * dpr);
    c.height = Math.round(h * dpr);
    const g = c.getContext('2d');
    if (!g) return;
    g.scale(dpr, dpr);
    g.clearRect(0, 0, w, h);
    if (!peaks?.length) return;
    const mid = h / 2;
    g.fillStyle = color;
    // The strip shows [viewFrom, viewFrom + viewSpan]; the peaks cover `span`
    // seconds of audio from zero. Map one onto the other so the waveform sits
    // under the pills at every zoom level.
    const audio = span && span > 0.5 ? span : duration;
    for (let x = 0; x < w; x++) {
      const t0 = viewFrom + (x / w) * viewSpan;
      const t1 = viewFrom + ((x + 1) / w) * viewSpan;
      if (t1 <= 0 || t0 >= audio) continue;
      // One column may cover several buckets; take the loudest, so a short
      // plosive stays a mark instead of being averaged into the floor.
      const a = Math.max(0, Math.floor((t0 / audio) * peaks.length));
      const b = Math.max(a + 1, Math.min(peaks.length, Math.ceil((t1 / audio) * peaks.length)));
      let v = 0;
      for (let i = a; i < b && i < peaks.length; i++) if (peaks[i] > v) v = peaks[i];
      const bar = Math.max(1, v * (h - 2));
      g.fillRect(x, mid - bar / 2, 1, bar);
    }
  }

  $effect(() => {
    void original; void preview; void track; void boxW;
    paint(canvasA, original, track === 'original' ? 'rgba(226,232,240,0.95)' : 'rgba(100,116,139,0.38)', originalSpan);
    paint(canvasB, preview, track === 'preview' ? 'rgba(129,140,248,0.95)' : 'rgba(99,102,241,0.35)', previewSpan);
  });

  $effect(() => {
    if (!strip) return;
    const ro = new ResizeObserver(([e]) => (boxW = e.contentRect.width));
    ro.observe(strip);
    return () => ro.disconnect();
  });

  /** The reclaimable air after each line: how long, and whether it is held.
      Anything under `gapMin` is a natural beat and is never touched. */
  const gaps = $derived(
    project.segments.map((seg, i) => {
      const next = i < project.segments.length - 1 ? project.segments[i + 1] : null;
      const from = placedEnd(seg);
      const to = next ? placedStart(next) : duration;
      return { seg, from, to, len: to - from };
    }).filter((g) => g.len >= gapMin && g.seg.audio)
  );

  /** One gesture: hear this lane, from this moment. */
  function pick(which: 'original' | 'preview', e: MouseEvent) {
    if (!ok) return;
    track = which;
    const r = (e.currentTarget as HTMLElement).getBoundingClientRect();
    onseek(viewFrom + ((e.clientX - r.left) / r.width) * viewSpan);
  }
</script>

<div class="rounded-lg border border-gray-800 bg-gray-950 p-3">
  <div class="mb-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-gray-500">
    <span class="font-medium text-gray-400">Timeline</span>
    <span class="text-gray-600">click a track to hear it from that point · drag a pill to move its take · shift-click to select blocks · right-click to slice, delete, merge or add a block</span>
    <span class="ml-auto flex items-center gap-2 font-mono">
      {#if loading}<span class="text-gray-600">reading waveform…</span>{/if}
      {playhead.toFixed(1)}s / {ok ? extent.toFixed(0) : '?'}s{#if extent > duration + 0.05}
        <span class="text-amber-400/80" title="the last take finishes after the picture does; the render holds the final frame">
          · {(extent - duration).toFixed(1)}s past the video
        </span>{/if}
    </span>
  </div>

  {#if ok}
    <div class="mb-1 flex items-center gap-2 text-[10px] text-gray-500">
      <span>zoom</span>
      {#each [1, 2, 4, 8, 16] as z}
        <button class="rounded px-1.5 py-0.5 transition
                       {zoom === z ? 'bg-indigo-600 text-white' : 'bg-gray-800 hover:bg-gray-700'}"
                onclick={() => setZoom(z)}>{z}×</button>
      {/each}
      {#if zoom > 1}
        <span class="font-mono text-gray-600">
          showing {fmt(viewFrom)} – {fmt(viewFrom + viewSpan)}
        </span>
        <button class="ml-auto rounded bg-gray-800 px-1.5 py-0.5 hover:bg-gray-700"
                onclick={() => centreOn(playhead)}>go to playhead</button>
        <span class="text-gray-600">scroll to pan</span>
      {:else}
        <span class="ml-auto text-gray-600">
          zoom in to grab short lines — a 3s line is {Math.round((3 / duration) * 100 * 14)}px at 1×
        </span>
      {/if}
    </div>
  {/if}

  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div bind:this={strip} class="relative w-full overflow-hidden rounded bg-gray-900"
       oncontextmenu={(e) => {
         // Right-click is the editing gesture: the moment under the pointer,
         // and a menu of what can be done there.
         if (!ok || !onmenu || !strip) return;
         e.preventDefault();
         const r = strip.getBoundingClientRect();
         const t = viewFrom + ((e.clientX - r.left) / r.width) * viewSpan;
         onmenu(Math.max(0, Math.min(duration, t)), e.clientX, e.clientY);
       }}
       onwheel={(e) => {
         if (zoom === 1) return;
         // Pan rather than scroll the page. Vertical wheel works too, because
         // most mice have no horizontal axis.
         e.preventDefault();
         const d = Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : e.deltaY;
         viewFrom = clampView(viewFrom + (d / 400) * viewSpan);
       }}>
    {#if !ok}
      <div class="flex h-24 items-center justify-center text-[11px] text-gray-600">
        waiting for the video length…
      </div>
    {:else}
      <!-- lane 1: the recording as it came off the screen capture -->
      <!-- svelte-ignore a11y_no_static_element_interactions, a11y_click_events_have_key_events -->
      <div class="block w-full cursor-pointer border-b border-gray-800/80
                  {track === 'original' ? 'bg-gray-800/40' : 'hover:bg-gray-800/20'}"
           onclick={(e) => pick('original', e)}>
        <div class="flex items-center gap-2 px-2 pt-1 text-[10px]">
          <i class="h-2 w-2 rounded-full {track === 'original' ? 'bg-white' : 'bg-gray-600'}"></i>
          <span class={track === 'original' ? 'text-gray-100' : 'text-gray-500'}>your recording</span>
          {#if track === 'original'}
            <span class="ml-auto rounded bg-white/10 px-1.5 text-[9px] text-gray-200">hearing this</span>
          {/if}
        </div>
        <canvas bind:this={canvasA} class="block h-14 w-full"></canvas>
      </div>

      <!-- lane 2: the same timeline with the rendered lines swapped in -->
      {#if preview}
        <!-- svelte-ignore a11y_no_static_element_interactions, a11y_click_events_have_key_events -->
        <div class="block w-full cursor-pointer border-b border-gray-800/80
                    {track === 'preview' ? 'bg-indigo-950/40' : 'hover:bg-gray-800/20'}"
             onclick={(e) => pick('preview', e)}>
          <div class="flex items-center gap-2 px-2 pt-1 text-[10px]">
            <i class="h-2 w-2 rounded-full {track === 'preview' ? 'bg-indigo-400' : 'bg-gray-600'}"></i>
            <span class={track === 'preview' ? 'text-gray-100' : 'text-gray-500'}>
              the mix · {rendered} of {project.segments.length} lines rendered
            </span>
            {#if previewStale}
              <span class="rounded bg-amber-500/20 px-1.5 text-[9px] text-amber-300">behind the script</span>
            {/if}
            {#if track === 'preview'}
              <span class="ml-auto rounded bg-indigo-500/20 px-1.5 text-[9px] text-indigo-200">hearing this</span>
            {/if}
          </div>
          <canvas bind:this={canvasB} class="block h-14 w-full"></canvas>
        </div>
      {:else}
        <div class="flex h-14 items-center justify-center border-b border-gray-800/80 text-[11px] text-gray-600">
          no mix yet — build the preview track to hear your rendered lines here
        </div>
      {/if}

      {#if clashes.length}
        <div class="mb-1 flex items-center gap-2 text-[10px] text-rose-300">
          <span class="rounded bg-rose-950/60 px-1.5 py-0.5">
            {clashes.length} overlap{clashes.length > 1 ? 's' : ''}
          </span>
          <span class="text-gray-500">
            a line needed more room than the one it replaced — drag it left into
            the air before it, or pull its right edge in
          </span>
          <button class="ml-auto rounded border border-rose-800 px-1.5 py-0.5 hover:bg-rose-950/60"
                  onclick={() => {
                    const at = clashes.findIndex((c) => c.s.id === selectedId);
                    const nxt = clashes[(at + 1) % clashes.length];
                    selectedId = nxt.s.id;
                    onseek(placedStart(nxt.s));
                  }}>go to next</button>
        </div>
      {/if}

      <!-- which lines the mix is made of, and where they actually sit -->
      <div class="relative h-6 bg-gray-950/60">
        <!-- A line left out is GONE from here.
             It was drawn greyed out so its old position stayed visible, and
             that was wrong: it sits under the neighbours that have moved into
             its space, catches the pointer meant for them, and makes the very
             rearrangement it was dropped to allow harder to do. Its space is
             the point; the block is not. -->
        {#each project.segments as s, i (s.id)}
          {#if !s.drop}
          {#if isTake(s) && (s.end - s.start) - placedLen(s) > 0.05}
            <!-- what the recording used to fill: the gap is reclaimable air -->
            <div class="pointer-events-none absolute inset-y-1 rounded-[2px] border border-dashed border-gray-700/70"
                 style="left:{pct(s.start)}; width:calc({wide(s.end - s.start)} - 1px)"></div>
          {/if}
          {#if isTake(s) && (s.hold ?? 0) > 0.01}
            <!-- room this line keeps: its own space, not the next line's -->
            <div class="pointer-events-none absolute inset-y-1 rounded-r-[2px] border border-l-0 border-dashed
                        border-current opacity-40 {FILL[lineState(s)]}"
                 style="left:{pct(placedEnd(s))}; width:calc({wide(s.hold ?? 0)} - 1px)"></div>
          {/if}
          <button
            class="absolute inset-y-1 rounded-[2px] border transition-colors {FILL[lineState(s)]}
                   {picked.includes(s.id) ? 'border-amber-300 ring-1 ring-amber-300'
                     : selectedId === s.id ? 'border-white ring-1 ring-white' : 'border-transparent'}
                   {isTake(s) ? 'cursor-ew-resize' : ''}
                   {drag?.id === s.id ? 'z-10 ring-1 ring-indigo-300' : ''}"
            style="left:{pct(placedStart(s))}; width:max(3px, calc({wide(placedLen(s))} - 1px))"
            title={s.drop
              ? `${num(s)}. LEFT OUT — ${s.say.slice(0, 60)}`
              : isTake(s)
                ? `${num(s)}. ${s.say.slice(0, 60)}\n\ndrag to move — ${(s.offset ?? 0) > 0 ? '+' : ''}${(s.offset ?? 0).toFixed(2)}s`
                : `${num(s)}. ${s.say.slice(0, 70)}`}
            onpointerdown={(e) => { e.stopPropagation(); down(e, s); }}
            onpointermove={(e) => move(e, s, i)}
            onpointerup={(e) => { e.stopPropagation(); up(s, e.shiftKey); }}
            onclick={(e) => {
              e.stopPropagation();
              // A block with no take is not draggable, so pointer-up never
              // selected it; handle it as a plain click.
              if (isTake(s)) return;
              if (e.shiftKey && onpick) onpick(s.id, true);
              else { selectedId = s.id; onseek(s.start); onpick?.(s.id, false); }
            }}
            aria-label={s.say.slice(0, 40)}
          ></button>
          {#if over(i) > 0}
            <!-- the part that runs into the next line -->
            <button class="absolute inset-y-1 bg-rose-500/60 hover:bg-rose-400/80"
                    style="left:{pct(placedEnd(s) - over(i))}; width:max(2px, {wide(over(i))})"
                    title="track {num(s)} runs {over(i).toFixed(2)}s into track {num(project.segments[i + 1])}, “{project.segments[i + 1]?.say.slice(0, 40)}” — drag either line, or shorten this one"
                    onclick={(e) => { e.stopPropagation(); selectedId = s.id; }}
                    aria-label="overlap of {over(i).toFixed(2)} seconds"></button>
          {/if}
          {/if}
          {#if isTake(s)}
            <!-- svelte-ignore a11y_no_static_element_interactions -->
            <!-- A short line is a narrow pill: track 25 is 3.02s of a 243s
                 video, so its handle was about 8px of a 15px block and the
                 pointer kept landing on the neighbour instead. Widen the grab
                 target well past the visible rule, and widen it further once
                 the line is selected. -->
            <div class="absolute inset-y-0 cursor-col-resize
                        {selectedId === s.id ? 'w-5' : 'w-3'}
                        after:absolute after:inset-y-1 after:right-0 after:w-0.5
                        after:bg-white/40 hover:after:bg-white
                        {sizing?.id === s.id ? 'z-20 after:bg-white' : 'z-10'}"
                 style="left:calc({pct(blockEnd(s))} - {selectedId === s.id ? 18 : 10}px)"
                 title={`drag to give track ${num(s)} more room (${((s.stretch ?? 1) * 100).toFixed(0)}%)`}
                 onpointerdown={(e) => sizeDown(e, s)}
                 onpointermove={(e) => sizeMove(e, s, i)}
                 onpointerup={() => sizeUp(s)}></div>
          {/if}
        {/each}
      </div>

      <!-- reclaimable air, and the switch that spares it -->
      <div class="relative h-5 border-t border-gray-800/80 bg-gray-950/40">
        {#each gaps as g (g.seg.id)}
          <button
            class="absolute inset-y-0.5 flex items-center justify-center overflow-hidden rounded-[2px]
                   border text-[9px] leading-none transition
                   {g.seg.holdGap
                     ? 'border-emerald-700 bg-emerald-900/50 text-emerald-200'
                     : 'border-dashed border-gray-700 bg-gray-800/40 text-gray-500 hover:border-amber-600 hover:text-amber-300'}"
            style="left:{pct(g.from)}; width:max(6px, calc({wide(g.len)} - 1px))"
            title="{g.len.toFixed(2)}s of air after “{g.seg.say.slice(0, 40)}”
{g.seg.holdGap ? 'HELD — kept at full length' : 'reclaimable — the render will glide through it'}
click to {g.seg.holdGap ? 'release' : 'hold'}"
            onclick={(e) => { e.stopPropagation(); g.seg.holdGap = !g.seg.holdGap; onhold?.(g.seg.id); }}
            aria-label="gap after {g.seg.say.slice(0, 30)}"
          >{g.len >= 1.4 ? (g.seg.holdGap ? 'hold' : `${g.len.toFixed(1)}s`) : ''}</button>
        {/each}
      </div>

      <!-- Blur masks: when each part of the picture is hidden. -->
      {#if (project.masks ?? []).length}
        <!-- Each bar is its span: drag it to shift it, drag an end to
             lengthen or shorten it, click it to go to it. -->
        <div class="relative h-5 border-t border-gray-800/80 bg-gray-950/40">
          {#each project.masks ?? [] as m (m.id)}
            <!-- svelte-ignore a11y_no_static_element_interactions, a11y_click_events_have_key_events -->
            <div class="absolute inset-y-0.5 cursor-grab overflow-hidden rounded-[2px] border px-1.5 text-[9px] leading-[14px]
                        {selectedMask === m.id ? 'border-violet-300 bg-violet-700/70 text-white'
                                               : 'border-violet-700/80 bg-violet-900/50 text-violet-200 hover:bg-violet-800/60'}"
                 style="left:{pct(m.from)}; width:max(10px, calc({wide(m.to - m.from)} - 1px))"
                 title="blur mask {m.id.slice(1)}: {fmt(m.from)}–{fmt(m.to)} — drag to shift, drag an end to change its length"
                 onpointerdown={(e) => mdown(e, m, 'move')} onpointermove={(e) => mmove(e, m)} onpointerup={() => mup(m)}
                 onclick={(e) => e.stopPropagation()}>
              <span class="absolute inset-y-0 left-0 w-1.5 cursor-ew-resize bg-violet-300/60"
                    onpointerdown={(e) => mdown(e, m, 'from')} onpointermove={(e) => mmove(e, m)} onpointerup={() => mup(m)}></span>
              blur {m.id.slice(1)}
              <span class="absolute inset-y-0 right-0 w-1.5 cursor-ew-resize bg-violet-300/60"
                    onpointerdown={(e) => mdown(e, m, 'to')} onpointermove={(e) => mmove(e, m)} onpointerup={() => mup(m)}></span>
            </div>
          {/each}
        </div>
      {/if}

      <!-- Where one recording ends and the next begins, for a video joined
           from several: the stop-and-start is usually worth a look. -->
      {#each (project.parts ?? []).slice(1) as part, i (part.start)}
        <div class="pointer-events-none absolute top-0 h-full border-l-2 border-sky-400/70"
             style="left:{pct(part.start)}">
          <span class="absolute bottom-0.5 left-1 whitespace-nowrap rounded bg-sky-950/90 px-1 text-[9px] text-sky-200"
                title={part.name}>recording {i + 2}</span>
        </div>
      {/each}
      <!-- Deleted sections, across every lane: gone from the finished video. -->
      {#each project.cuts ?? [] as c (c.from)}
        <div class="pointer-events-none absolute top-0 h-full border-x border-red-900/70"
             style="left:{pct(c.from)}; width:{wide(c.to - c.from)};
                    background: repeating-linear-gradient(135deg, rgba(127,29,29,.45) 0 6px, rgba(17,24,39,.55) 6px 12px);">
          <span class="absolute left-1 top-1 rounded bg-red-950/90 px-1 text-[9px] text-red-200">
            cut {(c.to - c.from).toFixed(1)}s
          </span>
        </div>
      {/each}
      <!-- Slices: edit points that bound what can be deleted. -->
      {#each project.slices ?? [] as x (x)}
        <div class="pointer-events-none absolute top-0 h-full border-l border-dashed border-amber-400/90"
             style="left:{pct(x)}">
          <span class="absolute -left-1.5 -top-0.5 text-[10px] leading-none text-amber-300">✂</span>
        </div>
      {/each}
      <div class="pointer-events-none absolute top-0 h-full w-px bg-white/90" style="left:{pct(playhead)}"></div>
    {/if}
  </div>

  <div class="mt-2 flex flex-wrap items-center gap-x-3 text-[10px] text-gray-500">
    <span>lines:</span>
    <span class="flex items-center gap-1"><i class="inline-block h-2 w-2 rounded-sm bg-emerald-600"></i>your voice</span>
    <span class="flex items-center gap-1"><i class="inline-block h-2 w-2 rounded-sm bg-indigo-600"></i>rendered</span>
    <span class="flex items-center gap-1"><i class="inline-block h-2 w-2 rounded-sm bg-amber-600"></i>needs re-render</span>
    <span class="flex items-center gap-1"><i class="inline-block h-2 w-2 rounded-sm bg-gray-700"></i>not rendered</span>
    {#if project.segments.some((x) => x.drop)}
      <span class="flex items-center gap-1 text-gray-600">
        <i class="inline-block h-2 w-2 rounded-sm bg-gray-800/40"></i>left out
      </span>
    {/if}
    <span class="flex items-center gap-1">
      <i class="inline-block h-2 w-3 rounded-sm border border-dashed border-gray-600"></i>
      what the recording filled — the gap is reclaimable air
    </span>
    <span class="text-gray-600">drag a pill to move it · drag its right edge to give it more room</span>
    <span class="flex items-center gap-1">
      <i class="inline-block h-2 w-3 rounded-sm bg-emerald-800"></i>
      held: kept at full length. Click any gap to hold or release it.
    </span>
  </div>
</div>
