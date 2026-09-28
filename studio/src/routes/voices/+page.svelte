<script lang="ts">
  import { Button, Input, Label, Alert, Card } from 'flowbite-svelte';
  import { goto, invalidateAll } from '$app/navigation';

  let { data } = $props();

  /**
   * The voice lab: the voices, and a way to start a new one.
   *
   * A voice is made on its own page — record reads, train, promote a
   * checkpoint, adopt a sample — so this page only names one and lists them.
   * Project pools (a project's own pooled read) are not voices you work on and
   * are left to the project that owns them.
   */
  const voices = $derived((data.voices ?? []).filter((v: any) => v.kind !== 'project'));
  let name = $state('');
  let busy = $state(false);
  let error = $state('');

  const media = (p: string) => `/api/media?path=${encodeURIComponent(p)}`;
  const dB = (v: number | undefined) => (v == null ? '—' : `${v.toFixed(1)} dB`);

  async function create() {
    busy = true; error = '';
    try {
      const r = await fetch('/api/voices', {
        method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ name: name.trim() })
      });
      const d = await r.json();
      if (!r.ok || d.error) throw new Error(d.error ?? `${r.status}`);
      await goto(`/voices/${d.voice.id}`);
    } catch (e: any) { error = e.message; } finally { busy = false; }
  }

  async function drop(id: string, label: string) {
    if (!confirm(`Delete the voice "${label}"? Its reads, training runs and sample go to the trash folder on the server.`)) return;
    try {
      const r = await fetch(`/api/voices?id=${encodeURIComponent(id)}`, { method: 'DELETE' });
      const d = await r.json();
      if (!r.ok || d.error) throw new Error(d.error ?? `${r.status}`);
      await invalidateAll();
    } catch (e: any) { error = e.message; }
  }
</script>

<svelte:head><title>Voices · Voiceover</title></svelte:head>

<div class="grid gap-6 lg:grid-cols-[360px_1fr]">
  <Card class="max-w-none self-start border-gray-800 p-5" style="background:#111827">
    <h2 class="mb-3 text-base font-semibold">New voice</h2>
    <Label class="mb-2">Whose voice is it?</Label>
    <Input bind:value={name} placeholder="A name for this speaker"
           onkeydown={(e: KeyboardEvent) => { if (e.key === 'Enter' && name.trim()) create(); }} />
    <Button class="mt-3 w-full" disabled={busy || !name.trim()} onclick={create}>
      {busy ? 'Creating…' : 'Create and open it'}
    </Button>
    <ol class="mt-4 list-decimal space-y-1 pl-5 text-[11px] leading-relaxed text-gray-500">
      <li>Record reads on its page — the more, the steadier the voice. Around
        half an hour is where training starts to pay.</li>
      <li>Train it, then score the checkpoints and promote the best.</li>
      <li>Promoting finds a sample to hand the model on every line; listen,
        and adopt one.</li>
      <li>Pick the voice in a project's settings.</li>
    </ol>
    {#if error}<Alert color="red" class="mt-3 text-xs">{error}</Alert>{/if}
  </Card>

  <div>
    <h2 class="mb-3 text-base font-semibold">Voices</h2>
    {#if !voices.length}
      <p class="text-sm text-gray-500">None yet. Name one on the left.</p>
    {/if}
    <div class="space-y-2">
      {#each voices as v (v.id)}
        <div class="rounded-lg border border-gray-800 bg-gray-950 p-3">
          <div class="flex items-baseline gap-3">
            <a href="/voices/{v.id}"
               class="truncate text-sm font-medium text-indigo-300 underline
                      decoration-indigo-800 hover:text-indigo-200">{v.name}</a>
            <button class="ml-auto text-[10px] text-gray-600 underline hover:text-red-400"
                    onclick={() => drop(v.id, v.name)}>delete</button>
          </div>
          {#if v.reference}
            <div class="mt-1 flex flex-wrap gap-x-4 gap-y-0.5 text-[11px] text-gray-500">
              <span>{v.stats?.seconds?.toFixed(1) ?? '—'}s sample</span>
              <span>floor {dB(v.stats?.floor_db)}</span>
              <span class="text-gray-400">SNR {dB(v.stats?.snr_db)}</span>
              {#if v.stats?.holes}
                <span class="text-amber-500"
                      title="Stretches of true digital silence inside the sample.">
                  {v.stats.holes} silent {v.stats.holes === 1 ? 'hole' : 'holes'} · {v.stats.hole_s}s
                </span>
              {/if}
            </div>
            <audio class="mt-2 h-8 w-full" controls preload="none" src={media(v.reference)}></audio>
          {:else}
            <p class="mt-1 text-[11px] text-gray-500">
              No sample yet — record, train and find one on its page.
            </p>
          {/if}
        </div>
      {/each}
    </div>
  </div>
</div>
