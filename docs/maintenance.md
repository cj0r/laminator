# Maintenance and cleanup

## The schedule

While the web UI runs it can do two things on its own, **but only once you turn them on** (Settings > Options):

- A **quick check** (off until you choose how often, in Settings, Options): new titles, changed posters, new custom posters.
- A **monthly maintenance** pass (off until you turn it on): every title's score refreshed, then a cleanup.

Scheduled runs show on the Runs page. **Run maintenance** does the monthly pass by hand.

## Cleanup

Removes what laminator no longer needs: records of titles that left your library, orphan files, and old caches and exports.

- A title that disappears is only cleaned up after **30 days** missing, and only if it's still missing then.
- **A server or NAS that is just offline never counts as removed.** If the server can't be reached, or many titles vanish at once, nothing is cleaned.
- Removed things go to the **trash** for 90 days first ([restore them](backups.md#restoring-from-the-trash)).

The **Cleanup** page (or `laminator cleanup`) shows the plan before anything happens.
