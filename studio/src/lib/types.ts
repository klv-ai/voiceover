export type Segment = {
  id: string;
  start: number;          // source timeline
  end: number;
  heard: string;          // what whisper transcribed, verbatim
  say: string;            // what should be spoken (editable)
  sayOriginal: string;    // the mechanical rewrite, for revert
  /** What this line said before the last flow pass, so a single line can be
      put back without undoing the whole pass. */
  sayBeforeFlow?: string;
  /** The last text a machine pass (rewrite, flow) wrote into this line. A line
      whose `say` differs from it has been edited by hand — which the polish
      pass must know, to leave that wording alone. */
  sayMachine?: string;
  /** What this line said before a polish suggestion was taken, for undo. */
  sayBeforePolish?: string;
  /** A block the operator ADDED — narration over what was dead air in the
      recording, with nothing of his under it. Its id carries a letter after
      the block it follows ("s012a"), because renumbering the others would
      break every take, setting and suggestion keyed to them. */
  added?: boolean;
  /** The blocks this one was merged from, exactly as they were, so the merge
      can be split back — takes included, which are set aside, not deleted. */
  mergedFrom?: Segment[];
  cleaned: boolean;       // has the LLM touched it
  locked: boolean;        // keep the real recording for this one
  audio?: string;         // rendered wav, relative to /work
  duration?: number;
  wpm?: number;
  /** The rate the WORDS come out at, pauses excluded — what the ear follows.
      `wpm` divides by the whole file, so a line that ends on a beat reads as
      slow while sounding fast: three words in 1.17s with half of that silent
      scores 154 and articulates at 300. Both are kept, because the gross
      figure still decides whether a take fits its slot. */
  speechWpm?: number;
  terminalStep?: number;   // semitones the synthetic line ends above its body
  /** What the checker made of the last take, kept rather than discarded.
      Every take is already transcribed and compared to the script — the
      result was computed, reported once into a job log, and thrown away, so
      the only way to find a line that came out wrong was to listen to all of
      them. `check.score` is 1 when the take says the line exactly; `heard` is
      what the transcriber actually heard, which is usually enough to see what
      went wrong without playing anything. */
  check?: { ok: boolean; score: number | null; heard?: string; at: string };
  /** Signature of the text + knobs the current wav was generated from, so the
      UI can tell when what you are about to hear is not what you just set. */
  renderedFrom?: string;
  seed?: number;
  /** Seconds to shift this take inside its own gap: negative takes up dead
      space, positive lengthens the pause before it. Does not affect the
      rendered wav, only where it sits, so it is not part of `signature`. */
  offset?: number;
  /** Seconds of room this line KEEPS after its take, on top of the take's own
      length. A line's span in the original recording is whatever the operator
      happened to say there, and a re-read is often longer — track 25's slot is
      1.34s against a 3.02s read, so the block ends while the voice is still
      going and the next one is already due. `hold` lets the block own the
      space instead of shoving its neighbour along: the take is untouched and
      unstretched, and the renderer will not reclaim what is held. */
  hold?: number;
  /** Keep the silence AFTER this line at full length: a beat the operator
      wants. Everything else is reclaimable air, and the render glides the
      picture through it rather than cutting. */
  holdGap?: boolean;
  /** Give this line more (or less) room: a time factor applied to the take.
      1 is natural, 1.3 stretches it into the silence after it. Only ever what
      the gap allows — a stretch can never run into the next line. */
  stretch?: number;
  /** Leave this line out of the finished video: no take, no recording, and
      the renderer glides through its span like any other silence. */
  drop?: boolean;
  /** How THIS line should be delivered, when it differs from the script's own
      direction — "slower, conspiratorial", "land this one". Empty or absent
      falls through to the project setting, and both being empty is the
      undirected path every take in this project was rendered on. */
  direction?: string;
  /** Per-line overrides. Undefined means "use the project setting". */
  over?: {
    targetWpm?: number;
    terminal?: 'none' | 'fall';
  };
};

export type Settings = {
  targetWpm: number;
  /** Gaps shorter than this are left alone; longer ones are sped through
      unless held. And how fast the picture may glide. */
  gapMin: number;
  gapSpeed: number;
  /** Global voice tone. `pitch` is a small resample shift in semitones;
      `warmth` is a low shelf at 300 Hz in dB — the safer of the two, since it
      adds body without moving any pitch. */
  pitch: number;
  warmth: number;
  voiceRef: string;
  /** A LoRA fine-tune of the local model, as a checkpoint directory. Empty
      means the stock weights. Unlike a reference clip, this is the voice
      LEARNED rather than imitated, so it does not have to be re-derived on
      every line. */
  voxLora?: string;
  /** Which VOICE narrates this, by id from the voice lab. The voice carries
      its own promoted checkpoint, so a project follows whatever that voice
      currently is rather than pinning a path to a checkpoint directory. */
  voice?: string;
  /** Narrate from the MODEL alone: no reference clip, no channel match, no
      voice fit.
      Everything those passes do is compensation for a generator that derives
      the speaker afresh on every line — which is what a reference clip makes
      it do, and structurally what "block 68 is a different voice" and
      "95-100 is a different room" are. A voice trained on enough of its own
      speaker does not need any of it. Measured at 32 mined minutes this was
      WORSE (pitch spread 7.4 -> 11.5); it is worth trying again on a voice
      trained on fifty. */
  noAssist?: boolean;
  /** How the whole script should be delivered: "enthusiastic, energetic",
      "warm and unhurried". A demo read straight is accurate and dull, and this
      is the knob for that.

      NOT free: VoxCPM refuses a style instruction alongside a reference
      transcript, so a direction switches from the cloning mode that reproduces
      every nuance to the one that clones timbre only. Measured over five
      lines, the mode change alone moves pitch 6.6 Hz and the spectrum 0.8 dB
      before any direction is applied; "enthusiastic" then lifts pitch 11.7 Hz
      and shortens delivery, which is what enthusiasm is.

      Empty is the undirected path, byte for byte. Clearing this is the way
      back. */
  direction?: string;
  /** Build the mix from the takes alone rather than patching them over the
      original recording. Defaults ON once every line has a take: there is
      nothing of the screencast left to keep, and keeping it leaks through
      every seam. Switch it off to go back to patching — useful while only
      part of a video has been re-read. Does NOT restale takes: this is how
      they are assembled, not how they were made. */
  syntheticMix?: boolean;
  /** Take the hiss out of the assembled mix: RNNoise, then a GENTLE gate that
      settles the pauses 6 dB lower rather than to silence. Chosen by ear on
      full-length A/B videos — a deep gate cleared more but was heard pumping
      back in under the voice. Defaults ON (absent = on). Like syntheticMix it
      is how takes are assembled, so it does not restale them. */
  cleanBackground?: boolean;
  cleanModel: string;
};

export type Project = {
  id: string;
  name: string;
  source: string;
  createdAt: string;
  /** Bumped on every save. A page sends the rev it loaded; settings written
      against a stale rev are ignored, because a page that has not seen the
      current settings would otherwise revert them wholesale. */
  rev?: number;
  /** How far this project has got before it is usable.
      A new project is not a project yet: the video has to be transcribed, its
      fillers and stumbles found, and the script cut into lines. That takes
      minutes on a four-minute video, and until it is done there is nothing to
      show — so the state is recorded rather than inferred from which fields
      happen to be populated. */
  state?: 'building' | 'ready' | 'failed';
  buildError?: string;
  analysis?: string;
  duration?: number;      // seconds, from the analysis — not from a media event
  metrics?: Record<string, number>;
  settings: Settings;
  segments: Segment[];
  outputs: { master?: string; final?: string; synthetic?: string };
  /** The assembled track, and the mix it was built from. Stored so a reload
      restores what you were listening to instead of silently dropping it. */
  preview?: { path: string; sig: string; ts: number };
  /** Edit points made with the blade, in source seconds. They cut nothing on
      their own; they bound the sections that can be deleted. */
  slices?: number[];
  /** Sections deleted from the finished video, picture and sound together. */
  cuts?: Cut[];
  /** Parts of the picture blurred for a stretch of time. */
  masks?: Mask[];
  /** The recordings this video was joined from, in order, and where each
      begins in it (source seconds). Absent for a video uploaded whole. */
  parts?: { name: string; start: number; duration: number }[];
};

/**
 * A blurred rectangle over the picture, from `from` to `to` (source seconds).
 * Position and size are FRACTIONS of the frame (0-1), so a mask drawn on the
 * small preview lands on exactly the same pixels of the 4K original.
 */
export type Mask = { id: string; from: number; to: number; x: number; y: number; w: number; h: number };

/** Where a new mask starts, by the part of the screen it covers. */
export const MASK_PRESETS: Record<string, { label: string; x: number; y: number; w: number; h: number }> = {
  right: { label: 'the right side', x: 0.62, y: 0, w: 0.38, h: 1 },
  top: { label: 'a strip along the top', x: 0, y: 0, w: 1, h: 0.14 },
  bottom: { label: 'a strip along the bottom', x: 0, y: 0.86, w: 1, h: 0.14 },
  box: { label: 'a box to place', x: 0.35, y: 0.35, w: 0.3, h: 0.3 }
};

/** The masks showing at `t`. */
export function masksAt(p: { masks?: Mask[] }, t: number): Mask[] {
  return (p.masks ?? []).filter((m) => t >= m.from && t <= m.to);
}

/** A mask made sane: inside the frame, at least a sliver in size, and a
    span of at least a tenth of a second inside the video. */
export function tidyMask(m: Mask, total: number): Mask {
  const c = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, Number.isFinite(v) ? v : lo));
  const w = c(m.w, 0.02, 1), h = c(m.h, 0.02, 1);
  const x = c(m.x, 0, 1 - w), y = c(m.y, 0, 1 - h);
  const from = c(m.from, 0, Math.max(0, total - 0.1));
  const to = c(m.to, from + 0.1, Math.max(from + 0.1, total));
  const r = (v: number) => Math.round(v * 10000) / 10000;
  return { id: m.id, from: r(from), to: r(to), x: r(x), y: r(y), w: r(w), h: r(h) };
}


/** A deleted section, source seconds. `dropped` lists blocks that sat wholly
    inside it and went with it, so restoring the section brings them back. */
export type Cut = { from: number; to: number; dropped?: string[] };

/** What a rendered wav depends on. Anything here changing makes it stale. */
/**
 * Settings the studio no longer offers, at the values they defaulted to.
 *
 * Every take stores the signature it was made under, and the signature is a
 * POSITIONAL array that still has slots for them: Chatterbox's expressiveness
 * and CFG, pacing from the operator's own read, cloning from it, the engine,
 * the channel match, the voice fit. Dropping a slot, or changing what one
 * holds, marks every take in every project as made under other settings.
 * So the slots stay, filled from what each project saved over these
 * defaults — exactly what the loaded settings used to hold — and nothing
 * reads them for anything else.
 */
const RETIRED = {
  exaggeration: 0.7, cfgWeight: 0.4, paceFromRead: true, paceMin: 160, paceMax: 210,
  refFromRead: false, provider: 'chatterbox', voiceId: '', stability: 0.5, similarity: 0.8,
  channelMatch: 'source', t3Checkpoint: '', fitTakes: true
} as const;

export function signature(seg: Segment, st: Settings): string {
  const o: any = seg.over ?? {};
  const r: any = { ...RETIRED, ...st };
  const g: any = seg;
  return JSON.stringify([
    seg.say.trim(),
    seg.seed ?? null,
    o.targetWpm ?? st.targetWpm,
    o.exaggeration ?? r.exaggeration,
    o.cfgWeight ?? r.cfgWeight,
    o.terminal ?? 'none',
    r.paceFromRead ? Math.round((g.humanSpeech ?? g.humanDur ?? 0) * 100) : 0,
    r.paceFromRead ? [r.paceMin, r.paceMax] : 0,
    // Tone is applied at synthesis, so changing it must restale every take.
    st.pitch ?? 0,
    st.warmth ?? 0,
    // The reference IS the voice, so changing it restales everything.
    st.voiceRef,
    r.refFromRead ? (g.human ?? '') : '',
    // Anything APPENDED is appended only when it differs from its old value,
    // so adding a field never restales a take made before it existed.
    ...(r.provider && r.provider !== 'chatterbox'
      ? [r.provider, r.voiceId ?? '', r.stability ?? 0.5, r.similarity ?? 0.8]
      : []),
    ...(r.channelMatch && r.channelMatch !== 'source' ? ['ch', r.channelMatch] : []),
    ...(r.t3Checkpoint ? ['t3', r.t3Checkpoint] : []),
    ...(st.voxLora ? ['lora', st.voxLora] : []),
    ...(st.voice ? ['voice', st.voice] : []),
    ...(st.noAssist ? ['noassist'] : []),
    ...(r.fitTakes === false ? ['nofit'] : []),
    // A directed line is made differently, so it must restale — and only a
    // directed one. An undirected project appends nothing and is untouched.
    ...((seg.direction ?? st.direction ?? '').trim()
      ? ['dir', (seg.direction ?? st.direction ?? '').trim()] : [])
  ]);
}

/**
 * What a loaded project's settings are laid over: anything a project never
 * saved comes from here. Changing a value changes how every such project
 * renders — and restales its takes — so these stay as they were; a new
 * project starts from NEW_PROJECT_SETTINGS instead.
 */
export const DEFAULT_SETTINGS: Settings = {
  targetWpm: 210,
  gapMin: 0.8,
  gapSpeed: 3,
  pitch: 0,
  warmth: 0,
  voiceRef: '',
  voxLora: '',
  voice: '',
  noAssist: false,
  direction: '',
  cleanModel: 'gemma4:12b'
};

/**
 * Where a new project starts: the configuration that produced the best
 * videos. Each value differs from DEFAULT_SETTINGS for a measured reason —
 * a 210 wpm target time-stretched every line of a trained voice, and the
 * gentler gap speed left dead air on screen too long.
 */
export const NEW_PROJECT_SETTINGS: Settings = {
  ...DEFAULT_SETTINGS,
  targetWpm: 0,
  gapSpeed: 5
  // cleanModel: the studio's default writing model, set on the server when a
  // project is created (see newProjectSettings).
};


/** The number a block is spoken of by: "s031" is 31; an added block after it,
    "s031a", is 31a. Shared so the timeline, the script and every panel agree. */
export function blockLabel(s: { id: string }): string {
  const m = /^s(\d+)([a-z]*)$/.exec(s.id);
  return m ? String(Number(m[1])) + m[2] : s.id;
}

/**
 * Where a block sits against the picture: its take at its nudged position,
 * plus any room held after it — or, not rendered, the span of the recording.
 * The same arithmetic the timeline draws with, so the server and the page
 * agree on what space is free.
 */
export function blockSpan(s: Segment, _st?: Settings): [number, number] {
  const take = !!s.audio && !s.locked && !s.drop;
  const len = take ? s.duration ?? 0 : 0;
  const placed = len ? len * (s.stretch ?? 1) : s.end - s.start;
  const start = s.start + (take ? (s.offset ?? 0) : 0);
  return [start, start + placed + (take ? (s.hold ?? 0) : 0)];
}

/** The dead air around a moment, as [from, to] — or null if a block is
    playing then. A dropped block frees its space; everything else holds it. */
export function freeGapAt(p: { segments: Segment[]; settings: Settings }, at: number,
                          total: number): [number, number] | null {
  const spans = p.segments.filter((s) => !s.drop).map((s) => blockSpan(s, p.settings))
    .sort((a, b) => a[0] - b[0]);
  let lo = 0;
  for (const [a, b] of spans) {
    if (at < a) return [lo, a];
    if (at <= b) return null;
    lo = Math.max(lo, b);
  }
  return [lo, Math.max(total, lo)];
}

/** Breathing room kept either side of an added block, and the least a block
    may be. */
export const ADD_MARGIN = 0.15;
export const ADD_MIN = 0.8;

/**
 * Where a new block goes if asked for at `at`: inside the free gap there, a
 * margin clear of both neighbours, as long as its words need at a natural
 * pace (about 175 wpm) and no longer than the gap allows. Pure, so the route
 * and a test run the same rule.
 */
export function placeAdded(p: { segments: Segment[]; settings: Settings }, at: number, total: number,
                           say: string): { start: number; end: number; gap: [number, number] } | { error: string } {
  const gap = freeGapAt(p, at, total);
  if (!gap) return { error: 'a block is already playing there — pick some dead air' };
  const [lo, hi] = gap;
  if (hi - lo - 2 * ADD_MARGIN < ADD_MIN)
    return { error: `only ${(hi - lo).toFixed(1)}s of air there — not enough for a block` };
  const words = say.trim() ? say.trim().split(/\s+/).length : 0;
  const want = words ? words / 2.9 + 0.4 : 2.0;
  const start = Math.min(Math.max(at, lo + ADD_MARGIN), hi - ADD_MARGIN - ADD_MIN);
  const end = Math.min(hi - ADD_MARGIN, start + Math.max(ADD_MIN, want));
  return { start: +start.toFixed(3), end: +end.toFixed(3), gap };
}


// ---- Cutting the timeline --------------------------------------------------

/** How close (s) a right-click must be to a slice to act on that slice. */
export const SLICE_NEAR = 0.35;

/** The non-dropped block playing at `t`, if any. */
export function blockAt(p: { segments: Segment[]; settings: Settings }, t: number): Segment | null {
  for (const s of p.segments) {
    if (s.drop) continue;
    const [a, b] = blockSpan(s, p.settings);
    if (t > a && t < b) return s;
  }
  return null;
}

/** The deleted section containing `t`, if any. */
export function cutAt(p: { cuts?: Cut[] }, t: number): Cut | null {
  return (p.cuts ?? []).find((c) => t >= c.from && t <= c.to) ?? null;
}

/** The section `t` falls in: from the slice before it (or the start) to the
    slice after it (or the end) — what "delete this section" removes. */
export function sectionAt(p: { slices?: number[] }, t: number, total: number): [number, number] {
  const cuts = [...(p.slices ?? [])].sort((a, b) => a - b);
  let a = 0, b = total;
  for (const x of cuts) {
    if (x <= t) a = x;
    else { b = x; break; }
  }
  return [a, b];
}

/**
 * What deleting [a, b] would do. A section may not cut THROUGH a block —
 * half a sentence is never what anyone wants — so a block straddling either
 * edge refuses it, naming the block. Blocks wholly inside go with it.
 */
export function cutCheck(p: { segments: Segment[]; settings: Settings }, a: number, b: number):
    { error: string } | { inside: string[] } {
  const inside: string[] = [];
  for (const s of p.segments) {
    if (s.drop) continue;
    const [x, y] = blockSpan(s, p.settings);
    if (y <= a || x >= b) continue;
    if (x >= a && y <= b) inside.push(s.id);
    else return { error: `that would cut through block ${blockLabel(s)} — slice in the air either side of it` };
  }
  return { inside };
}

/** How long the finished video runs once the deleted sections are gone —
    before the dead air is glided, so an upper bound. */
export function afterCuts(p: { cuts?: Cut[] }, total: number): number {
  return total - (p.cuts ?? []).reduce((n, c) => n + Math.max(0, Math.min(c.to, total) - c.from), 0);
}


// ---- Merging adjacent blocks ------------------------------------------------

/** Whether the blocks `ids` can become one: two or more, next to each other in
    the script, none left out, and all kept as your voice or none of them. */
export function mergeCheck(p: { segments: Segment[] }, ids: string[]): { error: string } | { run: Segment[] } {
  const idx = [...new Set(ids)].map((id) => p.segments.findIndex((s) => s.id === id)).filter((i) => i >= 0)
    .sort((a, b) => a - b);
  if (idx.length < 2) return { error: 'pick two or more blocks — shift-click the second' };
  for (let k = 1; k < idx.length; k++)
    if (idx[k] !== idx[k - 1] + 1) return { error: 'only blocks next to each other can be merged' };
  const run = idx.map((i) => p.segments[i]);
  const gone = run.find((s) => s.drop);
  if (gone) return { error: `block ${blockLabel(gone)} is left out — bring it back or leave it out of the merge` };
  if (run.some((s) => !!s.locked) && !run.every((s) => !!s.locked))
    return { error: 'some of these keep your voice and some do not — make them match first' };
  return { run };
}

/**
 * One block from a run of adjacent ones: its span from the first's start to
 * the last's end, the words joined, and nothing rendered yet — the joined
 * line is said as one take. The originals ride along in `mergedFrom`.
 *
 * `sayMachine` is left unset on purpose. Joined, it could make a line the
 * operator had rewritten look machine-written, and the polish pass would then
 * treat his words as a draft.
 */
export function mergedBlock(run: Segment[]): Segment {
  const a = run[0], z = run[run.length - 1];
  const join = (f: (s: Segment) => string | undefined) =>
    run.map(f).map((t) => (t ?? '').trim()).filter(Boolean).join(' ');
  return {
    id: a.id, start: a.start, end: z.end,
    heard: join((s) => s.heard), say: join((s) => s.say), sayOriginal: join((s) => s.sayOriginal),
    cleaned: run.some((s) => s.cleaned), locked: !!a.locked,
    ...(a.direction ? { direction: a.direction } : {}),
    ...(a.over ? { over: a.over } : {}),
    ...(a.offset ? { offset: a.offset } : {}),
    ...(z.hold ? { hold: z.hold } : {}),
    ...(z.holdGap ? { holdGap: z.holdGap } : {}),
    ...(run.every((s) => s.added) ? { added: true } : {}),
    mergedFrom: run.map((s) => JSON.parse(JSON.stringify(s)))
  };
}
