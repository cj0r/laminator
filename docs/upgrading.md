# Upgrading

## From 0.1.0 to 0.2.0

Before you upgrade, make a backup: **Settings > Backup**, or copy the data folder (the one mounted at `/data`). Then pull the new image and recreate the container. Nothing else is needed.

What happens on the first start of 0.2.0:

- **Your data folder is reorganised, once.** The old single-server layout (`state.db`, `originals/`, `out/`, `schedule.json` directly in the data folder) moves into `servers/<emby or jellyfin>/`. Files are renamed, never copied or deleted. It refuses to run, and leaves everything where it was, if anything would be overwritten or the folder spans filesystems.
- **`state.db` leaves WAL mode.** If an earlier build left it in WAL mode, laminator first saves one consistent copy next to it as `state.db.pre-persist`, then converts it. Nothing reads that copy; delete it when you are happy.
- **Nothing runs on its own until you turn the schedule on** (Settings > Options). Your posters and overrides are untouched until then.
- **No password set yet?** The web UI asks for the one-time setup code from `docker logs <container>` and lets you choose a password.
- **A second server** (Emby plus Jellyfin) stays out of scheduled runs until you tick **Include in scheduled runs** for it in Settings.

Posters already on your server keep their badges. The next run updates scores in place, and an apply with nothing changed skips the title.

To go back, restore the backup you made and run the previous image. The conversion of the data folder is not reversed automatically, which is why the backup comes first.

## From 0.2.0 to 0.2.1

Nothing to do. 0.2.1 changes no behaviour; pull the new image and recreate the container.
