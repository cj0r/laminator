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

The Library has a **Filter** menu (grouped Metadata, Poster and Scores, each in alphabetical order: override applied, override pending; changed on server, custom poster, not managed, original restored, overlay applied, poster missing, score invalid (0.0), thumbnail (not poster); manual score, MDBList score missing, MDBList score present) and a **Score, year and content rating** section: a score range for the combined MDBList score or a single provider (a manual override counts), a year range, and a content rating chosen from the ratings in the library. Everything combines with the search box. Filters read what laminator already saved, so they never ask MDBList. The Metadata page has a search box and status and field filters for the planned changes.

With a filter or a search active, **Apply to these N titles...** appears next to the library button. It opens a confirmation that lists the titles, then draws exactly that list (a full refresh by default, so changed scores and overrides are picked up). Restore is not offered for a selection.

**Copies of the same title.** When a library holds the same movie or show more than once (an HD and a UHD folder, say), the Library shows those copies as one poster with a **2 copies** badge, and a **Copies together / Each copy** switch appears next to the view switch (remembered, like the view). Nothing is merged on your server: every copy is its own item with its own poster, state, custom poster and overrides, and the title page lists the copies, with **Apply overlay to all copies** and **Restore the original posters of all copies**, or you open one copy and work on it alone. Two items are copies when they have the same type and share an IMDb, TMDb or TVDB id; an item with no id is never grouped. Copies are grouped within one library. If the copies are in different states the badge reads *copies differ* (hover it to see which is which). The count line shows both numbers (for example "5 items (8 posters)"), and **Apply to these N titles** counts every copy, because a run draws on each one.

**Reset filters** (next to the filter controls, and "reset filters" in the count line) clears the search and every filter at once and keeps the library and view you chose. The Metadata page has a **Reset** button beside its search and filters.

**Thumbnail (not poster)** finds titles whose image in the server is a landscape or square frame (a video screen grab) and not a poster; give those a custom poster. **Badge reads 0.0** finds old badges drawn for titles with no real score. **Restore these N...** puts the original posters back for a filtered list and removes the CriticRating laminator wrote for them; ratings, sort titles and other overrides are not touched. A title that still has a 0.0 badge is cleaned up automatically on the next apply.
