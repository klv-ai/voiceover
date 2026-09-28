<script lang="ts">
  import { Button, Input, Label, Range, Select, Alert, Spinner, Toggle } from 'flowbite-svelte';
  import { onMount, tick, untrack } from 'svelte';
  import { wordDiff } from '$lib/diff';
  import Timeline from '$lib/components/Timeline.svelte';
  import LineDetail from '$lib/components/LineDetail.svelte';
  import { signature,
           blockLabel, blockSpan, freeGapAt, ADD_MARGIN, ADD_MIN,
           blockAt, cutAt, sectionAt, cutCheck, afterCuts, SLICE_NEAR, mergeCheck,
           masksAt, MASK_PRESETS, type Mask,
           type Project, type Segment } from '$lib/types';

  let { data } = $props();


  // Snapshot once, deliberately: everything below is edited in place, so
  // re-deriving from `data` would discard the operator's work.
  let p: Project = $state(untrack(() => structuredClone(data.project)));

  /** Every voice that can be cloned from, newest first, plus an escape hatch.
   *
   * A project's own pooled read comes back from the same list as a built
   * voice, so "my own read" is not a special case here — it is just another
   * voice whose id starts with `project:`. */
  const voices = $derived((data.voices ?? []).filter((v: any) => v.reference));
  const voiceItems = $derived([
    ...voices.map((v: any) => ({
      value: v.reference,
      name: v.stats?.seconds
        ? `${v.name} — ${Math.round(v.stats.seconds)}s`
        : v.name
    })),
    { value: 'custom', name: 'A file I will name myself…' }
  ]);
  /** `custom` whenever the stored path is not one of the listed voices, so a
      hand-set path is never silently replaced by the first voice in the list. */
  const voicePick = $derived(
    voices.some((v: any) => v.reference === p.settings.voiceRef)
      ? p.settings.voiceRef
      : 'custom'
  );

  /** What is actually in force, so the panel can stop offering what is not.
   *
   * Five controls accumulated here - a voice, a fine-tune, a reference wav,
   * "use my clean read", and a channel match - describing overlapping ways of
   * deciding who speaks. Any two of them can contradict, and the operator
   * cannot tell which won until a thirty-minute render is over. */
  const bare = $derived(!!p.settings.noAssist);
  const chosenVoice = $derived(
    (data.voices ?? []).find((v: any) => v.id === p.settings.voice) ?? null);

  function pickVoice(e: Event) {
    const v = (e.target as HTMLSelectElement).value;
    if (v === 'custom') return;          // keep what is there; let them type
    p.settings.voiceRef = v;
    save();
  }

  /** The settings as last seen from the server. Saving sends only what has
      moved since, so this page can never revert a value it never touched. */
  /** Segment fields as last seen from the server, for the same reason: a save
      must only carry what THIS page changed. Sending every field let a tab
      that loaded before a script revamp write its older wording back. */
  let segsSeen: Record<string, any> = $state(
    untrack(() => Object.fromEntries(
      (data.project.segments as any[]).map((s) => [s.id, JSON.stringify(s)])))
  );

  let settingsSeen: Record<string, unknown> = $state(
    untrack(() => ({ ...(data.project.settings as any) }))
  );

  let busy = $state('');
  let error = $state('');
  /** Everything outside the four steps, folded away until asked for. */
  let advanced = $state(false);
  let settingsOpen = $state(false);
  let settingsEl: HTMLDetailsElement | undefined = $state();

  /** The block number the timeline shows — the one the operator talks in. */
  const blockNo = (s: Segment) => blockLabel(s);

  // ---- Polish: suggestions, never edits -----------------------------------
  let polish: any = $state({ suggestions: [], checks: {}, statuses: {} });
  let polishOnly = $state(false);
  const squash = (t?: string) => (t ?? '').replace(/\s+/g, ' ').trim();
  /** The open suggestion for a block, until it is USED or KEPT-MINE — not until
      the line is edited. Hiding it on the first keystroke lost the operator's
      place: the suggestion went, then the filter took the whole block with it.
      An edited-since suggestion stays, marked, so taking it is a choice made
      knowing it replaces the edit. */
  function sugFor(s: Segment): any {
    if (polish.statuses?.[s.id]) return null;
    return (polish.suggestions ?? []).find((x: any) => x.id === s.id) ?? null;
  }
  const editedSince = (s: Segment, g: any) => squash(g.against) !== squash(s.say);
  /** Checker notes, pinned to the text they were made about: fix the line and
      the note goes. Not for a line that is not spoken. */
  function notesFor(s: Segment): string[] {
    if (s.drop || s.locked) return [];
    const c = polish.checks?.[s.id];
    return c && squash(c.text) === squash(s.say) ? c.notes : [];
  }
  const polishRan = $derived(!!((polish.suggestions ?? []).length || Object.keys(polish.checks ?? {}).length));
  /** Every block the pass had something to say about, FIXED when it ran.
      The filtered view shows exactly these, so a block never disappears from
      under the operator while he is working on it. */
  const reviewSet = $derived(new Set<string>([
    ...(polish.suggestions ?? []).map((x: any) => x.id),
    ...Object.keys(polish.checks ?? {})
  ]));
  const polishOpen = $derived(p.segments.filter((s) => !s.locked && !s.drop && sugFor(s)));
  /** Safe to take in bulk: nobody has edited the block, then or since. */
  const polishDrafts = $derived(polishOpen.filter((s) => {
    const g = sugFor(s);
    return g && !g.edited && g.kind !== 'drop' && !editedSince(s, g);
  }));
  const polishNoted = $derived(p.segments.filter((s) => notesFor(s).length));

  onMount(async () => {
    try {
      const d = await (await fetch(`/api/projects/${p.id}/polish`)).json();
      if (!d.error) polish = d;
    } catch { /* no polish yet is fine */ }
  });

  async function runPolish() {
    await save();
    const d = await call(`/api/projects/${p.id}/polish`, { method: 'POST' }, 'polish');
    if (d && !d.error) { polish = d; view = 'script'; polishOnly = true; }
  }

  async function markPolish(ids: string[], status: 'accepted' | 'dismissed') {
    const d = await call(`/api/projects/${p.id}/polish`, {
      method: 'PATCH', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ ids, status })
    });
    if (d && !d.error) polish = d;
  }

  async function takeSuggestion(s: Segment, g: any) {
    if (g.kind === 'drop') s.drop = true;
    else { s.sayBeforePolish = s.say; s.say = g.text; }
    await save();
    await markPolish([s.id], 'accepted');
  }

  /** Every suggestion on a block the operator has NOT edited. Never his own
      edits in bulk, and never a drop — those are taken one at a time. */
  async function takeAllDrafts() {
    const take = polishDrafts.map((s) => [s, sugFor(s)] as const);
    for (const [s, g] of take) { s.sayBeforePolish = s.say; s.say = g.text; }
    await save();
    await markPolish(take.map(([s]) => s.id), 'accepted');
  }

  function undoPolish(s: Segment) {
    if (!s.sayBeforePolish) return;
    s.say = s.sayBeforePolish;
    s.sayBeforePolish = '';
    save();
  }
  let selectedId = $state(untrack(() => p.segments[0]?.id ?? ''));
  /** "s031" -> "31". The number every part of the UI names a line by, so a
      flaw heard in the mix can be found in the script, the timeline and the
      recorder without quoting its words back. */
  const num = (s: { id: string }) => blockLabel(s);
  let videoEl: HTMLVideoElement | null = $state(null);
  let audioEl: HTMLAudioElement | null = $state(null);
  let playhead = $state(0);
  let duration = $state(untrack(() => data.project.duration ?? 0));
  let track: 'original' | 'preview' = $state('original');
  let previewSrc = $state('');
  // What the assembled track was built from. A rendered line lives in the
  // project immediately, but the preview is a separate file — without this it
  // silently plays the mix from before your change, which is the same trap the
  // per-line Play button had.
  let previewBuiltFrom = $state('');

  // Peak magnitudes for the strip under the video. Fetched per track and
  // re-fetched whenever the file behind it is rebuilt — a stale waveform would
  // draw the mix from before your change, the same trap Play once had.
  let originalPeaks: number[] | null = $state(null);
  let previewPeaks: number[] | null = $state(null);
  let peaksLoading = $state(false);
  let originalSpan = $state<number | null>(null);
  let previewSpan = $state<number | null>(null);

  /** Read the whole script in one pass, or tune one line against the picture. */
  let view: 'script' | 'tune' = $state('tune');

  type RenderState = {
    status: 'idle' | 'running' | 'done' | 'error' | 'stopped';
    kind?: 'render' | 'synth-all';
    step?: string; log?: string[]; error?: string; output?: string; seconds?: number;
    done?: number; total?: number;
  };
  let job: RenderState = $state({ status: 'idle' });
  let pollTimer: ReturnType<typeof setTimeout> | null = null;

  const media = (path?: string) => (path ? `/api/media?path=${encodeURIComponent(path)}` : '');
  const selected = $derived(p.segments.find((s) => s.id === selectedId));
  const stale = (s: Segment) => !!s.audio && s.renderedFrom !== signature(s, p.settings);
  /** Everything the assembled track depends on. */
  const mixSignature = $derived(
    JSON.stringify([p.settings.cleanBackground !== false, p.segments.map((s) =>
      [s.id, s.locked ? 'real' : (s.audio ?? ''), '', s.renderedFrom ?? '',
       s.offset ?? 0, s.stretch ?? 1, !!s.drop])])
  );
  const previewStale = $derived(!!previewSrc && previewBuiltFrom !== mixSignature);
  /** A muxed preview is a video and plays in the main element; an older
      audio-only one still needs the companion <audio>. */
  const previewIsVideo = $derived(previewSrc.split('&')[0].endsWith('.mp4')
                                  || /%2Fpreview\.\d+\.mp4/.test(previewSrc));

  /** How far the selected take can move before it would touch its neighbours.
      The assembler clamps too; this only keeps the slider honest. */
  const nudge = $derived.by(() => {
    const i = p.segments.findIndex((s) => s.id === selectedId);
    if (i < 0) return { min: -2, max: 2 };
    const s = p.segments[i];
    // Where the neighbours ACTUALLY sit, and all the room there actually is.
    //
    // This measured against the original spans, ignoring every offset already
    // applied, and then clamped the answer to plus or minus two seconds. On a
    // line with six seconds of clear air after it the slider simply refused to
    // go past two, with nothing on screen to say why. The timeline's own drag
    // has always used placed positions; this is the same arithmetic.
    const lenOf = (x: Segment) => (x.duration ?? (x.end - x.start)) * (x.stretch ?? 1);
    const prev = i > 0 ? p.segments[i - 1] : null;
    const next = i < p.segments.length - 1 ? p.segments[i + 1] : null;
    const prevEnd = prev ? prev.start + (prev.offset ?? 0) + lenOf(prev) + (prev.hold ?? 0) : 0;
    const nextStart = next ? next.start + (next.offset ?? 0) : (p.duration ?? s.end);
    const len = lenOf(s) + (s.hold ?? 0);
    return {
      min: +(prevEnd - s.start + 0.02).toFixed(2),
      max: +(nextStart - (s.start + len) - 0.02).toFixed(2)
    };
  });

  const counts = $derived({
    total: p.segments.length,
    done: p.segments.filter((s) => s.audio && !s.locked && !stale(s)).length,
    real: p.segments.filter((s) => s.locked).length,
    stale: p.segments.filter((s) => stale(s) && !s.locked).length
  });

  /** Lines whose take was transcribed and did not say the line.
      Not the same as a missing take: these render, they play, and they are
      wrong — which is why they were only ever found by listening to all of
      them. */
  const suspect = $derived(
    p.segments.filter((s) => !s.drop && !s.locked && s.check
                        && s.check.score !== null && !s.check.ok)
  );
  /** Rendered, but never actually checked, because the transcriber was
      unavailable at the time. Silence from the checker reads exactly like a
      pass, so it is worth saying out loud. */
  const unchecked = $derived(
    p.segments.filter((s) => !s.drop && !s.locked && s.audio
                        && (!s.check || s.check.score === null))
  );

  async function call(url: string, init?: RequestInit, label = '') {
    busy = label; error = '';
    try {
      const r = await fetch(url, init);
      const d = await r.json();
      if (d.error) { error = d.error; return null; }
      return d;
    } catch (e: any) { error = e.message; return null; }
    finally { busy = ''; }
  }

  const step = async (path: string, label: string) => {
    const d = await call(`/api/projects/${p.id}/${path}`, { method: 'POST' }, label);
    if (d && d.id) { p = d; selectedId ||= p.segments[0]?.id ?? ''; }
  };

  /** Step 1a. Transcribe and split into lines — only ever for a project with
      no script. Splitting REPLACES every line, so on a project that already
      has one this would throw away the rewrite; re-splitting lives under
      Advanced where it has to be asked for. */
  async function transcribe() {
    if (p.segments.length) return;
    await step('analyze', 'analyze');
    if (!error && p.analysis) await step('script', 'script');
  }

  /** Step 1b. Rewrite, then read it whole for flow. The rewrite leaves any line
      edited by hand alone; the flow pass keeps each line's previous wording
      so it can be put back on its own. */
  async function rewrite() {
    await step('clean', 'clean');
    if (!error) await flowPass();
  }

  /** Step 2 is the voice, which lives in the settings panel. */
  async function openVoice() {
    settingsOpen = true;
    await tick();
    settingsEl?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  let flowChanged: string[] = $state([]);
  /** Edit the whole script for flow. Destructive to the wording, so each line
      keeps what it said before and can be put back on its own. */
  async function flowPass() {
    await save();
    const d = await call(`/api/projects/${p.id}/flow`, { method: 'POST' }, 'flow');
    if (d?.project) {
      p = d.project;
      flowChanged = d.changed ?? [];
      selectedId ||= p.segments[0]?.id ?? '';
    }
  }

  /** Only the settings this page has actually moved. */
  function changedSettings() {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(p.settings as any)) {
      if (JSON.stringify(v) !== JSON.stringify((settingsSeen as any)[k])) out[k] = v;
    }
    return out;
  }

  /** Only the segments this page has actually edited, and only their editable
      fields. */
  function changedSegments() {
    const EDITABLE = ['say', 'sayOriginal', 'sayBeforeFlow', 'sayBeforePolish', 'cleaned', 'locked',
                      'drop', 'holdGap', 'offset', 'stretch', 'hold', 'seed', 'over', 'direction'];
    const out: any[] = [];
    for (const s of p.segments) {
      const before = segsSeen[s.id];
      if (before === JSON.stringify(s)) continue;      // untouched
      const patch: any = { id: s.id };
      let any = false;
      const was = before ? JSON.parse(before) : {};
      for (const k of EDITABLE) {
        if (JSON.stringify((s as any)[k]) !== JSON.stringify(was[k])) {
          patch[k] = (s as any)[k];
          any = true;
        }
      }
      if (any) out.push(patch);
    }
    return out;
  }

  /**
   * ONE SAVE AT A TIME.
   *
   * What is sent is a DIFF against `segsSeen`, and `segsSeen` only catches up
   * when a save returns. Two overlapping saves therefore both diff against the
   * same stale baseline, and the second re-sends the values the first had
   * already replaced — a line reverts to what it said a moment ago, or another
   * line's earlier text is written back over it. With the tune box now saving
   * a second after you stop typing, overlapping saves are the normal case, not
   * a rare one.
   *
   * So they queue. A save asked for while one is in flight does not compute
   * anything; it sets a flag, and the running save goes round again afterwards
   * with a baseline that is actually current.
   */
  let saving: Promise<void> | null = null;
  let saveAgain = false;

  async function save(): Promise<void> {
    if (saving) { saveAgain = true; return saving; }
    saving = (async () => {
      try {
        do {
          saveAgain = false;
          const saved = await call(`/api/projects/${p.id}`, {
            method: 'PATCH', headers: { 'content-type': 'application/json' },
            body: JSON.stringify({ settings: changedSettings(), segments: changedSegments() })
          }, 'saving');
          if (saved?.id) {
            p.rev = saved.rev;
            settingsSeen = { ...(saved.settings as any) };
            segsSeen = Object.fromEntries((saved.segments as any[]).map((x) => [x.id, JSON.stringify(x)]));
          }
        } while (saveAgain);
      } finally {
        saving = null;
      }
    })();
    return saving;
  }

  // ---- Add a block where the recording has only dead air -----------------
  let newSay = $state('');
  const clock = (t: number) => `${Math.floor(t / 60)}:${(t % 60).toFixed(1).padStart(4, '0')}`;

  async function addBlock(at = playhead) {
    if (!newSay.trim()) return;
    await save();
    const d = await call(`/api/projects/${p.id}/segment`, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ at, say: newSay.trim() })
    }, 'add');
    if (!d?.project) return;
    p = d.project;
    newSay = '';
    selectedId = d.id;
    const seg = p.segments.find((x) => x.id === d.id);
    if (seg) await synth(seg);
  }

  async function removeBlock(seg: Segment) {
    const i = p.segments.findIndex((x) => x.id === seg.id);
    const d = await call(`/api/projects/${p.id}/segment?sid=${encodeURIComponent(seg.id)}`,
                         { method: 'DELETE' }, 'remove');
    if (!d?.project) return;
    p = d.project;
    selectedId = p.segments[Math.max(0, i - 1)]?.id ?? '';
    if (previewSrc) await buildPreview();
  }

  // ---- Editing the timeline: a menu where the view line is ---------------
  let menu = $state<{ t: number; x: number; y: number } | null>(null);
  let menuSay = $state('');
  let menuAdding = $state(false);

  /** Right-click: the view line moves to the moment, and the menu opens there. */
  function openMenu(t: number, x: number, y: number) {
    // Editing pauses playback: a moving view line would put a new block or a
    // slice somewhere other than where the right-click was.
    videoEl?.pause();
    audioEl?.pause();
    seek(t);
    menu = { t, x: Math.min(x, window.innerWidth - 310), y: Math.min(y, window.innerHeight - 280) };
    menuSay = '';
    menuAdding = false;
  }

  /** Everything the menu needs to know about the moment it was opened on. */
  const menuInfo = $derived.by(() => {
    if (!menu) return null;
    const t = menu.t;
    const section = sectionAt(p, t, duration);
    const whole = section[0] <= 0.001 && section[1] >= duration - 0.001;
    const gap = freeGapAt(p, t, duration);
    return {
      block: blockAt(p, t),
      cut: cutAt(p, t),
      gap,
      room: gap ? gap[1] - gap[0] - 2 * ADD_MARGIN : 0,
      section, whole,
      check: whole ? null : cutCheck(p, section[0], section[1]),
      nearSlice: (p.slices ?? []).find((x) => Math.abs(x - t) <= SLICE_NEAR) ?? null
    };
  });

  // ---- Blur masks ----------------------------------------------------------
  /** Where the picture actually sits inside the video element: it is scaled
      to fit and letterboxed, and a mask is placed on the PICTURE. */
  let videoBox = $state({ left: 0, top: 0, width: 0, height: 0 });
  function measureVideo() {
    if (!videoEl) return;
    const cw = videoEl.clientWidth, ch = videoEl.clientHeight;
    const vw = videoEl.videoWidth || 16, vh = videoEl.videoHeight || 9;
    const k = Math.min(cw / vw, ch / vh);
    const w = vw * k, h = vh * k;
    videoBox = { left: videoEl.offsetLeft + (cw - w) / 2, top: videoEl.offsetTop + (ch - h) / 2, width: w, height: h };
  }
  $effect(() => {
    if (!videoEl) return;
    const ro = new ResizeObserver(measureVideo);
    ro.observe(videoEl);
    return () => ro.disconnect();
  });

  /** Editing: masks can be dragged and resized over the picture. Otherwise
      they only show the blur, and let clicks through to the player. */
  let maskEdit = $state(false);
  /** The mask being worked on. It stays on the picture while editing even
      when the playhead is outside its span — faded — so it can always be
      grabbed; a mask that only exists while it is on could not be. */
  let selectedMask = $state('');
  const masksNow = $derived.by(() => {
    const on = masksAt(p, playhead);
    const sel = maskEdit ? (p.masks ?? []).find((m) => m.id === selectedMask) : undefined;
    return sel && !on.includes(sel) ? [...on, sel] : on;
  });
  const maskOn = (m: Mask) => playhead >= m.from && playhead <= m.to;

  /** "1:23.4", "83.4" or "83" — as seconds. */
  function parseClock(v: string): number | null {
    const t = v.trim();
    const m = /^(\d+):(\d{1,2}(?:\.\d+)?)$/.exec(t);
    if (m) return Number(m[1]) * 60 + Number(m[2]);
    const n = Number(t);
    return Number.isFinite(n) ? n : null;
  }
  async function maskTime(id: string, which: 'from' | 'to', v: string) {
    const t = parseClock(v);
    if (t === null) return;
    await maskSet(id, { [which]: t });
  }
  /** Another mask in the same place, starting where this one ends. */
  async function maskContinue(m: Mask) {
    await save();
    const d = await maskCall({ op: 'mask-add', at: m.to, x: m.x, y: m.y, w: m.w, h: m.h });
    if (d?.id) { selectedMask = d.id; seek(m.to); }
  }
  function maskGo(m: Mask) {
    selectedMask = m.id;
    seek(m.from + 0.05);
  }
  async function maskTimeDone(id: string) {
    const m = (p.masks ?? []).find((x) => x.id === id);
    if (m) await maskSet(id, { from: m.from, to: m.to });
  }
  /** 'move', or which edge/corner is being pulled. */
  type MaskGrip = 'move' | 'n' | 's' | 'e' | 'w' | 'ne' | 'nw' | 'se' | 'sw';
  let maskDrag: { id: string; mode: MaskGrip; x0: number; y0: number; m0: Mask } | null = null;
  const GRIPS: { mode: MaskGrip; at: string; cursor: string }[] = [
    { mode: 'nw', at: 'left:-7px; top:-7px', cursor: 'nwse-resize' },
    { mode: 'n', at: 'left:calc(50% - 7px); top:-7px', cursor: 'ns-resize' },
    { mode: 'ne', at: 'right:-7px; top:-7px', cursor: 'nesw-resize' },
    { mode: 'e', at: 'right:-7px; top:calc(50% - 7px)', cursor: 'ew-resize' },
    { mode: 'se', at: 'right:-7px; bottom:-7px', cursor: 'nwse-resize' },
    { mode: 's', at: 'left:calc(50% - 7px); bottom:-7px', cursor: 'ns-resize' },
    { mode: 'sw', at: 'left:-7px; bottom:-7px', cursor: 'nesw-resize' },
    { mode: 'w', at: 'left:-7px; top:calc(50% - 7px)', cursor: 'ew-resize' }
  ];

  /** Drawing a new mask: where the drag began and where it is now, as
      fractions of the picture. */
  let draw = $state<{ x0: number; y0: number; x1: number; y1: number } | null>(null);
  let maskLayer: HTMLDivElement | undefined = $state();
  const frac = (e: PointerEvent) => {
    const r = maskLayer!.getBoundingClientRect();
    return { x: Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)),
             y: Math.min(1, Math.max(0, (e.clientY - r.top) / r.height)) };
  };
  function drawDown(e: PointerEvent) {
    if (!maskEdit || e.target !== maskLayer) return;   // on a mask: that mask handles it
    e.preventDefault();
    try { maskLayer!.setPointerCapture(e.pointerId); } catch { /* ignore */ }
    const f = frac(e);
    draw = { x0: f.x, y0: f.y, x1: f.x, y1: f.y };
  }
  function drawMove(e: PointerEvent) {
    if (!draw) return;
    const f = frac(e);
    draw = { ...draw, x1: f.x, y1: f.y };
  }
  async function drawUp() {
    if (!draw) return;
    const d = draw;
    draw = null;
    const x = Math.min(d.x0, d.x1), y = Math.min(d.y0, d.y1);
    const w = Math.abs(d.x1 - d.x0), h = Math.abs(d.y1 - d.y0);
    if (w < 0.02 || h < 0.02) return;                  // a click, not a drag
    await save();
    const r = await maskCall({ op: 'mask-add', at: playhead, x, y, w, h });
    if (r?.id) selectedMask = r.id;
  }
  function toggleMaskEdit() {
    maskEdit = !maskEdit;
    // Drawing on a moving picture puts the mask on the wrong frames.
    if (maskEdit) { videoEl?.pause(); audioEl?.pause(); }
  }

  async function maskCall(body: Record<string, unknown>) {
    const d = await call(`/api/projects/${p.id}/timeline`, {
      method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body)
    }, 'mask');
    if (d?.project) p = d.project;
    return d;
  }
  async function maskAdd(preset: string) {
    if (!menu) return;
    const at = menu.t;
    menu = null;
    await save();
    const d = await maskCall({ op: 'mask-add', at, preset });
    if (d?.id) maskEdit = true;
  }
  async function maskSet(id: string, patch: Partial<Mask>) {
    menu = null;
    await maskCall({ op: 'mask-update', id, ...patch });
  }
  async function maskRemove(id: string) {
    menu = null;
    await maskCall({ op: 'mask-remove', id });
  }
  function maskDown(e: PointerEvent, m: Mask, mode: MaskGrip) {
    if (!maskEdit) return;
    e.stopPropagation();
    e.preventDefault();
    try { (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId); } catch { /* ignore */ }
    selectedMask = m.id;
    maskDrag = { id: m.id, mode, x0: e.clientX, y0: e.clientY, m0: { ...m } };
  }
  function maskMove(e: PointerEvent) {
    if (!maskDrag || !videoBox.width) return;
    const m = (p.masks ?? []).find((x) => x.id === maskDrag!.id);
    if (!m) return;
    const dx = (e.clientX - maskDrag.x0) / videoBox.width, dy = (e.clientY - maskDrag.y0) / videoBox.height;
    const c = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));
    const o = maskDrag.m0, g = maskDrag.mode;
    if (g === 'move') { m.x = c(o.x + dx, 0, 1 - o.w); m.y = c(o.y + dy, 0, 1 - o.h); return; }
    // Each edge moves on its own; the opposite edge stays where it was.
    if (g.includes('w')) { const x = c(o.x + dx, 0, o.x + o.w - 0.02); m.w = o.x + o.w - x; m.x = x; }
    if (g.includes('e')) m.w = c(o.w + dx, 0.02, 1 - o.x);
    if (g.includes('n')) { const y = c(o.y + dy, 0, o.y + o.h - 0.02); m.h = o.y + o.h - y; m.y = y; }
    if (g.includes('s')) m.h = c(o.h + dy, 0.02, 1 - o.y);
  }
  async function maskUp() {
    if (!maskDrag) return;
    const m = (p.masks ?? []).find((x) => x.id === maskDrag!.id);
    maskDrag = null;
    if (m) await maskCall({ op: 'mask-update', id: m.id, x: m.x, y: m.y, w: m.w, h: m.h });
  }

  // ---- Picking a run of blocks, to merge them -----------------------------
  /** Blocks picked with shift-click: the run from the selected block to the
      one shift-clicked, inclusive. A plain click clears it. */
  let picked = $state<string[]>([]);
  function onpick(id: string, shift: boolean) {
    if (!shift || !selectedId || selectedId === id) { picked = []; return; }
    const a = p.segments.findIndex((x) => x.id === selectedId);
    const b = p.segments.findIndex((x) => x.id === id);
    if (a < 0 || b < 0) return;
    picked = p.segments.slice(Math.min(a, b), Math.max(a, b) + 1).map((x) => x.id);
  }
  const pickedLabel = $derived(picked.length
    ? `${blockLabel({ id: picked[0] })}–${blockLabel({ id: picked[picked.length - 1] })}` : '');
  const pickedCheck = $derived(picked.length >= 2 ? mergeCheck(p, picked) : null);

  async function mergePicked() {
    if (!pickedCheck || 'error' in pickedCheck) return;
    menu = null;
    await save();
    const d = await call(`/api/projects/${p.id}/timeline`, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ op: 'merge', ids: picked })
    }, 'merge');
    if (d?.project) { p = d.project; selectedId = d.id; picked = []; }
  }

  async function splitBack(id: string) {
    menu = null;
    await save();
    const d = await call(`/api/projects/${p.id}/timeline`, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ op: 'unmerge', id })
    }, 'merge');
    if (d?.project) { p = d.project; selectedId = d.ids?.[0] ?? selectedId; picked = []; }
    if (previewSrc) await buildPreview();
  }

  async function timelineOp(op: 'slice' | 'unslice' | 'cut' | 'restore') {
    if (!menu || !menuInfo) return;
    const at = op === 'unslice' && menuInfo.nearSlice != null ? menuInfo.nearSlice : menu.t;
    menu = null;
    await save();
    const d = await call(`/api/projects/${p.id}/timeline`, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ op, at })
    }, 'timeline');
    if (d?.project) p = d.project;
  }

  async function menuAddBlock() {
    if (!menu || !menuSay.trim()) return;
    const at = menu.t;
    newSay = menuSay;
    menu = null;
    await addBlock(at);
  }

  let untrimNote = $state('');
  /** Undo the end trim on this block's take, from its untouched original. */
  async function useWholeTake(seg: Segment) {
    untrimNote = '';
    await save();
    const d = await call(`/api/projects/${p.id}/segment/${seg.id}/untrim`, { method: 'POST' }, `untrim:${seg.id}`);
    if (!d) return;
    if (d.segment) p.segments[p.segments.findIndex((x) => x.id === seg.id)] = d.segment;
    untrimNote = d.restored ? `put back ${d.restored.toFixed(2)}s at the end of block ${blockLabel(seg)}` : (d.note ?? '');
    if (previewSrc && d.restored) await buildPreview();
  }

  /** How far an added block's take runs past the space it was given — the
      next block then starts that much late, as after any long take. */
  function overrun(seg: Segment): number {
    if (!seg.audio || seg.drop) return 0;
    const i = p.segments.findIndex((x) => x.id === seg.id);
    const next = p.segments.slice(i + 1).find((x) => !x.drop);
    const end = blockSpan(seg, p.settings)[1];
    const limit = next ? blockSpan(next, p.settings)[0] : duration;
    return Math.max(0, end - limit);
  }

  async function synth(seg: Segment) {
    await save();
    const d = await call(`/api/projects/${p.id}/segment/${seg.id}/synth`, { method: 'POST' }, `synth:${seg.id}`);
    if (d?.segment) p.segments[p.segments.findIndex((s) => s.id === seg.id)] = d.segment;
    // Fold it straight into the assembled track. Rebuilding costs about a
    // second, so there is no reason to make the operator ask for it.
    if (previewSrc) await buildPreview();
  }

  /** Rebuild the whole track: your recording with rendered lines swapped in. */
  async function buildPreview() {
    await save();
    const sig = mixSignature;
    const d = await call(`/api/projects/${p.id}/preview`, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ sig })
    }, 'preview');
    if (d?.path) {
      const at = audioEl?.currentTime ?? 0;
      previewSrc = `${media(d.path)}&v=${d.ts}`;   // bust the cache on rebuild
      previewBuiltFrom = sig;
      track = 'preview';
      loadPeaks('preview');
      // keep the listener where they were
      queueMicrotask(() => { if (audioEl) audioEl.currentTime = at; });
    }
  }

  /** Play the assembled track from just before a line, so it is judged against
      its neighbours rather than on its own. */
  async function hearInContext(seg: Segment) {
    if (!previewSrc || previewStale) await buildPreview();
    track = 'preview';
    const at = Math.max(0, seg.start - 1.5);
    seek(at);
    await videoEl?.play();
  }

  let linePlaying = $state('');
  let lineAudio: HTMLAudioElement | null = null;

  /** Hear one line's take on its own. Deliberately independent of the
      timeline: it pauses the video first so the two never compete for the
      speakers, and it does not move the playhead. */
  function stopLine() {
    lineAudio?.pause();
    lineAudio = null;
    linePlaying = '';
  }

  async function playLine(s: Segment) {
    const again = linePlaying === s.id;
    stopLine();
    if (again) return;                                        // second click stops
    if (!s.audio) return;
    videoEl?.pause();
    linePlaying = s.id;
    const a = new Audio(`${media(s.audio)}&v=${Date.now()}`);
    lineAudio = a;
    const clear = () => { if (linePlaying === s.id) linePlaying = ''; };
    a.onended = clear;
    a.onerror = clear;
    try { await a.play(); } catch { clear(); }
  }

  /** A pill was dragged. Persist and refresh the mix once the drag settles —
      rebuilding on every pixel would queue a dozen assemblies. */
  let nudgeTimer: ReturnType<typeof setTimeout> | null = null;
  function onnudge() {
    if (nudgeTimer) clearTimeout(nudgeTimer);
    nudgeTimer = setTimeout(async () => {
      await save();
      if (previewSrc) await buildPreview();
    }, 450);
  }

  /** Hear the tone settings on one real line before committing to a re-render
      of all of them. Uses the selected line, so you judge it on your own words. */
  let toneAudio: HTMLAudioElement | null = null;
  let tonePlaying = $state(false);
  async function auditionTone() {
    const seg = selected ?? p.segments.find((x) => !x.locked);
    if (!seg) return;
    toneAudio?.pause();
    if (tonePlaying) { tonePlaying = false; return; }
    await save();
    tonePlaying = true;
    const d = await call(`/api/projects/${p.id}/segment/${seg.id}/synth`, { method: 'POST' }, 'tone');
    if (d?.segment) {
      p.segments[p.segments.findIndex((x) => x.id === seg.id)] = d.segment;
      videoEl?.pause();
      const a = new Audio(`${media(d.segment.audio)}&v=${Date.now()}`);
      toneAudio = a;
      a.onended = a.onerror = () => (tonePlaying = false);
      try { await a.play(); } catch { tonePlaying = false; }
    } else {
      tonePlaying = false;
    }
  }

  let confirmReset = $state(false);
  /** Put every per-line tweak back to the project defaults. Text, recordings
      and takes are untouched — only the knobs. */
  async function resetLines() {
    confirmReset = false;
    for (const s of p.segments) {
      s.over = undefined;
      s.offset = 0;
      s.stretch = 1;
      s.holdGap = false;
    }
    await save();
    if (previewSrc) await buildPreview();
  }

  async function loadPeaks(which: 'original' | 'preview') {
    peaksLoading = true;
    try {
      const r = await fetch(`/api/projects/${p.id}/peaks?track=${which}`);
      const d = await r.json();
      if (Array.isArray(d.peaks)) {
        // Keep the length of audio the strip covers. The mix ends with the
        // last take and the video ends where it ends — here 0.25s apart — and
        // a strip drawn to fill the width is stretched by exactly that ratio.
        if (which === 'original') { originalPeaks = d.peaks; originalSpan = d.duration ?? null; }
        else { previewPeaks = d.peaks; previewSpan = d.duration ?? null; }
      }
    } catch {
      /* no strip is better than a wrong one */
    } finally {
      peaksLoading = false;
    }
  }

  /** The render re-encodes the picture and measures loudness twice, so it runs
      detached on the server and this asks how it is going. */
  async function pollRender() {
    const r = await fetch(`/api/projects/${p.id}/render`);
    job = await r.json();
    if (job.status === 'running') {
      pollTimer = setTimeout(pollRender, 2000);
      return;
    }
    if (job.status === 'done' || job.status === 'stopped') {
      await absorbRenders();
      // Every time, not only when a preview already existed: step 3 is "render
      // the lines and let me hear them", and a first render that left the
      // operator to find a second button is how Build preview got pressed on a
      // project with nothing in it.
      if (job.kind === 'synth-all' && p.segments.some((s) => s.audio)) await buildPreview();
    }
  }

  /** Render every line that needs it. Long — forty lines is a good twenty
      minutes — so it runs detached, saves each take as it lands, and can be
      abandoned without losing what is already done. */
  async function startRenderAll(all = false) {
    await save();
    const d = await call(`/api/projects/${p.id}/render-all`, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ all })
    });
    if (d) { job = d; if (d.status === 'running') pollRender(); }
  }

  async function stopBatch() {
    const d = await call(`/api/projects/${p.id}/render-all`, { method: 'DELETE' });
    if (d) job = d;
  }

  /** Take the batch's results without discarding text edited since it began. */
  async function absorbRenders() {
    const fresh = await (await fetch(`/api/projects/${p.id}`)).json();
    if (!fresh?.segments) return;
    for (const f of fresh.segments) {
      const mine = p.segments.find((s) => s.id === f.id);
      if (!mine) continue;
      mine.audio = f.audio;
      mine.duration = f.duration;
      mine.wpm = f.wpm;
      mine.terminalStep = f.terminalStep;
      mine.renderedFrom = f.renderedFrom;
    }
    if (fresh.outputs) p.outputs = fresh.outputs;
    // Take the SETTINGS back too.
    //
    // A tab left open across a settings change made anywhere else — another
    // tab, a fix applied server-side — holds the values it loaded, and its
    // next save writes them all back. That is how per-line cloning came back
    // from the dead twice: it was switched off on the server, and a stale page
    // quietly restored it and restaled all thirty-seven lines. Absorbing here
    // costs nothing, because a batch has just finished and nobody is mid-edit.
    if (fresh.settings) p.settings = fresh.settings;
  }

  async function startRender() {
    await save();
    const d = await call(`/api/projects/${p.id}/render`, { method: 'POST' });
    if (d) { job = { status: 'running', step: d.step }; pollRender(); }
  }

  /** The analysis knows the length; a cached video may never fire a metadata
      event at all, which left the timeline dividing by zero. */
  function pickDuration() {
    // THE ANALYSIS IS THE AUTHORITY, not whatever media element is loaded.
    //
    // Every time on this timeline — a segment's span, an offset, the room a
    // line holds — is measured against the source recording. The <video>
    // element is not always showing that: once a preview has been built it is
    // showing the preview, which is a DIFFERENT length (418.27s against the
    // source's 420.68s here, because the picture proxy is a shade shorter than
    // the mix). Scaling the timeline by the element meant the whole strip
    // stretched by 0.58% the moment a render finished — correct on load, off by
    // eight pixels at the right-hand end a minute later, which is precisely
    // when the operator saw it move.
    if (p.duration && p.duration > 0.5) { duration = p.duration; return; }
    const d = videoEl?.duration;
    if (d && isFinite(d) && d > 0.5) duration = d;
  }

  /** The new source is ready: put the playhead back and carry on playing. */
  function onMeta() {
    pickDuration();
    measureVideo();
    const r = restore;
    restore = null;
    if (!r || !videoEl) return;
    if (r.at > 0.2 && Math.abs(videoEl.currentTime - r.at) > 0.2) videoEl.currentTime = r.at;
    if (r.play) videoEl.play().catch(() => { /* needs a gesture */ });
  }

  /** Grow a textarea to its content, so a long line is never a 2-row window. */
  function autosize(node: HTMLTextAreaElement) {
    const fit = () => { node.style.height = 'auto'; node.style.height = `${node.scrollHeight + 2}px`; };
    fit();
    node.addEventListener('input', fit);
    return { destroy: () => node.removeEventListener('input', fit) };
  }

  function seek(t: number) {
    if (videoEl) videoEl.currentTime = t;
    if (audioEl) audioEl.currentTime = t;
    playhead = t;
  }

  // Video carries the picture, the preview wav carries the sound. Keeping them
  // as two elements means a rebuild is instant — no re-encode to hear a change.
  // Swapping the element's source resets it, so remember where we were and
  // whether we were playing. Restored on `loadedmetadata`, NOT in a microtask:
  // the new source has not loaded by then, so the seek is discarded and you
  // land back at zero.
  let restore: { at: number; play: boolean } | null = null;
  $effect(() => {
    // BOTH of these swap the element's source: switching lane, and rebuilding
    // the mix (which mints a new filename to bust the cache). Watching only
    // `track` meant every rebuild dropped you back to the start.
    void track;
    void previewSrc;
    if (!videoEl || !previewIsVideo) return;
    restore = { at: untrack(() => playhead), play: !videoEl.paused };
  });

  let lastSync = 0;
  function syncFromVideo() {
    if (!videoEl) return;
    playhead = videoEl.currentTime;
    // Play the video as it will be cut: jump over a deleted section. Only while
    // playing, so a paused playhead can still be put inside one to restore it.
    //
    // The jump lands a little PAST the section's end, and nothing is checked
    // while a jump is still in flight. Jumping to the end exactly — which the
    // section's own test counts as inside it — made the seek's own "seeked"
    // event find the playhead in the section and jump again, over and over,
    // each seek stalling playback: heard and seen as the player flickering
    // between play and pause until it happened to creep past the edge.
    if (!videoEl.paused && !videoEl.seeking) {
      const gone = (p.cuts ?? []).find((c) => playhead >= c.from && playhead < c.to - 0.02);
      if (gone) { seek(Math.min(gone.to + 0.05, duration)); return; }
    }
    if (track !== 'preview' || !audioEl) return;
    // NEVER correct while it is still fetching. Setting currentTime is a seek,
    // a seek aborts the in-flight range request, aborting it makes the element
    // buffer again, and buffering grows the very drift that asked for the
    // correction. That loop ends with the audio stalled on `waiting` and the
    // browser pausing the video outright — silence, with no error anywhere.
    if (audioEl.readyState < 3 || audioEl.seeking) return;
    const drift = Math.abs(audioEl.currentTime - videoEl.currentTime);
    const now = performance.now();
    if (drift > 0.35 && now - lastSync > 1000) {
      lastSync = now;
      audioEl.currentTime = videoEl.currentTime;
    }
  }
  const onplay = () => {
    stopLine();   // an audition must never keep running under the timeline
    if (track !== 'preview' || !audioEl) return;
    // Only seek if it is actually somewhere else; a redundant seek at t=0 is
    // enough to abort the first fetch and stall the start of playback.
    if (Math.abs(audioEl.currentTime - videoEl!.currentTime) > 0.35) {
      audioEl.currentTime = videoEl!.currentTime;
    }
    audioEl.play().catch(() => { /* the lane switch will retry */ });
  };

  /** The native controls can unmute the video underneath the preview mix, and
      nothing else would notice: the mute effect only re-runs when the track
      changes. Re-assert it, and let the native slider drive the mix instead. */
  const onvolume = () => {
    if (!videoEl || track !== 'preview') return;
    if (audioEl) audioEl.volume = videoEl.volume;
    if (!videoEl.muted) videoEl.muted = true;
  };
  const onpause = () => audioEl?.pause();

  // Switching lane mid-playback has to hand the sound over. Muting the video
  // without also pausing the preview element left BOTH playing at once, which
  // is the "weird mix of audio tracks".
  $effect(() => {
    const t = track;
    if (!videoEl) return;
    // A muxed preview carries its own sound: muting it would silence the mix.
    videoEl.muted = t === 'preview' && !previewIsVideo;
    if (!audioEl) return;
    if (t === 'preview') {
      if (Math.abs(audioEl.currentTime - videoEl.currentTime) > 0.35) {
        audioEl.currentTime = videoEl.currentTime;
      }
      if (!videoEl.paused) audioEl.play().catch(() => {});
    } else {
      audioEl.pause();
    }
  });

  $effect(() => {
    untrack(() => {
      loadPeaks('original');
      pollRender();
      // The assembled track outlives the page. Restoring it — along with the
      // mix it was built from — means a reload keeps what you were listening
      // to, and still says plainly if the script has moved on since.
      const pv = data.project.preview;
      if (pv?.path) {
        previewSrc = `${media(pv.path)}&v=${pv.ts}`;
        previewBuiltFrom = pv.sig;
        loadPeaks('preview');
      }
    });
    return () => { if (pollTimer) clearTimeout(pollTimer); };
  });
</script>

<svelte:head><title>{p.name || 'Project'} — Voiceover</title></svelte:head>

<div class="mb-4 flex flex-wrap items-baseline gap-3">
  <h1 class="text-xl font-semibold">{p.name}</h1>
  <div class="flex overflow-hidden rounded-md border border-gray-800 text-xs">
    <button class="px-3 py-1 transition {view === 'script' ? 'bg-indigo-600 text-white' : 'text-gray-400 hover:bg-gray-900'}"
            onclick={() => (view = 'script')}>Script</button>
    <button class="px-3 py-1 transition {view === 'tune' ? 'bg-indigo-600 text-white' : 'text-gray-400 hover:bg-gray-900'}"
            onclick={() => (view = 'tune')}>Tune</button>
  </div>
  <span class="truncate text-xs text-gray-500">{p.source}</span>
  {#if p.metrics}
    <span class="text-xs text-gray-400">
      {p.metrics.wpm} wpm · {p.metrics.hard_fillers} ums · {p.metrics.stutters} repeats
    </span>
  {/if}
</div>

{#if error}<Alert color="red" class="mb-4">{error}</Alert>{/if}

<svelte:window onclick={() => (menu = null)}
               onkeydown={(e) => { if (e.key === 'Escape') menu = null; }} />

{#if menu && menuInfo}
  {@const mi = menuInfo}
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <div class="fixed z-50 w-[300px] rounded-lg border border-gray-700 bg-gray-900 p-1 text-xs text-gray-200 shadow-2xl"
       style="left:{menu.x}px; top:{menu.y}px" role="menu" tabindex="-1"
       onclick={(e) => e.stopPropagation()}>
    <div class="px-2 pb-1 pt-0.5 font-mono text-[10px] text-gray-500">
      {clock(menu.t)}{#if mi.block} · block {blockLabel(mi.block)}{/if}{#if mi.cut} · deleted{/if}
    </div>
    {#if picked.length >= 2 && pickedCheck}
      {#if 'error' in pickedCheck}
        <div class="px-2 py-1.5 text-amber-300/80">Can't merge {pickedLabel}: {pickedCheck.error}</div>
      {:else}
        <button class="block w-full rounded px-2 py-1.5 text-left hover:bg-gray-800" onclick={mergePicked}>
          Merge blocks {pickedLabel} into one
          <span class="block text-[10px] text-gray-500">one line, rendered as one take — split back any time</span>
        </button>
      {/if}
      <div class="my-1 border-t border-gray-800"></div>
    {/if}
    {#if mi.block?.mergedFrom?.length}
      <button class="block w-full rounded px-2 py-1.5 text-left hover:bg-gray-800" onclick={() => mi.block && splitBack(mi.block.id)}>
        Split block {blockLabel(mi.block)} back into {mi.block.mergedFrom.length}
        <span class="block text-[10px] text-gray-500">their original takes come back as they were</span>
      </button>
    {/if}
    {#if mi.cut}
      <button class="block w-full rounded px-2 py-1.5 text-left hover:bg-gray-800" onclick={() => timelineOp('restore')}>
        Restore this section ({(mi.cut.to - mi.cut.from).toFixed(1)}s)
        {#if mi.cut.dropped?.length}<span class="text-gray-500"> · brings back block{mi.cut.dropped.length > 1 ? 's' : ''} {mi.cut.dropped.map((id) => blockLabel({ id })).join(', ')}</span>{/if}
      </button>
    {:else}
      {#if mi.nearSlice != null}
        <button class="block w-full rounded px-2 py-1.5 text-left hover:bg-gray-800" onclick={() => timelineOp('unslice')}>
          Remove the slice at {clock(mi.nearSlice)}
        </button>
      {:else if mi.block}
        <div class="px-2 py-1.5 text-gray-500">Can't slice here — block {blockLabel(mi.block)} is playing</div>
      {:else}
        <button class="block w-full rounded px-2 py-1.5 text-left hover:bg-gray-800" onclick={() => timelineOp('slice')}>
          ✂ Slice here
        </button>
      {/if}

      {#if mi.whole}
        <div class="px-2 py-1.5 text-gray-500">Slice first — then delete the part you don't want</div>
      {:else if mi.check && 'error' in mi.check}
        <div class="px-2 py-1.5 text-amber-300/80">Can't delete {clock(mi.section[0])}–{clock(mi.section[1])}: {mi.check.error}</div>
      {:else if mi.check}
        <button class="block w-full rounded px-2 py-1.5 text-left text-red-300 hover:bg-gray-800" onclick={() => timelineOp('cut')}>
          Delete this section · {clock(mi.section[0])}–{clock(mi.section[1])} ({(mi.section[1] - mi.section[0]).toFixed(1)}s)
          {#if mi.check.inside.length}<span class="block text-[10px] text-gray-500">takes block{mi.check.inside.length > 1 ? 's' : ''} {mi.check.inside.map((id) => blockLabel({ id })).join(', ')} with it — restore brings {mi.check.inside.length > 1 ? 'them' : 'it'} back</span>{/if}
        </button>
      {/if}

      <div class="my-1 border-t border-gray-800"></div>
      {#each masksAt(p, menu.t) as m (m.id)}
        <div class="px-2 pt-1 text-[10px] text-violet-300">blur mask {m.id.slice(1)} · {clock(m.from)}–{clock(m.to)}</div>
        <div class="flex gap-1 px-1 pb-1">
          <button class="rounded px-2 py-1 hover:bg-gray-800" onclick={() => menu && maskSet(m.id, { to: menu.t })}>end it here</button>
          <button class="rounded px-2 py-1 hover:bg-gray-800" onclick={() => menu && maskSet(m.id, { from: menu.t })}>start it here</button>
          <button class="rounded px-2 py-1 text-red-300 hover:bg-gray-800" onclick={() => maskRemove(m.id)}>delete</button>
        </div>
      {/each}
      <div class="px-2 pt-1 text-[10px] text-gray-500">Blur from here, for ten seconds:</div>
      <div class="flex flex-wrap gap-1 px-1 pb-1">
        {#each Object.entries(MASK_PRESETS) as [k, v] (k)}
          <button class="rounded border border-gray-800 px-2 py-1 hover:bg-gray-800" onclick={() => maskAdd(k)}>{v.label}</button>
        {/each}
      </div>
      <div class="my-1 border-t border-gray-800"></div>
      {#if !mi.block && mi.gap && mi.room >= ADD_MIN}
        {#if !menuAdding}
          <button class="block w-full rounded px-2 py-1.5 text-left hover:bg-gray-800" onclick={() => (menuAdding = true)}>
            + Add a block here… <span class="text-gray-500">({(mi.gap[1] - mi.gap[0]).toFixed(1)}s of air)</span>
          </button>
        {:else}
          <div class="flex gap-1 p-1">
            <!-- svelte-ignore a11y_autofocus -->
            <input bind:value={menuSay} autofocus placeholder="e.g. Thank you for watching."
                   onkeydown={(e) => { if (e.key === 'Enter') menuAddBlock(); }}
                   class="min-w-0 flex-1 rounded border border-gray-700 bg-gray-950 px-2 py-1 text-xs
                          text-gray-100 placeholder:text-gray-600 focus:border-indigo-600 focus:ring-0" />
            <Button size="xs" disabled={!menuSay.trim() || !!busy} onclick={menuAddBlock}>Add</Button>
          </div>
        {/if}
      {/if}
    {/if}
  </div>
{/if}

<!-- Four steps. Everything else is under Advanced. -->
<div class="mb-4 rounded-lg border border-gray-800 bg-gray-950 p-3">
  <div class="flex flex-wrap items-center gap-2">
    <span class="text-[11px] uppercase tracking-wide text-gray-500">1 · Script</span>
    {#if !p.segments.length}
      <Button size="xs" disabled={!!busy} onclick={transcribe}>
        {busy === 'analyze' ? 'Transcribing…' : busy === 'script' ? 'Splitting…' : 'Transcribe'}
      </Button>
    {:else}
      <span class="text-[11px] text-gray-400">{p.segments.length} lines</span>
    {/if}
    <Button size="xs" color="alternative" disabled={!!busy || !p.segments.length} onclick={rewrite}
            title="rewrite the script, then read it whole so it flows. Lines you edited by hand are left alone; each line keeps its previous wording and can be put back.">
      {busy === 'clean' ? 'Rewriting…' : busy === 'flow' ? 'Making it flow…' : 'Rewrite'}
    </Button>
    <Button size="xs" color="alternative" disabled={!!busy || !p.segments.length} onclick={runPolish}
            title="suggest a finished-script version of each block, learned from your edits on earlier videos. Nothing changes until you take a suggestion.">
      {busy === 'polish' ? 'Polishing…' : 'Polish'}
    </Button>

    <span class="mx-1 h-4 w-px bg-gray-800"></span>
    <span class="text-[11px] uppercase tracking-wide text-gray-500">2 · Voice</span>
    <button class="rounded border px-2 py-0.5 text-xs
                   {chosenVoice ? 'border-gray-700 text-gray-200 hover:border-gray-500'
                                : 'border-amber-700 text-amber-300 hover:border-amber-500'}"
            onclick={openVoice}>
      {#if chosenVoice}{chosenVoice.name} · {bare ? 'model alone' : 'assisted'}
      {:else}pick a voice{/if}
    </button>

    <span class="mx-1 h-4 w-px bg-gray-800"></span>
    <Button size="xs" color={counts.done + counts.real < counts.total ? 'primary' : 'alternative'}
            disabled={!!busy || job.status === 'running' || !p.segments.length}
            onclick={() => startRenderAll(false)}
            title="render every line that has no take, or whose text or settings changed — then build the preview to listen to">
      {job.status === 'running' && job.kind === 'synth-all' ? 'Rendering…' : '3 · Render lines'}
    </Button>
    <Button size="xs" color="green"
            disabled={!!busy || job.status === 'running' || !counts.done}
            onclick={startRender}>
      {job.status === 'running' && job.kind !== 'synth-all' ? 'Rendering…' : '4 · Final video'}
    </Button>

    {#if busy}<span class="flex items-center gap-2 text-xs text-gray-400"><Spinner size="4" /> {busy}</span>{/if}
    <span class="ml-auto text-[11px] text-gray-500">
      {counts.done} rendered · {counts.real} kept real · {counts.stale} stale · {counts.total} lines
    </span>
    <button class="text-[11px] text-gray-500 underline-offset-2 hover:text-gray-300 hover:underline"
            onclick={() => (advanced = !advanced)}>
      {advanced ? 'Hide advanced' : 'Advanced'}
    </button>
  </div>

  {#if advanced}
    <div class="mt-2 flex flex-wrap items-center gap-2 border-t border-gray-800 pt-2">
    <Button size="xs" color="alternative" disabled={!!busy} onclick={() => step('analyze', 'analyze')}>
      {busy === 'analyze' ? 'Transcribing…' : 'Transcribe again'}
    </Button>
    <Button size="xs" color="alternative" disabled={!!busy} onclick={() => step('master', 'master')}>
      {busy === 'master' ? 'Mastering…' : 'Master the recording'}
    </Button>
    <Button size="xs" color="alternative" disabled={!!busy || !p.analysis} onclick={() => step('script', 'script')}>
      {busy === 'script' ? 'Splitting…' : 'Split into lines again (replaces the script)'}
    </Button>
    <Button size="xs" color="alternative" disabled={!!busy || !p.segments.length} onclick={() => step('clean', 'clean')}>
      {busy === 'clean' ? 'Rewriting…' : 'Rewrite only'}
    </Button>
    <Button size="xs" color="alternative" disabled={!!busy || !p.segments.length} onclick={flowPass}
            title="edit the whole script so it reads as one piece, not a list of repairs">
      {busy === 'flow' ? 'Reading it whole…' : 'Make it flow only'}
    </Button>
    <Button size="xs" color={previewStale ? 'yellow' : 'alternative'}
            disabled={!!busy || !p.segments.length} onclick={buildPreview}>
      {busy === 'preview' ? 'Assembling…'
        : previewStale ? 'Rebuild preview'
        : previewSrc ? 'Preview up to date' : 'Build preview track'}
    </Button>
    <Button size="xs" color="alternative" disabled={!!busy} onclick={save}>Save</Button>
    {#if confirmReset}
      <span class="text-[11px] text-amber-300">Reset every line's timing and knobs?</span>
      <Button size="xs" color="red" onclick={resetLines}>Yes, reset</Button>
      <Button size="xs" color="alternative" onclick={() => (confirmReset = false)}>Cancel</Button>
    {:else}
      <Button size="xs" color="alternative" disabled={!!busy}
              onclick={() => (confirmReset = true)}
              title="put every per-line nudge, stretch, held gap and knob back to the project defaults">
        Reset line tweaks
      </Button>
    {/if}
    </div>
  {/if}
</div>

{#if job.status !== 'idle'}
  <div class="mb-4 rounded-lg border p-3 text-xs
              {job.status === 'error' ? 'border-red-900 bg-red-950/40'
               : job.status === 'done' && job.kind !== 'synth-all' ? 'border-emerald-900 bg-emerald-950/30'
               : 'border-gray-800 bg-gray-950'}">
    <div class="flex flex-wrap items-center gap-2">
      {#if job.status === 'running'}<Spinner size="4" />{/if}
      <span class="font-medium text-gray-200">
        {job.status === 'running' ? job.step
          : job.status === 'stopped' ? job.step
          : job.status === 'done' ? (job.kind === 'synth-all' ? job.step : 'Final video ready')
          : 'Render failed'}
      </span>
      {#if job.seconds}<span class="font-mono text-gray-500">{job.seconds}s</span>{/if}
      {#if job.kind === 'synth-all' && job.total}
        <span class="font-mono text-gray-400">{job.done ?? 0} / {job.total} lines</span>
        <div class="h-1.5 w-40 overflow-hidden rounded bg-gray-800">
          <div class="h-full bg-indigo-500 transition-all"
               style="width:{Math.round(((job.done ?? 0) / job.total) * 100)}%"></div>
        </div>
      {/if}
      {#if job.status === 'running' && job.kind === 'synth-all'}
        <Button size="xs" color="alternative" onclick={stopBatch}>Stop after this line</Button>
      {/if}
      {#if job.status === 'done' && job.kind !== 'synth-all' && p.outputs.final}
        <span class="ml-auto flex items-center gap-3">
          <a class="text-emerald-400 underline" href="{media(p.outputs.final)}&download=1" download={p.outputs.final.split('/').pop()}>
            download {p.outputs.final.split('/').pop()}
          </a>
          <a class="text-gray-400 underline hover:text-gray-200"
             href="/api/projects/{p.id}/captions?format=srt"
             title="subtitles timed to the rendered cut — upload alongside the video">captions .srt</a>
          <a class="text-gray-400 underline hover:text-gray-200"
             href="/api/projects/{p.id}/captions?format=vtt">.vtt</a>
        </span>
      {/if}
    </div>
    {#if job.error}<pre class="mt-2 whitespace-pre-wrap text-[11px] text-red-300">{job.error}</pre>{/if}
    {#if job.log?.length}
      <ol class="mt-2 space-y-0.5 text-[11px] text-gray-500">
        {#each job.log as line}<li>{line}</li>{/each}
      </ol>
    {/if}
    {#if job.status === 'done' && job.kind !== 'synth-all' && p.outputs.final}
      <!-- svelte-ignore a11y_media_has_caption -->
      <video controls class="mt-3 max-h-[40vh] w-full rounded bg-black" src={media(p.outputs.final)}></video>
    {/if}
  </div>
{:else if p.outputs.final}
  <!-- The job register lives in memory, so a server restart loses the run but
       not the file it produced. Keep the finished video reachable. -->
  <div class="mb-4 flex flex-wrap items-center gap-3 rounded-lg border border-emerald-900 bg-emerald-950/30 p-3 text-xs">
    <span class="font-medium text-gray-200">Final video from an earlier run</span>
    <a class="text-emerald-400 underline" href="{media(p.outputs.final)}&download=1" download={p.outputs.final.split('/').pop()}>
      download {p.outputs.final.split('/').pop()}
    </a>
    <a class="ml-3 text-gray-400 underline hover:text-gray-200"
       href="/api/projects/{p.id}/captions?format=srt"
       title="subtitles timed to the rendered cut — YouTube takes .srt directly">captions .srt</a>
    <a class="ml-2 text-gray-400 underline hover:text-gray-200"
       href="/api/projects/{p.id}/captions?format=vtt">.vtt</a>
    <span class="text-gray-500">re-render to pick up anything you have changed since</span>
  </div>
{/if}

{#if view === 'script'}
  <!-- The whole read, in order. One pass over the words before committing the
       machine to twenty minutes of synthesis. -->
  <div class="space-y-2">
    <p class="text-xs text-gray-500">
      Edit the whole read here, then <strong class="text-gray-300">Render all</strong>.
      Lines you mark <em>keep my voice</em> are skipped. Switch to
      <strong class="text-gray-300">Tune</strong> to work a single line against the picture.
    </p>
    <!-- Shown whenever a polish run exists, so the filter below can always be
         switched off. It used to hide once nothing was open, and took its own
         checkbox with it, leaving the view stuck on whatever was filtered. -->
    {#if polishRan}
      <div class="sticky top-0 z-10 flex flex-wrap items-center gap-x-3 gap-y-1 rounded-lg border
                  border-indigo-900/60 bg-gray-950/95 p-2 text-[11px] backdrop-blur">
        <span class="font-medium text-indigo-300">Polish</span>
        <span class="text-gray-400">
          {#if polishOpen.length || polishNoted.length}
            {polishDrafts.length} on blocks you haven't edited ·
            {polishOpen.filter((x) => sugFor(x)?.edited).length} tidy-ups on your edits ·
            {polishOpen.filter((x) => sugFor(x)?.kind === 'drop').length} to consider dropping ·
            {polishNoted.length} with notes
          {:else}
            nothing left to review
          {/if}
        </span>
        <Button size="xs" color="alternative" disabled={!polishDrafts.length || !!busy} onclick={takeAllDrafts}
                title="take every suggestion on blocks you have not edited yourself. Your own edits and drops are left for you, one at a time.">
          Use all {polishDrafts.length} on unedited blocks
        </Button>
        <label class="ml-auto flex items-center gap-1.5 text-gray-400">
          <input type="checkbox" class="accent-indigo-500" bind:checked={polishOnly} />
          only the {reviewSet.size} blocks polish looked at
        </label>
      </div>
    {/if}
    {#each p.segments as s, i (s.id)}
      {@const g = sugFor(s)}
      {@const notes = notesFor(s)}
      {#if !polishOnly || reviewSet.has(s.id)}
      <div class="rounded-lg border p-3 transition
                  {s.locked ? 'border-emerald-900/60 bg-emerald-950/20' : 'border-gray-800 bg-gray-950'}">
        <div class="mb-1.5 flex flex-wrap items-center gap-2 text-[10px] text-gray-500">
          <!-- The block number, as the timeline shows it: the number the
               operator actually uses to talk about a line. -->
          <span class="rounded bg-gray-800 px-1.5 py-0.5 font-mono text-[11px] text-gray-200"
                title="block {blockNo(s)}">{blockNo(s)}</span>
          <span class="font-mono">{Math.floor(s.start / 60)}:{(s.start % 60).toFixed(0).padStart(2, '0')}</span>
          {#if s.locked}<span class="text-emerald-500">your voice</span>
          {:else if stale(s)}<span class="text-amber-500">needs re-render</span>
          {:else if s.audio}<span class="text-indigo-400">rendered</span>
          {:else}<span class="text-gray-600">not rendered</span>{/if}
          <!-- What the checker made of this take. It transcribed the line and
               compared it to the script at render time either way; showing the
               verdict is the difference between finding a bad line and
               listening to all of them to find it. -->
          {#if s.check && s.check.score !== null && !s.check.ok}
            <span class="rounded bg-red-950/60 px-1.5 py-0.5 font-medium text-red-300"
                  title="The take was transcribed and did not match the script. Three draws were tried; this was the closest.">
              says {Math.round((s.check.score ?? 0) * 100)}%
            </span>
          {:else if s.check && s.check.score === null}
            <span class="text-gray-600" title="The transcriber was unavailable, so this take was never checked.">unchecked</span>
          {/if}
          {#if s.check?.heard && !s.check.ok}
            <span class="truncate italic text-gray-500">heard: {s.check.heard.slice(0, 70)}</span>
          {:else if s.heard && s.heard !== s.say}
            <span class="truncate italic text-gray-600">heard: {s.heard.slice(0, 80)}</span>
          {/if}
          <div class="ml-auto flex items-center gap-2">
            <button class="rounded p-1 transition
                           {linePlaying === s.id ? 'bg-indigo-600 text-white'
                            : s.audio ? 'text-gray-500 hover:bg-gray-800 hover:text-indigo-300' : 'text-gray-700'}"
                    disabled={!s.audio} onclick={() => playLine(s)} aria-label="hear this line">
              {#if linePlaying === s.id}
                <svg viewBox="0 0 10 10" class="h-2.5 w-2.5 fill-current"><rect x="2" y="2" width="6" height="6" /></svg>
              {:else}
                <svg viewBox="0 0 10 10" class="h-2.5 w-2.5 fill-current"><path d="M2.5 1.2 8 5l-5.5 3.8z" /></svg>
              {/if}
            </button>
            <Toggle bind:checked={s.locked} size="small"><span class="text-[10px]">keep my voice</span></Toggle>
            <button class="rounded px-1.5 py-0.5 text-[10px] transition
                           {s.drop ? 'bg-red-900/70 text-red-200'
                            : 'text-gray-500 hover:bg-gray-800 hover:text-red-300'}"
                    onclick={() => { s.drop = !s.drop; save(); }}
                    title="leave this line out of the finished video entirely">
              {s.drop ? 'dropped' : 'drop'}
            </button>
            <button class="text-gray-500 underline hover:text-indigo-300"
                    onclick={() => { selectedId = s.id; view = 'tune'; seek(s.start); }}>tune</button>
          </div>
        </div>
        <!-- Saved when the box loses focus. Script edits used to wait for the
             Save button, so leaving the page — or the page reloading under an
             update — quietly threw away whatever had been typed. -->
        <textarea use:autosize bind:value={p.segments[i].say} onchange={save} disabled={s.locked || s.drop} rows="1"
                  class="w-full resize-none overflow-hidden rounded border border-gray-800 bg-gray-900 p-2
                         text-sm leading-relaxed text-gray-100 focus:border-indigo-600 focus:ring-0
                         disabled:opacity-50"></textarea>
        {#if g}
          <div class="mt-1.5 rounded border border-indigo-900/60 bg-indigo-950/20 p-2 text-sm">
            <div class="mb-1 flex flex-wrap items-center gap-2 text-[10px] uppercase tracking-wide text-indigo-300">
              {g.kind === 'drop' ? 'consider dropping this block' : g.edited ? 'tidy your edit' : 'polish'}
              {#if g.kind !== 'drop' && editedSince(s, g)}
                <span class="normal-case tracking-normal text-amber-300/90">
                  · you've edited this line since — using it replaces your edit
                </span>
              {/if}
            </div>
            {#if g.kind !== 'drop'}
              <p class="text-xs leading-relaxed">
                {#each wordDiff(s.say, g.text) as piece}
                  {#if piece.t === 'same'}<span class="text-gray-400">{piece.w + ' '}</span>
                  {:else if piece.t === 'add'}<span class="rounded bg-emerald-900/60 px-0.5 text-emerald-200">{piece.w}</span>{' '}
                  {:else}<span class="text-red-400/70 line-through">{piece.w}</span>{' '}{/if}
                {/each}
              </p>
              <!-- The same suggestion as it would be read: no strike-through
                   to decipher. -->
              <p class="mt-1.5 border-l-2 border-emerald-700/70 pl-2 leading-relaxed text-gray-100">{g.text}</p>
            {:else}
              <p class="text-xs text-gray-400">It doesn't carry anything the picture needs.</p>
            {/if}
            {#if g.notes?.length}<p class="mt-1 text-[10px] text-amber-300/80">{g.notes.join(' · ')}</p>{/if}
            <div class="mt-1.5 flex gap-2">
              <Button size="xs" color="primary" disabled={!!busy} onclick={() => takeSuggestion(s, g)}>
                {g.kind === 'drop' ? 'Drop this block' : 'Use this'}
              </Button>
              <Button size="xs" color="alternative" disabled={!!busy}
                      onclick={() => markPolish([s.id], 'dismissed')}>Keep mine</Button>
            </div>
          </div>
        {/if}
        {#if notes.length}
          <p class="mt-1 text-[11px] text-amber-300/80">{notes.join(' · ')}</p>
        {/if}
        {#if s.sayBeforePolish && polish.statuses?.[s.id] === 'accepted'}
          <button class="mt-1 text-[10px] text-gray-500 underline hover:text-gray-300"
                  onclick={() => undoPolish(s)}
                  title={`put back: ${s.sayBeforePolish}`}>undo polish</button>
        {/if}
        <!-- How this ONE line should land, when it differs from the script's
             own direction. Left empty it inherits, which is what almost every
             line wants — a whole read delivered a line at a time is how a
             performance stops being one. -->
        {#if !s.locked && !s.drop}
          <input
            class="mt-1 w-full rounded border border-gray-800/70 bg-gray-900/60 px-2 py-1
                   text-[11px] text-gray-300 placeholder:text-gray-600
                   focus:border-indigo-600 focus:ring-0"
            placeholder={p.settings.direction
              ? `direction for this line (otherwise: ${p.settings.direction})`
              : 'direction for this line — optional'}
            bind:value={p.segments[i].direction} onchange={save} />
        {/if}
      </div>
      {/if}
    {/each}
    {#if !p.segments.length}
      <p class="p-3 text-xs text-gray-500">Transcribe, then split into lines.</p>
    {/if}
    <div class="sticky bottom-0 flex items-center gap-2 border-t border-gray-800 bg-gray-900/95 py-3">
      <Button size="sm" disabled={!!busy} onclick={save}>Save script</Button>
      <Button size="sm" color="primary" disabled={!!busy || job.status === 'running'}
              onclick={() => startRenderAll(false)}>Render all</Button>
      <!-- Render all only touches lines whose TEXT or KNOBS changed, which is
           right almost always and useless after a fix to how a take is
           processed rather than to what is said: nothing restales, so the
           button reports everything as already rendered. -->
      <button
        class="text-[11px] text-gray-500 underline hover:text-gray-300 disabled:opacity-40"
        disabled={!!busy || job.status === 'running'}
        onclick={() => { if (confirm(`Re-render all ${counts.total} lines from scratch? Lines kept as the original are left alone.`)) startRenderAll(true); }}>
        re-render every line
      </button>
      <span class="text-[11px] text-gray-500">
        {counts.total - counts.done - counts.real} of {counts.total} lines still need a take
      </span>
      <!-- A line whose take does not say the line is not a missing take: it
           renders, it plays, and it is wrong. Counting them here is what turns
           "listen to all 143" into "look at these four". -->
      {#if suspect.length}
        <button class="rounded bg-red-950/60 px-2 py-0.5 text-[11px] font-medium text-red-300
                       transition hover:bg-red-900/60"
                onclick={() => { const t = suspect[0]; selectedId = t.id; view = 'tune'; seek(t.start); }}
                title={suspect.map((x) => x.id).join(', ')}>
          {suspect.length} take{suspect.length === 1 ? '' : 's'} did not say the line
        </button>
      {/if}
      {#if unchecked.length}
        <span class="text-[11px] text-amber-600/80"
              title="Rendered before the checker worked, or while it was unavailable. Re-render to find out.">
          {unchecked.length} never checked
        </span>
      {/if}
    </div>
  </div>
{:else}
<div class="mb-4 space-y-3">
  <div class="relative overflow-hidden rounded-lg border border-gray-800 bg-black">
    <!-- One element. The mix arrives already muxed onto a light proxy of the
         picture, so there is no second media element to keep in sync and
         nothing to starve. -->
    <!-- svelte-ignore a11y_media_has_caption -->
    <video bind:this={videoEl} controls class="max-h-[42vh] w-full bg-black"
           src={track === 'preview' && previewIsVideo ? previewSrc : media(p.outputs.master ?? p.source)}
           ontimeupdate={syncFromVideo} onseeked={syncFromVideo}
           onplay={onplay} onpause={onpause} onvolumechange={onvolume}
           onloadedmetadata={onMeta} ondurationchange={pickDuration}></video>
    <!-- Blur masks, on a layer that covers exactly the PICTURE (not the
         letterboxed element). Showing: the blur previewed live. Editing:
         drag on the picture to draw one; drag its middle to move it, any
         edge or corner to resize it. -->
    {#if maskEdit || masksNow.length}
      <!-- svelte-ignore a11y_no_static_element_interactions -->
      <div bind:this={maskLayer}
           class="absolute {maskEdit ? 'cursor-crosshair' : 'pointer-events-none'}"
           style="left:{videoBox.left}px; top:{videoBox.top}px; width:{videoBox.width}px; height:{videoBox.height}px;
                  {maskEdit ? 'outline: 1px dashed rgba(196,181,253,.5); outline-offset: -1px;' : ''}"
           onpointerdown={drawDown} onpointermove={drawMove} onpointerup={drawUp}>
        {#each masksNow as m (m.id)}
          <!-- svelte-ignore a11y_no_static_element_interactions -->
          <div class="absolute {maskEdit ? 'cursor-move border border-dashed' : ''}
                      {maskEdit && selectedMask === m.id ? 'border-violet-200 ring-1 ring-violet-300' : 'border-violet-400/70'}"
               style="left:{m.x * 100}%; top:{m.y * 100}%; width:{m.w * 100}%; height:{m.h * 100}%;
                      {maskOn(m) ? 'backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px); background: rgba(10,10,20,.18);'
                                 : 'background: rgba(139,92,246,.10); opacity: .75;'}"
               onpointerdown={(e) => maskDown(e, m, 'move')} onpointermove={maskMove} onpointerup={maskUp}>
            {#if maskEdit}
              <span class="absolute left-1 top-1 flex items-center gap-1.5 rounded bg-violet-950/90 px-1.5 py-0.5
                           text-[10px] text-violet-100"
                    onpointerdown={(e) => e.stopPropagation()}>
                mask {m.id.slice(1)} · {clock(m.from)}–{clock(m.to)}{#if !maskOn(m)} · not showing now{/if}
                <button class="underline hover:text-white" title="end this mask at the playhead"
                        onclick={() => maskSet(m.id, { to: playhead })}>end here</button>
                <button class="text-red-300 hover:text-red-200" title="delete this mask"
                        onclick={() => maskRemove(m.id)}>✕</button>
              </span>
              {#each GRIPS as gr (gr.mode)}
                <span class="absolute h-3.5 w-3.5 rounded-sm border border-violet-950 bg-violet-200 hover:bg-white"
                      style="{gr.at}; cursor:{gr.cursor}"
                      onpointerdown={(e) => maskDown(e, m, gr.mode)} onpointermove={maskMove} onpointerup={maskUp}></span>
              {/each}
            {/if}
          </div>
        {/each}
        {#if draw}
          <div class="pointer-events-none absolute border-2 border-violet-300 bg-violet-400/20"
               style="left:{Math.min(draw.x0, draw.x1) * 100}%; top:{Math.min(draw.y0, draw.y1) * 100}%;
                      width:{Math.abs(draw.x1 - draw.x0) * 100}%; height:{Math.abs(draw.y1 - draw.y0) * 100}%;"></div>
        {/if}
      </div>
    {/if}
  </div>
  <div class="flex flex-wrap items-center gap-2 text-[11px]">
    <button class="rounded border px-2 py-0.5 {maskEdit ? 'border-violet-400 bg-violet-950/60 text-violet-100' : 'border-gray-700 text-gray-300 hover:border-gray-500'}"
            onclick={toggleMaskEdit}>
      {maskEdit ? 'Done with blur masks' : 'Edit blur masks'}
    </button>
    {#if maskEdit}
      <span class="text-violet-200/80">
        Drag on the picture to draw one, from {clock(playhead)} for ten seconds · drag a mask to move it,
        an edge or corner to resize · on the timeline, drag its bar to shift it or an end to change its length
      </span>
    {:else if (p.masks ?? []).length}
      <span class="text-gray-500">{(p.masks ?? []).length} blur mask{(p.masks ?? []).length === 1 ? '' : 's'} — they show over the picture while they're on</span>
    {/if}
  </div>
  {#if maskEdit && (p.masks ?? []).length}
    <!-- Every mask, with its exact times. Type a time (1:23.4 or 83.4), or
         set it from the playhead. -->
    <div class="rounded-lg border border-violet-900/60 bg-gray-950 p-2 text-[11px]">
      <div class="grid grid-cols-[auto_auto_auto_1fr_auto] items-center gap-x-3 gap-y-1">
        <span class="text-[10px] uppercase tracking-wide text-gray-500">mask</span>
        <span class="text-[10px] uppercase tracking-wide text-gray-500">starts</span>
        <span class="text-[10px] uppercase tracking-wide text-gray-500">ends</span>
        <span class="text-[10px] uppercase tracking-wide text-gray-500">covers</span>
        <span></span>
        {#each p.masks ?? [] as m (m.id)}
          <button class="rounded px-1.5 py-0.5 text-left font-mono {selectedMask === m.id ? 'bg-violet-800/70 text-white' : 'text-violet-200 hover:bg-gray-800'}"
                  onclick={() => maskGo(m)} title="go to it">{m.id.slice(1)}{#if maskOn(m)} ●{/if}</button>
          <span class="flex items-center gap-1">
            <input class="w-16 rounded border border-gray-800 bg-gray-900 px-1 py-0.5 font-mono text-gray-100 focus:border-violet-500 focus:ring-0"
                   value={clock(m.from)} onchange={(e) => maskTime(m.id, 'from', e.currentTarget.value)} />
            <button class="text-gray-500 underline hover:text-gray-200" onclick={() => maskSet(m.id, { from: playhead })}
                    title="start it at the playhead">⟵ here</button>
          </span>
          <span class="flex items-center gap-1">
            <input class="w-16 rounded border border-gray-800 bg-gray-900 px-1 py-0.5 font-mono text-gray-100 focus:border-violet-500 focus:ring-0"
                   value={clock(m.to)} onchange={(e) => maskTime(m.id, 'to', e.currentTarget.value)} />
            <button class="text-gray-500 underline hover:text-gray-200" onclick={() => maskSet(m.id, { to: playhead })}
                    title="end it at the playhead">⟵ here</button>
          </span>
          <span class="truncate text-gray-500">
            {Math.round(m.w * 100)}% × {Math.round(m.h * 100)}% at {Math.round(m.x * 100)}%, {Math.round(m.y * 100)}%
            · {(m.to - m.from).toFixed(1)}s
          </span>
          <span class="flex items-center gap-2">
            <button class="text-gray-400 underline hover:text-gray-200" onclick={() => maskContinue(m)}
                    title="another mask in the same place, starting where this one ends">continue after</button>
            <button class="text-red-300/80 hover:text-red-200" onclick={() => maskRemove(m.id)} title="delete">✕</button>
          </span>
        {/each}
      </div>
    </div>
  {/if}
  {#if previewSrc && !previewIsVideo}
    <!-- svelte-ignore a11y_media_has_caption -->
    <audio bind:this={audioEl} src={previewSrc} preload="auto" class="hidden"></audio>
  {/if}
  <Timeline project={p} {duration} {playhead} bind:selectedId bind:track onseek={seek}
            original={originalPeaks} preview={previewPeaks}
            {previewStale} loading={peaksLoading} {onnudge} {originalSpan} {previewSpan}
            gapMin={p.settings.gapMin ?? 0.8}
            onhold={() => save()} onmenu={openMenu} {onpick} {picked}
            {selectedMask} onmaskselect={(id) => (selectedMask = id)} onmaskdone={maskTimeDone} />

  <!-- Narration where the recording has only dead air: a sign-off over the
       closing images, an introduction, a line between two others. Only ever
       into free space, with room either side, so it cannot land on a
       neighbour. -->
  <div class="mt-2 flex flex-wrap items-center gap-2 text-[11px]">
    {#if (p.cuts ?? []).length}
      <span class="text-red-300/80">
        {(p.cuts ?? []).length} section{(p.cuts ?? []).length === 1 ? '' : 's'} deleted ·
        the finished video runs at most {clock(afterCuts(p, duration))} of {clock(duration)}
      </span>
    {/if}
    {#if picked.length >= 2}
      <span class="text-amber-300/90">blocks {pickedLabel} selected</span>
      {#if pickedCheck && !('error' in pickedCheck)}
        <Button size="xs" color="alternative" disabled={!!busy} onclick={mergePicked}>Merge into one</Button>
      {:else if pickedCheck && 'error' in pickedCheck}
        <span class="text-amber-300/70">{pickedCheck.error}</span>
      {/if}
      <button class="text-gray-500 underline hover:text-gray-300" onclick={() => (picked = [])}>clear</button>
    {:else}
      <span class="text-gray-600">Right-click the timeline to slice, delete a section, or add a block · shift-click blocks to merge them.</span>
    {/if}
    {#if selected?.audio && !selected.locked}
      <button class="ml-auto text-gray-400 underline hover:text-gray-200" disabled={!!busy}
              title="put back anything trimmed off the end of this block's take — the words are never cut short; if it runs long, later blocks move along"
              onclick={() => selected && useWholeTake(selected)}>
        {busy === `untrim:${selected.id}` ? 'restoring…' : `use the whole take (block ${blockLabel(selected)})`}
      </button>
      {#if untrimNote}<span class="text-emerald-300/90">{untrimNote}</span>{/if}
    {/if}
    {#if selected?.mergedFrom?.length}
      <span class="ml-auto text-gray-500">
        block {blockLabel(selected)} was merged from {selected.mergedFrom.map((m) => blockLabel(m)).join(' + ')}
      </span>
      <button class="text-gray-400 underline hover:text-gray-200" disabled={!!busy}
              onclick={() => selected && splitBack(selected.id)}>split back</button>
    {/if}
    {#if selected?.added}
      <span class="ml-auto text-gray-500">block {blockLabel(selected)} was added</span>
      {#if overrun(selected) > 0.05}
        {@const last = !p.segments.slice(p.segments.findIndex((x) => x.id === selected?.id) + 1).some((x) => !x.drop)}
        <span class="text-amber-300/90"
              title="shorten its text, or drag it earlier on the timeline">
          {#if last}
            its take runs {overrun(selected).toFixed(1)}s past the end of the picture — the video holds its last frame
          {:else}
            its take runs {overrun(selected).toFixed(1)}s past its space — what follows starts that much late
          {/if}
        </span>
      {/if}
      <button class="text-red-400/80 underline hover:text-red-300" disabled={!!busy}
              onclick={() => selected && removeBlock(selected)}>remove it</button>
    {/if}
  </div>
</div>

<div class="grid gap-4 xl:grid-cols-[minmax(0,380px)_minmax(0,1fr)]">
  <!-- the script, as a list -->
  <div class="max-h-[70vh] space-y-1 overflow-y-auto rounded-lg border border-gray-800 bg-gray-950 p-2">
    {#each p.segments as s (s.id)}
      <div class="flex items-center gap-1 rounded transition
                  {picked.includes(s.id) ? 'bg-amber-950/40 ring-1 ring-amber-600/70'
                    : selectedId === s.id ? 'bg-indigo-950 ring-1 ring-indigo-600' : 'hover:bg-gray-900'}">
        <button class="min-w-0 flex-1 px-2 py-1.5 text-left text-xs"
                onclick={(e) => {
                  if (e.shiftKey) { onpick(s.id, true); return; }
                  selectedId = s.id; seek(s.start); picked = [];
                }}>
          <div class="flex items-center gap-2 text-[10px] text-gray-500">
            <span class="rounded bg-gray-800 px-1 font-mono text-gray-300">{num(s)}</span>
            <span class="font-mono">{Math.floor(s.start / 60)}:{(s.start % 60).toFixed(0).padStart(2, '0')}</span>
            {#if s.locked}<span class="text-emerald-500">real</span>
            {:else if stale(s)}<span class="text-amber-500">stale</span>
            {:else if s.audio}<span class="text-indigo-400">rendered</span>
            {:else}<span class="text-gray-600">—</span>{/if}
            {#if s.terminalStep != null && s.terminalStep > 0.4}
              <span class="text-amber-400">↑</span>
            {/if}
          </div>
          <div class="truncate text-gray-300">{s.say}</div>
        </button>
        <button
          class="mr-1 shrink-0 rounded p-1.5 transition
                 {linePlaying === s.id ? 'bg-indigo-600 text-white'
                  : s.audio ? 'text-gray-500 hover:bg-gray-800 hover:text-indigo-300'
                  : 'text-gray-700'}"
          disabled={!s.audio}
          onclick={() => playLine(s)}
          title={!s.audio ? 'nothing rendered for this line yet'
                 : stale(s) ? 'hear the last render — the script has changed since'
                 : 'hear this line as rendered'}
          aria-label="hear this line">
          {#if linePlaying === s.id}
            <svg viewBox="0 0 10 10" class="h-2.5 w-2.5 fill-current"><rect x="2" y="2" width="6" height="6" /></svg>
          {:else}
            <svg viewBox="0 0 10 10" class="h-2.5 w-2.5 fill-current"><path d="M2.5 1.2 8 5l-5.5 3.8z" /></svg>
          {/if}
        </button>
      </div>
    {/each}
    {#if !p.segments.length}
      <p class="p-3 text-xs text-gray-500">Transcribe, then split into lines.</p>
    {/if}
  </div>

  <!-- one line at a time, with everything that applies to it -->
  <div class="space-y-4">
    {#if selected}
      <!-- NOT `bind:seg={p.segments[findIndex(...)]}`.
           A two-way binding to a computed index writes back through a setter
           that re-resolves that index AT WRITE TIME. Switching from one line to
           the next flushes the outgoing panel's value through it, by which
           point the index points at the line just selected — so the text typed
           into track 33 was written into track 34. The object is passed
           directly instead: the panel edits its fields in place, which is the
           same object already sitting in the array, so nothing has to be
           written back at all. -->
      {#key selected.id}
        <LineDetail seg={selected}
                    project={p} {busy} onsynth={synth} onseek={() => seek(selected.start)}
                    previewReady={!!previewSrc && !previewStale}
                    {nudge} oncontext={hearInContext} onsave={save} />
      {/key}
    {:else}
      <p class="text-sm text-gray-500">Pick a line from the list or the timeline.</p>
    {/if}

    <details class="rounded-lg border border-gray-800 bg-gray-950 p-4"
             bind:open={settingsOpen} bind:this={settingsEl}>
      <summary class="cursor-pointer text-sm font-semibold">Project defaults</summary>
      <div class="mt-4 space-y-4">
        <div class="rounded-lg border border-gray-800 bg-gray-900/40 p-3">
          <div class="mb-2 flex items-baseline justify-between">
            <span class="text-[11px] font-medium text-gray-300">Who narrates</span>
            <a href="/voices" class="text-[10px] text-indigo-400 underline">Voice lab</a>
          </div>
            <!-- What a render will actually do, in a sentence, because five
                 overlapping controls cannot be read as one setting and the
                 cost of guessing wrong is twenty minutes. -->
              <div class="mb-3 rounded border p-2 text-[11px] leading-relaxed
                          {bare ? 'border-emerald-900/60 bg-emerald-950/20 text-emerald-200'
                                : 'border-gray-800 bg-gray-900/40 text-gray-300'}">
                {#if bare && chosenVoice}
                  <strong>{chosenVoice.name}</strong>, no assist — the model
                  alone, with nothing carrying your accent, the room, or how
                  close you sat.
                  {#if !data.latest}
                    <span class="text-amber-400">
                      This voice has no trained version in use, so it will use
                      a sample anyway.</span>
                  {/if}
                {:else if bare}
                  <span class="text-amber-400">
                    No voice picked, so there is no training to work from —
                    this will use the sample below instead.</span>
                {:else if chosenVoice}
                  <strong>{chosenVoice.name}</strong>, assisted by a sample of
                  the real recording on every line.
                {:else}
                  Assisted by the sample below, on every line.
                {/if}
                {p.settings.targetWpm
                  ? `Paced to ${p.settings.targetWpm} wpm.`
                  : 'Spoken at its own pace.'}
                <button class="ml-1 underline decoration-dotted
                               hover:text-white disabled:opacity-40"
                        disabled={!!busy || !selectedId}
                        onclick={auditionTone}>hear one line first</button>
              </div>
              <Label class="mb-1 text-xs">Voice</Label>
              <Select size="sm" bind:value={p.settings.voice} onchange={save}
                      items={[{ value: '', name: 'No voice — use the reference below' },
                              ...(data.voices ?? [])
                                .filter((v: any) => v.kind !== 'project')
                                .map((v: any) => ({ value: v.id, name: v.name }))]} />
              <!-- Named for what the operator hears, not for what is switched
                   off. "Narrate from the model alone" read as "just send it
                   to the model", which is what anyone would want, when it
                   actually meant "give it no sample of my voice" - and the
                   result was a voice that had stepped back from the mic. -->
              <!-- The SAME word as the voice lab, for the same thing. The
                   render was assisting invisibly while the bench was not, so
                   a line that sounded perfect in one came back wrong in the
                   other and the difference looked like corruption. -->
              <div class="mt-3">
                <Toggle checked={!p.settings.noAssist} size="small"
                        onchange={(e: any) => {
                          p.settings.noAssist = !e.currentTarget.checked; save();
                        }}>
                  <span class="text-[11px]">Voice sample assist</span>
                </Toggle>
                <p class="mt-1 text-[10px] leading-relaxed text-gray-500">
                  Hands the model a few seconds of the real recording to
                  imitate on every line. That sample carries your accent, how
                  close you sat to the microphone, and the room — so it does
                  most of the work, and without it the voice drifts.
                  <span class="text-gray-400">Off is the model alone</span>,
                  which is what the training actually produced and is worth
                  hearing in the voice lab before trusting it here.
                  Changing this re-renders every line.
                </p>
              </div>
            {#if !p.settings.voice && (data.loras ?? []).length}
              <Label class="mb-1 text-xs">Fine-tune</Label>
              <Select size="sm" bind:value={p.settings.voxLora} onchange={save}
                      items={[{ value: '', name: 'Stock model (no fine-tune)' },
                              ...(data.loras ?? []).map((l: any) => ({ value: l.path, name: l.name }))]} />
              <p class="mb-3 mt-1 text-[10px] leading-relaxed text-gray-500">
                A fine-tune puts the voice in the MODEL, where a reference clip
                only lends it one line at a time. Measured over eight lines,
                the chosen checkpoint took pitch wander from
                <span class="text-gray-300">7.0 to 2.9</span> and the lines
                that came out wrong from two to none.
                <span class="text-amber-600/80">Later is not better:</span>
                across this run the model's sense of when to STOP degraded
                threefold while its voice kept improving, so a late checkpoint
                sounds right and runs on. Changing this re-renders every line.
              </p>
            {/if}
              <Label class="mb-1 text-xs">Direction</Label>
              <Input size="sm" placeholder="enthusiastic, energetic"
                     bind:value={p.settings.direction} onchange={save} />
              <p class="mb-3 mt-1 text-[10px] leading-relaxed text-gray-500">
                How the whole script should be delivered. A demo read straight
                is accurate and dull; this is the knob for that. Any line can
                overrule it below.
                <span class="text-amber-500">It costs fidelity:</span> the
                model refuses a style instruction alongside a reference
                transcript, so a direction switches from the clone that
                reproduces every nuance to the one that clones timbre only.
                Measured over five lines, that switch alone moves pitch
                6.6 Hz before any direction applies. Empty is the undirected
                path exactly — clearing this is the way back. Changing it
                re-renders every line.
              </p>
            <!-- A chosen voice brings its own sample; the file is only asked
                 for when narrating without one. -->
            {#if !bare && !p.settings.voice}
            <Label class="mb-1 text-xs">Whose voice</Label>
            <Select size="sm" value={voicePick} onchange={pickVoice}
                    items={voiceItems} />
            <p class="mt-1 text-[10px] leading-relaxed text-gray-500">
              The clone re-reads this file on EVERY line and copies what it
              hears — so whatever varies inside it varies across the whole
              video. A reference cut whole from one sitting holds steady; one
              stitched from clips mined across different recordings steps
              between them about once a second, and the render inherits every
              step.
            </p>
            {#if voicePick === 'custom'}
              <Input size="sm" class="mt-2" bind:value={p.settings.voiceRef} />
            {:else}
              <div class="mt-1 truncate font-mono text-[10px] text-gray-600">
                {p.settings.voiceRef || 'nothing chosen'}
              </div>
            {/if}
            {/if}
        </div>
        <div class="rounded-lg border border-gray-800 bg-gray-900/40 p-3">
          <div class="mb-2 text-[11px] font-medium text-gray-300">Voice tone</div>
          <Label class="mb-1 text-xs">
            Warmth · {(p.settings.warmth ?? 0) > 0 ? '+' : ''}{(p.settings.warmth ?? 0).toFixed(1)} dB
          </Label>
          <Range min="-6" max="10" step="0.5" bind:value={p.settings.warmth} />
          <p class="mb-3 mt-1 text-[10px] text-gray-600">
            A shelf at 300 Hz — the chest register that carries on a laptop. Adds
            body without touching pitch. Try +4 to +7; below about +3 it is subtle.
          </p>
          <Label class="mb-1 text-xs">
            Pitch · {(p.settings.pitch ?? 0) > 0 ? '+' : ''}{(p.settings.pitch ?? 0).toFixed(1)} semitones
          </Label>
          <Range min="-4" max="1.5" step="0.1" bind:value={p.settings.pitch} />
          <p class="mt-1 text-[10px] text-gray-600">
            A semitone is a big move on a voice — +3 was already chipmunk, so the
            range now stops at +1.5 and steps in tenths. Down is the useful
            direction: −0.5 is subtle, −1.5 noticeably deeper, −3 is a lot.
          </p>
          <Button size="xs" color="alternative" class="mt-3 w-full"
                  disabled={!!busy && busy !== 'tone'} onclick={auditionTone}>
            {busy === 'tone' ? 'Rendering a line…' : tonePlaying ? 'Stop' : 'Hear this on the selected line'}
          </Button>
          <p class="mt-1 text-[10px] text-gray-600">
            Re-renders the selected line with these settings. Everything else goes
            stale — Render all applies it to the whole script.
          </p>
        </div>

        <div class="rounded-lg border border-gray-800 bg-gray-900/40 p-3">
          <Toggle checked={p.settings.cleanBackground !== false} size="small"
                  onchange={(e: any) => {
                    p.settings.cleanBackground = e.currentTarget.checked; save();
                  }}>
            <span class="text-[11px]">Clean the background</span>
          </Toggle>
          <p class="mt-1 text-[10px] leading-relaxed text-gray-500">
            Takes the hiss out of the mix, then settles the pauses a little
            lower rather than to silence — the gentle version, which kept more
            of the voice than a hard gate. Applies to the preview and the final
            video; no line is re-rendered, only the preview is rebuilt.
          </p>
        </div>

        <div>          <Label class="mb-1 text-xs">
            Rate · {p.settings.targetWpm ? `${p.settings.targetWpm} wpm` : 'as it comes out'}
          </Label>
          <!-- Zero is a real choice, and there was no way to pick it. Forcing
               a rate means time-stretching every take that misses it, and a
               fifth either way is heard as the voice going strange - so
               "leave it alone" has to be reachable, not a number below the
               bottom of a slider. -->
          <div class="mb-1 flex items-center gap-2">
            <label class="flex items-center gap-1 text-[10px] text-gray-400">
              <input type="checkbox" class="accent-indigo-500"
                     checked={!p.settings.targetWpm}
                     onchange={(e: any) => {
                       p.settings.targetWpm = e.currentTarget.checked ? 0 : 190;
                       save();
                     }} />
              let it speak at its own pace
            </label>
          </div>
          <Range min="130" max="250" step="5" disabled={!p.settings.targetWpm}
                 bind:value={p.settings.targetWpm} />
          <p class="mt-1 text-[10px] leading-relaxed text-gray-600">
            One rate for the whole video, reached by stretching each take that
            misses it. A take a fifth off its target is heard as the voice
            going strange rather than as pacing, so leaving this off is often
            the better answer. Changing it restales every line.
          </p>
        </div>
        <!-- The dead air between lines, which is most of what "it drags" turns
             out to mean. A re-read is shorter than the sentence it replaces,
             so every line sits in a slot sized for the original: 495 seconds
             of silence across this video, against 169 after gliding at 3x.
             Nothing here restales a take — it is assembly only. -->
        <div>
          <Label class="mb-1 text-xs">
            Close gaps · {p.settings.gapSpeed ?? 3}× faster
          </Label>
          <Range min="1" max="8" step="0.5" bind:value={p.settings.gapSpeed}
                 onchange={save} />
          <p class="mt-1 text-[10px] leading-relaxed text-gray-600">
            How fast the picture runs through silence. Higher is tighter.
            Rebuild to hear it — no re-render.
          </p>
        </div>
        <div>
          <Label class="mb-1 text-xs">
            Leave alone under · {(p.settings.gapMin ?? 0.8).toFixed(1)}s
          </Label>
          <Range min="0.2" max="3" step="0.1" bind:value={p.settings.gapMin}
                 onchange={save} />
          <p class="mt-1 text-[10px] leading-relaxed text-gray-600">
            A pause shorter than this is a beat, not dead air, and is kept.
          </p>
        </div>
        <div>
          <Label class="mb-1 text-xs">Writing model</Label>
          <!-- The project's own choice, kept in the list even when it is not
               installed here, so opening a project never silently swaps it.
               New projects start with the studio's default (Settings). -->
          <Select size="sm" bind:value={p.settings.cleanModel} onchange={save}
                  items={[...new Set([p.settings.cleanModel, ...data.models].filter(Boolean))]
                    .map((m: string) => ({ value: m, name: m }))} />
        </div>
        <Button size="sm" class="w-full" onclick={save}>Save defaults</Button>
      </div>
    </details>
  </div>
</div>
{/if}
