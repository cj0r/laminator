# Changelog

Versions follow [semantic versioning](https://semver.org). Docker images are tagged `latest` (the newest release), `<version>`, `<major>.<minor>` and `<major>` (for a release tag such as `v0.2.0`), and `dev` (the latest test build from the development repository).

## 0.3.2 (2026-11-12)

### Changed
- The footer stays at the bottom of the window as a slim row and expands to the full footer at the end of the page.
- The Library's copies switch is now labelled Versions stacked / Unstacked, with an info icon explaining it.
- Updated the pinned dependencies: click 8.5.1, fastapi 0.162.5, pydantic 2.14.3, pydantic-core 2.50.4, typing-inspection 0.4.5 and annotated-doc 0.0.6.
- Updated the bundled htmx from 2.0.4 to 2.0.11, and listed it in the third-party notices.
- The Docker image uses the current `python:3.13-slim` base (rebuilt 2026-11-05).
- Dependabot groups the Python updates into one pull request, so pydantic and pydantic-core are updated together.
- The Docker Hub overview step of the release workflow uses peter-evans/dockerhub-description v5.

## 0.3.1 (2026-11-06)

### Changed
- The footer links to the GitHub repository again, now that it is public.

## 0.3.0 (2026-11-06)

### Added
- **Copies of one title are shown together in the Library.** A movie or show that appears more than once in a library (an HD and a UHD folder) is one poster with a "2 copies" badge, with a Copies together / Each copy switch (remembered). Nothing is merged on the server: each copy keeps its own poster, state and custom poster. The title page lists the copies and can apply to, or restore, all of them at once; the Library's counts and "Apply to these N titles" count every copy. Copies are items of the same type sharing an IMDb, TMDb or TVDB id; items without an id are never grouped.
- **The Custom posters page has the Library's views.** Posters, compact and list (the same choice as the Library's), search by title, library or file name, a status filter with counts (in use, waiting, unusable, not matched) and paging. Every view shows the poster file itself with its title, library, file and status, and files that match no title are listed in the same views. Thumbnails are made from the files in the assets folder, cached, and only files inside that folder are ever served; an unreadable file shows a placeholder tile.
- **Choose where the primary backups are kept.** Settings > Backup and restore > Backup folder (or the `LAMINATOR_BACKUP_DIR` environment variable) sets a folder other than `<data>/backups`, so the backups can live on another disk or share. The folder is checked before it is saved; existing backups can be moved there (each copied, checked and only then removed from the old folder); Settings and Status show the folder, how many backups are in it and any problem; `laminator backup list` shows it too. The default is unchanged.

### Changed
- Notes and errors appear next to the form you used, and the page opens scrolled to them, instead of at the top of a long page: every Settings section has its own note slot; a refused override on a title page stays on that title with what you typed kept; a refused custom poster upload returns to the title's upload box. Copy labels drop `[imdbid-…]` tags.
- Clearer labels: the Runs buttons (Apply overlays, Run maintenance, Clean up files, Restore originals), the Second copy menu (None, Local folder, SMB share), the backup upload and API-key options, and the Library filter menu (alphabetical; Override pending, Thumbnail (not poster), Score invalid (0.0) and others).
- The Library filter "MDBList score missing" counts a score from any single source and leaves out titles with a manual score; "Override pending" lists only entries whose values are not on the title yet.

### Fixed
- A script error on title pages (a form field named "item") stopped the unsaved-changes warning and the scroll to a note there.
- The two info icons on the Library opened together.

## 0.2.3 (2026-11-05)

A small maintenance release; the application itself is unchanged.

### Fixed
- The badges at the top of the README and the Docker Hub overview did not always load on GitHub, whose image proxy gives up after a few seconds on a slow badge. The test badge now comes from GitHub itself, the version badge uses the lighter form, the image-size badge is gone, and the others are cached for a day.

## 0.2.2 (2026-11-05)

A small maintenance release; the application itself is unchanged.

### Changed
- The README and the Docker Hub overview show the image's Docker Hub version, pull count and size, the test status and the license, and the quick start names the registries (Docker Hub and `ghcr.io`) and the tags.
- The release build uses `docker/setup-buildx-action` 4.4.1 (still pinned to a commit).

## 0.2.1 (2026-11-05)

A small maintenance release; the application itself is unchanged.

### Added
- A PNG version of the icon (`docs/logo/icon.png`, 512x512), for Community Applications and other places that do not take SVG.

### Changed
- The GitHub Actions used to test, build and release the image are pinned to commit SHAs instead of version tags (the version stays in a comment, and Dependabot keeps them current), so a moved or hijacked tag cannot change what runs with the release permissions.

## 0.2.0 (2026-11-05)

### Added
- **Emby and Jellyfin side by side**, each with its own saved posters and restore data. Checked against Emby 4.10 and Jellyfin 10.11 with movies; TV shows are less tested on Jellyfin.
- **Include in scheduled runs** per server. A newly connected server is left out of the schedule until you tick the box, so it never gets badges on its whole library by surprise. A server that already has saved posters stays included.
- **Library page**: filters (score, year, content rating, poster state, custom poster, metadata overridden, thumbnail instead of poster, 0.0 badges), search, Reset, titles per page or endless scroll, and **Apply to these N titles** for a filtered list.
- **Run queue**: a request made while another run works waits in line (cancel while waiting); small single-title requests go first. The Runs page updates by itself.
- **Metadata overrides**: title, tagline, critic rating, tags, genres, studios (replace, add, remove, clear) next to content rating, sort title, original title and description. A title that already has an override shows it filled in on its page, and **Save override** replaces that entry in place. Poster score is typed on the 0-100 scale, like the critic rating.
- **Not on this server**: an override for a title that exists only on the other server is listed apart on the Metadata page and summarised in one line in poster runs, not reported as a problem.
- **Custom base posters** (an assets folder named after each title's media folder), template editor and named templates, hamburger menu and layout fixes for phones.
- **Security**: first-start password setup with a one-time code, two-factor sign-in with recovery codes, revocable sessions, hardened container, security log, pinned dependencies with a build-time vulnerability audit.
- GPL-3.0 license and third-party notices.

### Changed
- **The combined score uses MDBList's average of the sources** when it sits 5 or more points below it (missing sources drag the combined score down: Claydream 4.4 versus 7.3). A named source (IMDb, Trakt, ...) is always shown as it is.
- Nothing runs on its own until you turn the schedule on.
- Settings opens in milliseconds: backup summaries are remembered instead of unpacking every archive on each visit. The Metadata page reads the server in parallel.

### Fixed
- **The state database no longer uses WAL.** It uses the rollback journal in PERSIST mode, which only overwrites the journal header in place. WAL's memory-mapped `-shm` file and its truncation of the `-wal` file were in the path of OpenZFS 2.4.2 kernel panics on one server, which hung Laminator and other containers. A database left in WAL mode by an earlier build is converted on the first start.
- **The Docker health check could pile up stuck processes** when the data folder hung: it ran `python` inside `/data`, which makes Python list that folder at start-up. It now runs in isolated mode and never touches the data folder.
- **A stuck disk can no longer hang the whole interface.** Sessions are checked from memory and the session file and the audit log are written by a background thread that a request waits for only briefly (and not at all while that write is stuck), so signing in and every page keep working. Before, a write that never returned inside the sign-in check froze everything, including the health check.
- **"The data folder is not responding" page and a failing health check** when the data folder does not answer for about 20 seconds (a read-only probe every 15 seconds, one at a time; `LAMINATOR_STORAGE_PROBE_SECONDS` and `LAMINATOR_STORAGE_STALL_SECONDS` change it). Scheduled runs wait until it answers again.
- **A copy of `state.db` (`state.db.pre-persist`) is kept** before an earlier build's WAL database is converted.
- **An MDBList failure (for example the daily request limit) put the API key into the job log, the UI and the server log.** Errors now say what happened in plain words and never contain the key.
- Jellyfin: asking it to lock `SortName`, `OriginalTitle` or `CriticRating` made the whole update fail with HTTP 400. Only the fields Jellyfin accepts are locked now. One title the server refuses no longer stops a metadata run.
- A score of 0 from MDBList no longer draws a badge reading 0.0, and a stale 0.0 badge is taken off on the next apply.
- A title with no poster in the server but a custom poster is now drawn.
- Stretched grid posters, the A-Z rail on phones, and a crash with no MDBList key set.

### Upgrading from 0.1.0
Back up first (Settings > Backup), then pull the new image. The data folder is reorganised once on the first start, `state.db` is converted from WAL (a copy is kept as `state.db.pre-persist`), and nothing runs until you turn the schedule on. Details in [docs/upgrading.md](docs/upgrading.md).

## 0.1.0

First version: MDBList score badges on Emby and Jellyfin posters, metadata overrides with backup, lock and restore, web UI, command line and Docker image.
