# Using the web UI

The menu is at the top; on a phone it scrolls sideways. Click any **(i)** icon for a short explanation.

- **Library**: your titles as posters, compact posters or a list, with search and an A-Z rail on big libraries. Labels show each title's state. **Custom posters** and **Apply to this library...** are here too. The per-page menu sets how many titles show at once: **Scroll** keeps loading more as you reach the bottom (the default), or pick 25 to 500 per page. The choice is remembered in a cookie.
- **Title page** (click a poster): the original, what the server shows and a preview; apply, restore, choose another poster, poster history, a custom poster, and the override form.
- **Templates**: design the badge. See [Templates](templates.md).
- **Metadata**: what your overrides would change. See [Metadata overrides](metadata-overrides.md).
- **Export**: download posters laid out next to your media. See [Export](export.md).
- **Runs**: every action runs as a job with a live log, one at a time. The big actions (apply everything, maintenance, cleanup, restore everything) are here. A request made while another run is working waits in line and starts by itself (you can cancel it while it waits). A small one, such as changing one title's poster, goes first: it runs between two titles of a big apply, which then carries on. Scheduled runs never queue; they skip if something is busy.
- **Status**: connections, schedule, backups and the security log.
- **Settings**: servers, keys, options, backups, sign-in and security. See [Settings](settings.md).

Anything that writes to your server asks for confirmation first.

Pages you type into (Settings, Templates, overrides) warn before you leave with unsaved changes: **Discard changes** or **Continue editing**.

The Library has a **Filter** menu (no MDBList score, MDBList score available, manual score override, not managed, overlay applied, original restored, changed on server, custom poster, no server poster, thumbnail instead of poster, badge reads 0.0, metadata overridden, listed in overrides.yaml) and a **Score, year and content rating** section: a score range for the combined MDBList score or a single provider (a manual override counts), a year range, and a content rating chosen from the ratings in the library. Everything combines with the search box. Filters read what laminator already saved, so they never ask MDBList. The Metadata page has a search box and status and field filters for the planned changes.

With a filter or a search active, **Apply to these N titles...** appears next to the library button. It opens a confirmation that lists the titles, then draws exactly that list (a full refresh by default, so changed scores and overrides are picked up). Restore is not offered for a selection.

**Reset filters** (next to the filter controls, and "reset filters" in the count line) clears the search and every filter at once and keeps the library and view you chose. The Metadata page has a **Reset** button beside its search and filters.

**Thumbnail instead of poster** finds titles whose image in the server is a landscape or square frame (a video screen grab) and not a poster; give those a custom poster. **Badge reads 0.0** finds old badges drawn for titles with no real score. **Restore these N...** puts the original posters back for a filtered list and removes the CriticRating laminator wrote for them; ratings, sort titles and other overrides are not touched. A title that still has a 0.0 badge is cleaned up automatically on the next apply.
