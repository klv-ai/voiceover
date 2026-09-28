<script lang="ts">
  import { Button, Input, Label, Card, Alert } from 'flowbite-svelte';
  import { goto, invalidateAll } from '$app/navigation';

  let { data } = $props();
  /** The recordings, in the order they will be joined. One is the usual
      case; several is a video recorded in sittings. */
  let files = $state<File[]>([]);
  let name = $state('');
  let busy = $state(false);
  let error = $state('');
  let over = $state(false);
  /** Bytes sent, so a 97 MB screencast does not look like a hung button. */
  let sent = $state(0);
  const total = $derived(files.reduce((n, f) => n + f.size, 0));
  /** Which recording is going up, when there are several. */
  let sending = $state(0);
  let input: HTMLInputElement | null = $state(null);

  /** The project being built right now, and how it is getting on. */
  let building = $state<{ id: string; name: string } | null>(null);
  let job = $state<any>({ status: 'idle' });

  /** A filename is a starting point for a name, not the name. Suggest it, and
      stop suggesting the moment the operator types something of their own. */
  let touched = $state(false);
  const suggest = $derived(
    (files[0]?.name ?? '').replace(/\.[^.]+$/, '').replace(/[_-]+/g, ' ').trim()
  );
  $effect(() => { if (!touched) name = suggest; });

  const MB = (n: number) => (n / 1_048_576).toFixed(n > 10_485_760 ? 0 : 1) + ' MB';

  /**
   * Add recordings. Several chosen at once go in name order — screen
   * recordings are named by the time they were made, so that is the order
   * they were recorded in — after any already here. The arrows fix the rest.
   */
  function pick(list: FileList | File[] | null | undefined) {
    error = '';
    const got = [...(list ?? [])];
    const bad = got.filter((f) => !/\.(mov|mp4|m4v|mkv|webm|avi)$/i.test(f.name));
    if (bad.length) error = `${bad.map((f) => f.name).join(', ')}: not a video I can read — try .mov or .mp4`;
    const fresh = got.filter((f) => !bad.includes(f)
      && !files.some((x) => x.name === f.name && x.size === f.size))
      .sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true }));
    files = [...files, ...fresh];
    if (input) input.value = '';
  }

  function move(i: number, by: number) {
    const j = i + by;
    if (j < 0 || j >= files.length) return;
    const next = [...files];
    [next[i], next[j]] = [next[j], next[i]];
    files = next;
  }

  function remove(i: number) {
    files = files.filter((_, k) => k !== i);
  }

  /**
   * Send the files themselves.
   *
   * XHR rather than fetch, only because fetch still cannot report UPLOAD
   * progress — and a hundred-megabyte screencast going up over a slow link is
   * exactly when the operator needs to see that something is happening.
   */
  function send(url: string, f: File, before: number): Promise<any> {
    return new Promise((resolve) => {
      const xhr = new XMLHttpRequest();
      xhr.open('POST', url);
      xhr.upload.onprogress = (e) => { if (e.lengthComputable) sent = before + e.loaded; };
      xhr.onload = () => {
        let d: any = {};
        try { d = JSON.parse(xhr.responseText); } catch { /* handled below */ }
        resolve(xhr.status >= 400 || d.error ? { error: d.error ?? `upload failed (${xhr.status})` } : d);
      };
      xhr.onerror = () => resolve({ error: 'the upload did not reach the server' });
      xhr.send(f);
    });
  }

  async function create() {
    if (!files.length) return;
    busy = true; error = ''; sent = 0; sending = 0;
    let d: any;
    if (files.length === 1) {
      const q = new URLSearchParams({ filename: files[0].name, name: name.trim() });
      d = await send(`/api/projects/upload?${q}`, files[0], 0);
    } else {
      // One at a time, in order; the first is handed a batch id for the rest.
      let batch = '', before = 0;
      for (const [i, f] of files.entries()) {
        sending = i + 1;
        const q = new URLSearchParams({ filename: f.name, index: String(i + 1), ...(batch ? { batch } : {}) });
        const r = await send(`/api/projects/upload/part?${q}`, f, before);
        if (r.error) { d = { error: `${f.name}: ${r.error}` }; break; }
        batch = r.batch;
        before += f.size;
      }
      if (!d) {
        const r = await fetch('/api/projects/upload/join', {
          method: 'POST', headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ batch, name: name.trim() })
        });
        d = await r.json().catch(() => ({ error: `could not join (${r.status})` }));
      }
    }
    busy = false;
    if (d?.error) { error = d.error; return; }
    building = { id: d.id, name: d.name };
    poll();
  }

  /** Watch the build. It takes minutes — transcribing a four-minute video is
      most of it — so the page reports what it is doing rather than spinning. */
  async function poll() {
    if (!building) return;
    const r = await fetch(`/api/projects/${building.id}/build`);
    job = await r.json();
    if (job.status === 'running') { setTimeout(poll, 1500); return; }
    await invalidateAll();
  }

  /** Build a failed project again from what the server already has. */
  async function retry(id: string, projectName: string) {
    error = '';
    const r = await fetch(`/api/projects/${id}/build`, { method: 'POST' });
    const d = await r.json().catch(() => ({}));
    if (!r.ok || d.error) { error = d.error ?? `could not try again (${r.status})`; return; }
    building = { id, name: projectName };
    job = { status: 'running', step: 'starting', log: [] };
    await invalidateAll();
    poll();
  }

  /** The project waiting on "are you sure", and why a delete failed. */
  let confirming = $state<string | null>(null);
  let deleteError = $state('');

  async function deleteProj(id: string) {
    deleteError = '';
    const r = await fetch(`/api/projects/${id}`, { method: 'DELETE' });
    const d = await r.json().catch(() => ({}));
    if (!r.ok || d.error) { deleteError = d.error ?? `could not delete (${r.status})`; return; }
    confirming = null;
    if (building?.id === id) dismiss();
    await invalidateAll();
  }

  function open() {
    const id = building?.id;
    building = null;
    if (id) goto(`/p/${id}`);
  }

  function dismiss() {
    building = null;
    job = { status: 'idle' };
    files = []; name = ''; touched = false; sent = 0;
    if (input) input.value = '';
  }
</script>

<svelte:head><title>Voiceover</title></svelte:head>

<div class="grid gap-6 lg:grid-cols-[420px_1fr]">
  <Card class="max-w-none self-start border-gray-800 p-5" style="background:#111827">
    {#if building}
      <h2 class="mb-1 text-base font-semibold">{building.name}</h2>

      {#if job.status === 'running'}
        <p class="text-xs text-gray-500">Building. This takes a few minutes.</p>
        <div class="mt-3 h-1.5 w-full overflow-hidden rounded bg-gray-800">
          <div class="h-full bg-indigo-500 transition-all duration-500"
               style="width:{job.total ? Math.round((job.done / job.total) * 100) : 8}%"></div>
        </div>
        <p class="mt-2 text-sm text-gray-300">{job.step ?? 'starting'}</p>
        {#if job.log?.length > 1}
          <ul class="mt-2 space-y-0.5 text-[11px] text-gray-600">
            {#each job.log.slice(0, -1) as l}<li>{l}</li>{/each}
          </ul>
        {/if}
        <p class="mt-3 text-[11px] text-gray-600">
          You can leave this page — it keeps building, and the project appears in
          the list when it is done.
        </p>

      {:else if job.status === 'error'}
        <Alert color="red" class="mt-3 text-xs">{job.error}</Alert>
        {#if error}<Alert color="red" class="mt-2 text-xs">{error}</Alert>{/if}
        <Button class="mt-3 w-full" onclick={() => building && retry(building.id, building.name)}>
          Try again
        </Button>
        <p class="mt-1 text-[11px] text-gray-600">
          From what is already uploaded — nothing is sent again.
        </p>
        <button class="mt-2 w-full text-[11px] text-gray-500 underline hover:text-gray-300"
                onclick={dismiss}>choose different files instead</button>

      {:else}
        <p class="text-xs text-emerald-400">Ready.</p>
        <ul class="mt-2 space-y-0.5 text-[11px] text-gray-500">
          {#each (job.log ?? []).slice(-2) as l}<li>{l}</li>{/each}
        </ul>
        <Button class="mt-4 w-full" onclick={open}>Open {building.name}</Button>
        <button class="mt-2 w-full text-[11px] text-gray-500 underline hover:text-gray-300"
                onclick={dismiss}>Add another video instead</button>
      {/if}

    {:else}
      <h2 class="mb-3 text-base font-semibold">New project</h2>

      <!-- svelte-ignore a11y_no_static_element_interactions -->
      <div role="button" tabindex="0"
           class="flex cursor-pointer flex-col items-center justify-center rounded-lg border-2
                  border-dashed px-4 text-center transition {files.length ? 'py-3' : 'py-8'}
                  {over ? 'border-indigo-500 bg-indigo-950/30' : 'border-gray-700 hover:border-gray-500'}"
           ondragover={(e) => { e.preventDefault(); over = true; }}
           ondragleave={() => (over = false)}
           ondrop={(e) => { e.preventDefault(); over = false; pick(e.dataTransfer?.files); }}
           onclick={() => input?.click()}
           onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') input?.click(); }}>
        {#if files.length}
          <div class="text-xs text-indigo-400 underline">add another recording</div>
          <div class="mt-0.5 text-[11px] text-gray-600">it is joined on at the end</div>
        {:else}
          <div class="text-sm text-gray-300">Drop a video here</div>
          <div class="mt-1 text-xs text-gray-500">or click to choose one — or several, recorded in sittings</div>
          <div class="mt-2 text-[11px] text-gray-600">.mov .mp4 .m4v .mkv .webm</div>
        {/if}
      </div>
      <input bind:this={input} type="file" class="hidden" multiple
             accept="video/*,.mov,.mp4,.m4v,.mkv,.webm,.avi"
             onchange={(e) => pick((e.currentTarget as HTMLInputElement).files)} />

      {#if files.length}
        <ol class="mt-3 space-y-1">
          {#each files as f, i (f.name + f.size)}
            <li class="flex items-center gap-2 rounded border border-gray-800 bg-gray-900/60 px-2 py-1.5">
              <span class="w-4 shrink-0 text-right text-[11px] text-gray-500">{i + 1}</span>
              <span class="min-w-0 flex-1">
                <span class="block truncate text-xs text-gray-200" title={f.name}>{f.name}</span>
                <span class="text-[10px] text-gray-500">{MB(f.size)}</span>
              </span>
              {#if files.length > 1}
                <button class="px-1 text-xs text-gray-500 hover:text-gray-200 disabled:opacity-30"
                        disabled={busy || i === 0} title="earlier" onclick={() => move(i, -1)}>↑</button>
                <button class="px-1 text-xs text-gray-500 hover:text-gray-200 disabled:opacity-30"
                        disabled={busy || i === files.length - 1} title="later" onclick={() => move(i, 1)}>↓</button>
              {/if}
              <button class="px-1 text-xs text-gray-500 hover:text-rose-300 disabled:opacity-30"
                      disabled={busy} title="leave this one out" onclick={() => remove(i)}>✕</button>
            </li>
          {/each}
        </ol>
        {#if files.length > 1}
          <p class="mt-1 text-[11px] text-gray-600">
            Joined top to bottom into one video, then built as usual. Put them in
            the order they were recorded.
          </p>
        {/if}

        <Label class="mb-2 mt-3">Name it</Label>
        <Input bind:value={name} placeholder="What is this video?"
               oninput={() => (touched = true)} />
        <p class="mt-1 text-[11px] text-gray-600">
          What the video is, not what the file is called. This is how you will
          find it again once there are a dozen.
        </p>
      {/if}

      {#if error}<Alert color="red" class="mt-3 text-xs">{error}</Alert>{/if}

      {#if busy}
        <div class="mt-4">
          <div class="h-1.5 w-full overflow-hidden rounded bg-gray-800">
            <div class="h-full bg-indigo-500 transition-all"
                 style="width:{total ? Math.round((sent / total) * 100) : 0}%"></div>
          </div>
          <p class="mt-2 text-xs text-gray-400">
            {#if files.length > 1}Uploading recording {sending} of {files.length} —{:else}Uploading{/if}
            {MB(sent)} of {MB(total)}
          </p>
        </div>
      {:else}
        <Button class="mt-4 w-full" disabled={!files.length} onclick={create}>
          {files.length > 1 ? `Join ${files.length} recordings and build` : 'Upload and build'}
        </Button>
      {/if}
    {/if}
  </Card>

  <div>
    <h2 class="mb-3 text-base font-semibold">Your videos</h2>
    {#if !data.projects.length}
      <p class="text-sm text-gray-500">Nothing yet. Add a video on the left.</p>
    {:else}
      <div class="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
        {#each data.projects as p}
          {@const done = p.state !== 'building' && p.state !== 'failed'}
          <div class="relative">
          <svelte:element this={done ? 'a' : 'div'} href={done ? `/p/${p.id}` : undefined}
             class="block rounded-lg border p-4 transition
                    {done ? 'border-gray-800 bg-gray-950 hover:border-gray-600'
                          : p.state === 'failed' ? 'border-rose-900/60 bg-gray-950/60'
                          : 'pointer-events-none border-gray-900 bg-gray-950/60 opacity-70'}">
            <div class="flex items-start gap-2">
              <div class="min-w-0 flex-1 font-medium">{p.name}</div>
              {#if p.state === 'building'}
                <span class="shrink-0 rounded bg-indigo-900/60 px-1.5 py-0.5 text-[10px] text-indigo-200">building</span>
              {:else if p.state === 'failed'}
                <span class="shrink-0 rounded bg-rose-900/60 px-1.5 py-0.5 text-[10px] text-rose-200">failed</span>
              {/if}
            </div>
            <div class="mt-1 truncate text-xs text-gray-500">{p.source.split('/').pop()}</div>
            <div class="mt-3 flex flex-wrap gap-1.5 text-[11px]">
              {#if p.duration}
                <span class="rounded bg-gray-800 px-1.5 py-0.5">
                  {Math.floor(p.duration / 60)}:{String(Math.round(p.duration % 60)).padStart(2, '0')}
                </span>
              {/if}
              {#if p.metrics}
                <span class="rounded bg-gray-800 px-1.5 py-0.5">{p.metrics.wpm} wpm</span>
                <span class="rounded bg-gray-800 px-1.5 py-0.5">{p.metrics.hard_fillers} ums</span>
              {/if}
              {#if (p.parts?.length ?? 0) > 1}
                <span class="rounded bg-sky-900/60 px-1.5 py-0.5">{p.parts?.length} recordings</span>
              {/if}
              {#if p.segments?.length}
                <span class="rounded bg-indigo-900/60 px-1.5 py-0.5">{p.segments.length} lines</span>
              {/if}
              {#if p.segments?.some((s: any) => s.human)}
                <span class="rounded bg-emerald-900/60 px-1.5 py-0.5">your voice</span>
              {/if}
              {#if p.outputs?.final}<span class="rounded bg-emerald-700/70 px-1.5 py-0.5">rendered</span>{/if}
            </div>
            {#if p.state === 'failed'}
              {#if p.buildError}<p class="mt-2 text-[11px] text-rose-400">{p.buildError}</p>{/if}
              <Button size="xs" class="mt-2" disabled={!!building} onclick={() => retry(p.id, p.name)}>
                Try again
              </Button>
            {/if}
          </svelte:element>
          <!-- Outside the link, so asking to delete never opens the project. -->
          {#if confirming === p.id}
            <div class="absolute inset-0 flex flex-col justify-center rounded-lg border border-rose-900
                        bg-gray-950/95 p-4 text-center">
              <div class="text-sm text-gray-200">Delete {p.name}?</div>
              <p class="mt-1 text-[11px] text-gray-500">
                Its script, takes and renders go to the trash folder on the
                server, not erased.
              </p>
              {#if deleteError}<p class="mt-1 text-[11px] text-rose-400">{deleteError}</p>{/if}
              <div class="mt-3 flex justify-center gap-2">
                <Button size="xs" color="red" onclick={() => deleteProj(p.id)}>Delete</Button>
                <Button size="xs" color="alternative"
                        onclick={() => { confirming = null; deleteError = ''; }}>Keep it</Button>
              </div>
            </div>
          {:else}
            <button class="absolute bottom-2 right-2 rounded px-1.5 py-0.5 text-[10px] text-gray-600
                           hover:bg-rose-950/60 hover:text-rose-300"
                    title="Delete this project"
                    onclick={() => { confirming = p.id; deleteError = ''; }}>delete</button>
          {/if}
          </div>
        {/each}
      </div>
    {/if}
  </div>
</div>
