import { json } from '@sveltejs/kit';
import { spawn } from 'node:child_process';
import { existsSync, statSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { getProject, workDir } from '$lib/server/projects';

/** Decode to mono PCM and reduce it to one magnitude per column.
 *
 * 8 kHz is deliberate: this is a picture of where the speech is, not an
 * oscilloscope, and it keeps a ten minute file to a few megabytes over the
 * pipe. Peaks are taken per bucket rather than averaged, so a short consonant
 * still shows up as a mark instead of being smoothed into the noise floor. */
function decode(path: string, columns: number): Promise<{ peaks: number[]; duration: number }> {
  const RATE = 8000;
  return new Promise((resolve, reject) => {
    const ff = spawn('ffmpeg', ['-v', 'error', '-i', path, '-map', '0:a:0', '-vn',
                                '-ac', '1', '-ar', String(RATE), '-f', 's16le', '-']);
    const chunks: Buffer[] = [];
    let err = '';
    ff.stdout.on('data', (d) => chunks.push(d));
    ff.stderr.on('data', (d) => (err += d));
    ff.on('close', (code) => {
      const buf = Buffer.concat(chunks);
      if (code !== 0 || buf.length < 2) return reject(new Error(err.slice(-500) || 'no audio'));
      const n = buf.length >> 1;
      const per = Math.max(1, Math.floor(n / columns));
      const peaks: number[] = [];
      for (let c = 0; c < columns; c++) {
        const a = c * per, b = Math.min(n, a + per);
        let hi = 0;
        for (let i = a; i < b; i++) {
          const v = Math.abs(buf.readInt16LE(i << 1));
          if (v > hi) hi = v;
        }
        peaks.push(Math.round((hi / 32768) * 1000) / 1000);
      }
      resolve({ peaks, duration: n / RATE });
    });
    ff.on('error', reject);
  });
}

export const GET = async ({ params, url }) => {
  const p = getProject(params.id);
  if (!p) return json({ error: 'not found' }, { status: 404 });
  const work = workDir(p.id);
  const track = url.searchParams.get('track') === 'preview' ? 'preview' : 'original';
  const columns = Math.min(4000, Math.max(200, Number(url.searchParams.get('columns')) || 1400));

  const path = track === 'preview' ? join(work, 'preview.wav') : (p.outputs.master ?? p.source);
  if (!path || !existsSync(path)) return json({ error: `no ${track} audio yet` }, { status: 404 });

  // Reducing ten minutes of audio takes a second or two, and the strip is
  // asked for again on every rebuild and every reload. Key the cache on the
  // file's own mtime so a re-assembled preview invalidates itself.
  const key = join(work, `peaks.${track}.${columns}.${statSync(path).mtimeMs}.json`);
  if (existsSync(key)) return json(JSON.parse(readFileSync(key, 'utf8')));
  try {
    const d = await decode(path, columns);
    writeFileSync(key, JSON.stringify(d));
    return json(d);
  } catch (e: any) {
    return json({ error: e.message }, { status: 500 });
  }
};
