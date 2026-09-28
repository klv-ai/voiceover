# studio

The web app: SvelteKit 5, Tailwind, flowbite-svelte. It owns the projects and
the voices and shells out to `../narrate` for everything that touches audio or
video. Installing and running it: see the README at the repo root.

```bash
npm run dev      # hot reload on :5199
npm run check    # types
npm run build && npm start
```

## Where things are

| | |
|---|---|
| `src/routes/+page.svelte` | new project (one recording or several, joined) and the project list |
| `src/routes/p/[id]/` | the project page: script, timeline, masks, the four steps |
| `src/routes/voices/` | the voice lab: list, and each voice's page (read, train, compare, sample) |
| `src/routes/settings/` | studio settings: writing model, your words, voice test lines |
| `src/routes/api/` | JSON endpoints behind all of it |
| `src/lib/types.ts` | Project, Segment, Settings, and the rules the page and server share (spans, cuts, merges, masks) |
| `src/lib/server/pipeline.ts` | every call into narrate: analyze, rewrite, synthesise and check a line, assemble, render |
| `src/lib/server/run.ts` | child processes, and the two long-lived workers (synthesis, take checking) |
| `src/lib/server/jobs.ts` | long operations run detached; pages poll them |
| `src/lib/server/projects.ts`, `voices.ts`, `settings.ts` | the stores, all JSON under `data/` |
| `src/hooks.server.ts` | refuses unsafe ids and cross-site writes |

## Rules worth knowing before changing anything

**A take's signature is positional.** Every rendered line stores
`signature(segment, settings)` from `types.ts` as `renderedFrom`, and a line
is stale when the two differ. The signature is a JSON array; removing a slot,
or changing what one holds, marks every take in every project as stale. New
fields are appended, and only when they differ from their old value.
Retired settings keep their slots (`RETIRED`).

**Loaded settings are `DEFAULT_SETTINGS` under what the project saved.**
Changing a default changes every project that never saved that key. New
projects start from `NEW_PROJECT_SETTINGS` instead.

**Re-read before writing.** A route that waits on a model for minutes and
then saves the copy it loaded writes back stale settings and edits; use
`updateProject()`, which re-reads and mutates. `saveProject()` also refuses to
resurrect a deleted project and logs stale overwrites to
`data/projects/stale.log`.

**Nothing the operator made is erased.** Deleting a project or voice moves it
to `data/trash`.

**Paths from the client are never trusted.** `/api/media` serves only files
under the data folder or a project's own source; ids must be one path
segment (`lib/server/paths.ts`).
