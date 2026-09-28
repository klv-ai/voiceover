import { homedir } from 'node:os';
import { join, resolve } from 'node:path';
import { env } from 'node:process';

/**
 * Where everything is.
 *
 * Nothing here is an absolute path any more. It used to be — one string naming
 * one directory on one Mac — which was fine right up until the work moved to a
 * Linux box with a GPU, at which point the studio started up perfectly and
 * could not find a single thing it needed.
 *
 * `narrate` is a SIBLING of `studio` in this repo, so it is derived from the
 * studio's own location rather than declared. That holds on any machine and in
 * any checkout, including two checkouts on the same machine.
 */
export const STUDIO = env.STUDIO_DIR ? resolve(env.STUDIO_DIR) : process.cwd();

/** The narrate repo drives everything; the studio is only a face for it. */
export const NARRATE = env.NARRATE_DIR
  ? resolve(env.NARRATE_DIR)
  : resolve(STUDIO, '..', 'narrate');

/**
 * Two interpreters on purpose. Analysis needs the stdlib and numpy and starts
 * in milliseconds; the voice model needs torch and a GPU stack that should not
 * share an environment with anything. Separate venvs, so installing one can
 * never break the other.
 */
export const PY = env.NARRATE_PY || join(NARRATE, '.venv/bin/python3');
export const PY_TTS = env.NARRATE_PY_TTS || join(NARRATE, '.venv-tts/bin/python3');
export const CLI = join(NARRATE, 'bin/narrate');

export const DATA = env.STUDIO_DATA ? resolve(env.STUDIO_DATA) : join(STUDIO, 'data');
export const PROJECTS = join(DATA, 'projects');
export const WORK = join(DATA, 'work');
/** Where uploaded video lands. Separate from `work`, which is derived output
    and can be thrown away and rebuilt — this is the source, and losing it
    loses the project. */
export const MEDIA = join(DATA, 'media');

/** Where Ollama answers — the models that rewrite and polish scripts. Which
    model is a studio setting (Settings page), not configuration. */
export const OLLAMA = env.OLLAMA_URL || 'http://localhost:11434';

/** The studio's settings file, told to every child process: the pipeline's
    Python side reads product terms and must-say names from it. */
env.VOICEOVER_SETTINGS = env.VOICEOVER_SETTINGS || join(DATA, 'settings.json');
export const HOME = homedir();
