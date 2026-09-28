import { sveltekit } from '@sveltejs/kit/vite';
import tailwindcss from '@tailwindcss/vite';
import { defineConfig } from 'vite';
export default defineConfig({
  plugins: [tailwindcss(), sveltekit()],
  server: {
    // `data/` holds the projects and every rendered wav, and it sits inside the
    // Vite root. Without this, saving a line or assembling a preview writes a
    // file the dev watcher is watching, and it FULL-RELOADS the page — which
    // reads as "every action takes us back to the start". Not SSR: the watcher.
    watch: { ignored: ['**/data/**'] }
  }
});
