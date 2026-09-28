<script lang="ts">
  import { Button, Input, Label, Alert, Card } from 'flowbite-svelte';
  import { untrack } from 'svelte';

  let { data } = $props();

  /** One text box per list, one entry per line — the lists are short and
      read more easily as text than as a stack of inputs. */
  const lines = (a: string[]) => a.join('\n');
  const list = (t: string) => t.split('\n').map((x) => x.trim()).filter(Boolean);

  const s0 = untrack(() => data.settings);
  let model = $state(s0.rewriteModel);
  let terms = $state(lines(s0.terms));
  let coined = $state(lines(s0.coined));
  let aliases = $state(Object.entries(s0.aliases).map(([h, m]) => `${h} → ${m}`).join('\n'));
  let mustSay = $state(lines(s0.mustSay));
  let testLines = $state(lines(s0.testLines));

  let busy = $state(false);
  let error = $state('');
  let saved = $state(false);

  /** Installed models, plus whatever is set now if it is not installed —
      a model can be named before it is pulled. */
  const installed = $derived(data.models ?? []);

  function parseAliases(t: string) {
    const out: Record<string, string> = {};
    for (const row of list(t)) {
      const [heard, meant] = row.split(/\s*(?:→|->|=)\s*/);
      if (heard && meant) out[heard.toLowerCase()] = meant;
    }
    return out;
  }

  async function save() {
    busy = true; error = ''; saved = false;
    try {
      const r = await fetch('/api/settings', {
        method: 'PUT', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({
          rewriteModel: model.trim(),
          terms: list(terms), coined: list(coined), aliases: parseAliases(aliases),
          mustSay: list(mustSay), testLines: list(testLines)
        })
      });
      const d = await r.json();
      if (!r.ok || d.error) throw new Error(d.error ?? `${r.status}`);
      saved = true;
    } catch (e: any) { error = e.message; } finally { busy = false; }
  }
</script>

<svelte:head><title>Settings · Voiceover</title></svelte:head>

<div class="mx-auto max-w-3xl space-y-4">
  <h1 class="text-base font-semibold">Settings</h1>

  <Card class="max-w-none border-gray-800 p-5" style="background:#111827">
    <h2 class="mb-1 text-sm font-semibold">Writing model</h2>
    <p class="mb-3 text-[11px] leading-relaxed text-gray-500">
      The model that rewrites and polishes scripts, via Ollama. New projects
      start with this one, and each project can choose its own in its
      settings. Any model Ollama can run — a local one keeps transcripts on
      this machine; a cloud one is faster and sends them out.
    </p>
    <Label class="mb-1 text-xs">Default model</Label>
    <Input bind:value={model} list="installed-models" placeholder="gemma4:12b" />
    <datalist id="installed-models">
      {#each installed as m}<option value={m}></option>{/each}
    </datalist>
    <p class="mt-1 text-[10px] text-gray-600">
      {#if installed.length}
        Installed: {installed.join(', ')}.
      {:else}
        Ollama did not answer, so the installed models are unknown.
      {/if}
      {#if model.trim() && installed.length && !installed.includes(model.trim())}
        <span class="text-amber-500">{model.trim()} is not installed yet — pull it before rewriting.</span>
      {/if}
    </p>
  </Card>

  <Card class="max-w-none border-gray-800 p-5" style="background:#111827">
    <h2 class="mb-1 text-sm font-semibold">Your words</h2>
    <p class="mb-3 text-[11px] leading-relaxed text-gray-500">
      The names your videos are made of. One per line.
    </p>

    <Label class="mb-1 text-xs">Product and place names</Label>
    <textarea bind:value={terms} rows="6"
              class="w-full rounded border border-gray-700 bg-gray-900 p-2 font-mono text-xs text-gray-100"></textarea>
    <p class="mb-4 mt-1 text-[10px] leading-relaxed text-gray-600">
      Spelled exactly. The transcriber is primed with them so it stops writing
      near-miss spellings, and every rewrite is told to restore a name the
      transcriber turned into an ordinary word. Keep it under about sixty.
    </p>

    <Label class="mb-1 text-xs">Coined names</Label>
    <textarea bind:value={coined} rows="3"
              class="w-full rounded border border-gray-700 bg-gray-900 p-2 font-mono text-xs text-gray-100"></textarea>
    <p class="mb-4 mt-1 text-[10px] leading-relaxed text-gray-600">
      Names whose spelling is exact, capitals included — polish flags any other
      spelling. Leave out ordinary words that are also product names; there the
      plain word is usually meant.
    </p>

    <Label class="mb-1 text-xs">What the transcriber hears instead</Label>
    <textarea bind:value={aliases} rows="3" placeholder="copyright → CopyWrite"
              class="w-full rounded border border-gray-700 bg-gray-900 p-2 font-mono text-xs text-gray-100"></textarea>
    <p class="mb-4 mt-1 text-[10px] leading-relaxed text-gray-600">
      <code>heard → meant</code>, one per line. Flagged in polish where it
      appears; never replaced without you seeing it.
    </p>

    <Label class="mb-1 text-xs">Must be said exactly</Label>
    <textarea bind:value={mustSay} rows="2"
              class="w-full rounded border border-gray-700 bg-gray-900 p-2 font-mono text-xs text-gray-100"></textarea>
    <p class="mt-1 text-[10px] leading-relaxed text-gray-600">
      A take is checked by transcribing it, and near-miss spellings are
      forgiven — which also forgives a mispronounced name. A name listed here
      has to come back spelled exactly, or the line is drawn again. Only list a
      name after checking the transcriber spells it right when it is said
      right: a homophone can never pass.
    </p>
  </Card>

  <Card class="max-w-none border-gray-800 p-5" style="background:#111827">
    <h2 class="mb-1 text-sm font-semibold">Voice test lines</h2>
    <p class="mb-3 text-[11px] leading-relaxed text-gray-500">
      What a voice says when its checkpoints are scored and its sample is
      chosen. Use sentences like your narration, with your own product names in
      them — those are the words a voice most needs to get right.
    </p>
    <textarea bind:value={testLines} rows="7"
              class="w-full rounded border border-gray-700 bg-gray-900 p-2 text-xs text-gray-100"></textarea>
  </Card>

  {#if error}<Alert color="red" class="text-xs">{error}</Alert>{/if}
  <div class="flex items-center gap-3">
    <Button disabled={busy} onclick={save}>{busy ? 'Saving…' : 'Save settings'}</Button>
    {#if saved}<span class="text-xs text-emerald-400">Saved.</span>{/if}
  </div>
</div>
