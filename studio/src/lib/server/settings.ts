import { existsSync, readFileSync, renameSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { DATA } from './config';
import { NEW_PROJECT_SETTINGS, type Settings } from '$lib/types';

/**
 * Settings for the whole studio, as opposed to one project: which model
 * writes, and the words this studio's videos are made of.
 *
 * Kept in data/settings.json. The pipeline's Python side reads the same file,
 * found through VOICEOVER_SETTINGS (set in config.ts, so every child process
 * inherits it), which is how a product name reaches the transcriber, the
 * rewriters and the take checker without being written into any of them.
 */
export type StudioSettings = {
  /** The model new projects rewrite and polish with, and the default for
      every other writing step (read scripts for a voice). Any model Ollama
      can run; a project can still pick its own. */
  rewriteModel: string;
  /** Product and place names, spelled exactly. Primed into the transcriber so
      it stops writing near-miss spellings, and handed to every rewriter so it
      restores a name the transcriber turned into an ordinary word. */
  terms: string[];
  /** Coined names whose spelling is exact, case included: polish flags any
      other spelling. Ordinary words that are also product names ("Ask") do
      not belong here — the lowercase word is usually meant. */
  coined: string[];
  /** What the transcriber writes for a coined name: "copyright" for
      "CopyWrite". Flagged where it appears, never replaced blind. */
  aliases: Record<string, string>;
  /** Names a take must be HEARD spelled exactly, or be generated again. Only
      list a name after checking the transcriber spells it right when it is
      said right — a homophone can never pass. */
  mustSay: string[];
  /** Lines a voice is tested with: scoring checkpoints and choosing a
      sample. Put your own product names in them — they are the words the
      voice most needs to get right. */
  testLines: string[];
};

export const DEFAULTS: StudioSettings = {
  rewriteModel: 'gemma4:12b',
  terms: [],
  coined: [],
  aliases: {},
  mustSay: [],
  testLines: [
    'Hello, and welcome to this walkthrough.',
    'Let me show you what happens when we send this to the model.',
    'It takes a moment, so I will pause here and let it finish.',
    'Let\'s highlight a section and right-click. A context menu opens with a number of options.',
    'We often use this for grants and other long proposals.',
    'This is running on a laptop with sixty-four gigabytes of memory.',
    'These are long documents, and the point is that you never have to read one twice.'
  ]
};

/** config.ts sets VOICEOVER_SETTINGS (to data/settings.json unless overridden). */
export const SETTINGS_FILE = process.env.VOICEOVER_SETTINGS || join(DATA, 'settings.json');

const words = (v: unknown) =>
  (Array.isArray(v) ? v : []).map((x) => String(x).trim()).filter(Boolean);

/** What is saved, over the defaults — a missing file or key is a default. */
export function studioSettings(): StudioSettings {
  let saved: Partial<StudioSettings> = {};
  try {
    if (existsSync(SETTINGS_FILE)) saved = JSON.parse(readFileSync(SETTINGS_FILE, 'utf8'));
  } catch { /* an unreadable file reads as defaults rather than breaking every page */ }
  return clean({ ...DEFAULTS, ...saved });
}

/** Only well-formed values, whatever was sent. */
function clean(s: Partial<StudioSettings>): StudioSettings {
  const aliases: Record<string, string> = {};
  for (const [k, v] of Object.entries(s.aliases ?? {})) {
    const heard = String(k).trim().toLowerCase(), meant = String(v).trim();
    if (heard && meant) aliases[heard] = meant;
  }
  return {
    rewriteModel: String(s.rewriteModel ?? '').trim() || DEFAULTS.rewriteModel,
    terms: [...new Set(words(s.terms))],
    coined: [...new Set(words(s.coined))],
    aliases,
    mustSay: [...new Set(words(s.mustSay))],
    testLines: words(s.testLines).length ? words(s.testLines) : DEFAULTS.testLines
  };
}

/** Merge a change in and write it whole (tmp + rename). */
export function saveStudioSettings(patch: Partial<StudioSettings>): StudioSettings {
  const next = clean({ ...studioSettings(), ...patch });
  const tmp = `${SETTINGS_FILE}.${process.pid}.tmp`;
  writeFileSync(tmp, JSON.stringify(next, null, 2));
  renameSync(tmp, SETTINGS_FILE);
  writeLexicon(next);
  return next;
}

/** The terms as the one-per-line file the transcriber and rewriters take. */
export const LEXICON_FILE = join(DATA, 'lexicon.txt');
export function writeLexicon(s: StudioSettings = studioSettings()) {
  writeFileSync(LEXICON_FILE,
    '# Written from Settings → Product terms; edit it there.\n' + s.terms.join('\n') + '\n');
  return LEXICON_FILE;
}

/** Where a new project starts: the recommended setup, rewriting with the
    studio's chosen model. */
export function newProjectSettings(): Settings {
  return { ...NEW_PROJECT_SETTINGS, cleanModel: studioSettings().rewriteModel };
}
