<script lang="ts">
  import '../app.css';
  let { children } = $props();
</script>

<div class="min-h-screen bg-gray-900 text-gray-100">
  <header class="border-b border-gray-800 bg-gray-950/80 backdrop-blur sticky top-0 z-20">
    <div class="mx-auto max-w-[1600px] px-6 py-3 flex items-center gap-4">
      <a href="/" class="font-semibold tracking-tight text-lg">Voiceover</a>
      <span class="text-xs text-gray-500">
        master · script · synthesise · retime
      </span>
      <a href="/voices"
         class="ml-auto rounded border border-gray-700 px-2 py-1 text-xs text-gray-400
                transition hover:border-gray-500 hover:text-gray-200">Voice lab</a>
      <a href="/settings"
         class="rounded border border-gray-700 px-2 py-1 text-xs text-gray-400
                transition hover:border-gray-500 hover:text-gray-200">Settings</a>
    </div>
  </header>
  <main class="mx-auto max-w-[1600px] px-6 py-6">
    {@render children()}
  </main>
</div>
