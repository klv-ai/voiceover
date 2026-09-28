import { json } from '@sveltejs/kit';
import { existsSync, mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { run, synthWorker } from '$lib/server/run';
import { NARRATE, PY, PY_TTS, OLLAMA, DATA } from '$lib/server/config';
import { VOICES, getVoice, latestOf } from '$lib/server/voices';
import { studioSettings } from '$lib/server/settings';

/**
 * Hear this voice say something.
 *
 * Choosing a checkpoint is a judgement made by listening, and until now that
 * listening was done by copying wavs off a machine. A voice you cannot audition
 * from its own page is a voice whose checkpoints get chosen on loss curves -
 * which is how a fine-tune that had forgotten how to stop was promoted, and
 * how a whole video came back full of noise the metrics could not see.
 */
export const POST = async ({ params, request }) => {
  const body = await request.json().catch(() => ({}));
  const voice = getVoice(params.id);
  if (!voice) return json({ error: 'no such voice' }, { status: 404 });

  let text = (body?.text ?? '').toString().trim();

  // Ask the writer for a line, when the operator would rather listen than type.
  if (!text && body?.about) {
    const r = await run(PY, [join(NARRATE, 'lab/read_script.py'),
                             '-o', '/tmp/say_one.json',
                             '--model', (body?.model || studioSettings().rewriteModel).toString(),
                             '--ollama', OLLAMA, '--passages', '1',
                             '--words', '35',
                             '--language', (body?.language || 'English').toString(),
                             '--words-to-learn', body.about.toString().slice(0, 200)],
                        { cwd: NARRATE, timeout: 300_000 });
    try {
      const d = JSON.parse(
        (await import('node:fs')).readFileSync('/tmp/say_one.json', 'utf8'));
      text = d?.passages?.[0]?.text ?? '';
    } catch { /* fall through to the error below */ }
    if (!text) return json({ error: `could not write a line:\n${r.err.slice(-400)}` },
                           { status: 500 });
  }
  if (!text) return json({ error: 'nothing to say' }, { status: 400 });

  // `latest` unless asked otherwise, and '' means the stock weights on purpose:
  // comparing a checkpoint against stock is the comparison that matters.
  const lora = body?.checkpoint === '' ? null
             : (body?.checkpoint ?? latestOf(params.id) ?? null);

  // The bench has to render the way the project will, or it flatters.
  //
  // It cloned from the reference with no pace target while a project was set
  // to narrate from the model alone at 190 wpm - two different modes - so the
  // same line sounded right here and wrong in the video, and the difference
  // read as the render "injecting" something. It was not injecting anything;
  // it was doing something else.
  const bare = !!body?.noAssist;
  const wpm = Number(body?.targetWpm) || 0;

  const out = join(DATA, 'work', '_bench');
  mkdirSync(out, { recursive: true });
  const wav = join(out, `${params.id}_${Date.now()}.wav`);

  await synthWorker.start(PY_TTS, join(NARRATE, 'lab/synth_one.py'));
  const r: any = await synthWorker.send({
    text,
    provider: 'voxcpm',
    voice_ref: voice.reference,
    output: wav,
    // Whatever the caller is judging against. Zero is the model unregulated;
    // a project's own target is what that project will actually do.
    target_wpm: wpm,
    seed: Number(body?.seed) || undefined,
    voice_opts: {
      ...(lora ? { lora } : {}),
      ...(bare && lora ? { no_prompt: true } : {}),
      ...(body?.direction ? { direction: body.direction.toString() } : {})
    }
  });
  if (!existsSync(wav)) return json({ error: 'nothing was generated' }, { status: 500 });
  return json({ text, path: wav, seconds: r?.duration,
                speechWpm: r?.speech_wpm, usedCheckpoint: lora ?? '',
                mode: bare ? 'model alone' : 'cloned from the reference',
                targetWpm: wpm });
};
