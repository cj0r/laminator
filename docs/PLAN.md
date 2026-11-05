# Plan: web front end and Docker

Goal: the same tool, controlled from a browser, shipped as one Docker container. The CLI stays (useful for
scripting and debugging); the UI is a second client of the same core.

## What already helps
- Core logic lives in importable modules (`server`, `mdblist`, `render`, `store`, `meta`). `meta` already returns
  structured results (`ItemPlan`, `Change`, `Result`) instead of printing.
- State is one SQLite file plus an `originals/` folder; templates and overrides are plain YAML.
- Safety rules (scope guard, hand-edit detection, read-back verification, restore) are in code, not in the CLI.

## What has to change first (phase 0, no UI yet) — DONE except item 4 (multi-server config) and the Dockerfile
1. **[done] Poster actions into the core.** `cmd_apply`/`cmd_render`/`cmd_restore` still print and live in `cli.py`. Move them to
   an `actions` module that yields events (`applied`, `skipped`, `lost`, `new poster adopted`, `error`) like `meta` does.
   CLI and web then render the same events.
2. **[done] One data directory.** Paths are currently relative to the working directory (`state.db`, `originals/`, `cache/`, `out/`,
   `templates/`, `fonts/`). Introduce `LAMINATOR_DATA` (default `.`; `/data` in Docker) and resolve everything from it.
3. **[done] Fonts.** The default template uses bundled Anton. Templates that name system fonts (`~/.local/share/fonts/impact.ttf`,
   Nimbus Sans) will not exist in a container. Implemented: fonts and templates resolve from `<data>/fonts|templates` first, then the copies bundled in the package. The UI font picker lists those.
4. **Config.** Server list in `/data/config.yaml` (multiple servers later); secrets only from env / Docker secrets, never
   written to disk by the app and never sent to the browser.
5. **[done] Concurrency.** One job at a time (Emby and MDBList are not meant to be hammered); SQLite in WAL mode; a lock so a
   scheduled run and a manual run cannot overlap.

## Backend
- **FastAPI**, one process. Endpoints are thin wrappers over `actions`/`meta`:
  `GET /api/libraries`, `GET /api/items`, `POST /api/render/preview` (returns a JPEG, writes nothing),
  `POST /api/apply`, `POST /api/restore`, `GET/PUT /api/templates/{name}`, `GET/PUT /api/overrides`,
  `POST /api/meta/diff`, `POST /api/meta/apply`, `GET /api/jobs/{id}` + `/events` (server-sent events for progress).
- **Jobs**: apply/restore/meta-apply run as background jobs with a live event stream; the UI shows per-item results.
- **Scheduler**: a "run daily at HH:MM for these libraries" setting (APScheduler or a small loop). Idempotency already makes
  repeat runs cheap; poster-change detection already makes them safe when the user swaps a poster in Emby.
- **Auth**: the UI can write to a media server, so it must not be open. v1: a single admin password from an env var
  (hashed session cookie); alternatively trust a reverse proxy. Never expose it unauthenticated.

## UI (screens)
1. **Setup**: server URL, test connection, pick libraries. Keys are read from the environment; the UI only shows "set / not set".
2. **Library**: poster grid, original vs. current, state badges (original / overlay / needs re-apply / new poster adopted).
3. **Per-item score override**: an editable score on the item's detail page (writes the `score:` entry in `overrides.yaml`), shown next to the MDBList value and the live preview.
3b. **Template editor**: live preview on a real poster. Sliders for gradient height/opacity, score size, margins, font picker,
   source order. Saves to YAML, so the file stays the source of truth.
4. **Run**: choose scope (library / selection), see a dry-run preview first, then apply. No "everything" button without a typed
   confirmation (the CLI's `--all` rule, kept).
5. **Metadata**: overrides editor with validation, diff view (`field: old -> new`), hand-edit conflicts highlighted with
   keep/overwrite choices, apply, restore.
6. **History**: what changed when, one-click restore per item or all.

Frontend tech: server-rendered pages with Jinja + htmx (no Node build, small image, easy to keep working) is the
recommended default. A SPA (React/Vue) only if you want richer client-side editing than the template preview needs.

## Docker
- Image: `python:3.13-slim`, non-root user, `pip install .`, bundled OFL fonts, `HEALTHCHECK` on `/healthz`.
- Volume `/data` holds: `config.yaml`, `overrides.yaml`, `templates/`, `fonts/` (optional), `state.db`, `originals/`, `cache/`.
  **`originals/` + `state.db` are the only way to restore: document and encourage backing up the volume.**
- Env: `API_EMBY`, `API_MDBLIST` (or `*_FILE` for Docker secrets), `LAMINATOR_ADMIN_PASSWORD`, `TZ`.
- Network: the container needs to reach Emby/Jellyfin (LAN) and `api.mdblist.com`. Not published to the internet.
- Compose example in the repo; image built by CI later.

## Phases
(Phases 1 and 2 are built: `laminator serve` with read-only browsing plus confirmed apply/restore jobs; see README. Not yet in the UI: metadata overrides editor/diff/apply, score override editor, template saving, scheduler.)
0. Refactor (above): `actions` module, data dir, font rule, config. Tests keep passing.
1. Read-only UI: libraries, items, state, template preview. Nothing can be written yet: safe to put in front of the real server.
2. Poster apply/restore from the UI with jobs and progress, scope rules enforced server-side.
3. Metadata screens (diff/apply/restore, hand-edit conflicts).
4. Scheduler, auth hardening, Dockerfile + compose, backup guidance.
5. Jellyfin parity: verify every Emby finding in the README against a real Jellyfin before claiming support.

## Decisions to confirm
- UI tech: htmx + Jinja (recommended) vs. a SPA.
- Auth: built-in admin password vs. only behind a reverse proxy.
- ~~Multiple servers in one instance, or one container per server.~~ Decided: one instance, Emby and Jellyfin side by side, each with its own state folder.
- Does the container get write access to media-server folders? (It should not: everything goes through the API.)


## Poster selection engine (built; see README "Choosing another poster")
First source: the media server's own remote-image providers (TheMovieDb, TheTVDB, FanArt), verified on Emby 4.10, no extra keys.
Research notes (checked 2026-11-01, mostly from web search results, so re-check before relying on them):
- **MediUX**: no public API documentation or terms found; a community post says the API is in a pre-release test phase with keys for testers. Not integrated, not scraped.
- **TMDb**: documented API (key needed); free for non-commercial use with attribution in an About/Credits section; it does not claim ownership of the images and gives no redistribution rights.
- **fanart.tv**: documented v3 API (`webservice.fanart.tv/v3/...`, personal key optional, newer images only visible to personal-key/VIP users after a delay); rate limits not published.
Direct TMDb/fanart/TVDB connectors were left out on purpose: the server already aggregates them, and code against APIs I cannot test without keys adds risk for no gain.
Possible later: a source interface for a MediUX-style set provider once an official API exists; a "previous posters" history screen (every earlier original is already kept on disk); automatic selection rules with a dry run.
