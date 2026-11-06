# Backups and restore

These back up **laminator's own data**: original posters, records, settings, overrides, templates and fonts. Not your media server.

## Making and restoring backups

In **Settings > Backup and restore**: create, download, upload and restore backups. Or:

```
laminator backup create [--include-secrets]
laminator backup verify FILE
laminator backup restore FILE [--force]
```

- Every backup is checked before it's restored, and replaced files are moved aside, not deleted.
- API keys and the password are only included if you ask, encrypted with a passphrase. Lose the passphrase and those are gone; everything else still restores.
- Backups are portable: restore one on a new machine or into a container. Do that **before** applying anything there, so the new install has your original posters.

## Where backups are kept

By default in the `backups` folder inside the data folder. That is on the same disk as the data it protects, so a failed disk takes both. To keep them somewhere safer, mount a folder from another disk or share into the container and choose it in **Settings > Backup and restore > Backup folder** (the path as the container sees it, for example `/backups`), or set the `LAMINATOR_BACKUP_DIR` environment variable. A folder saved in Settings wins over the variable. Creating, listing, downloading, verifying, restoring, uploading and pruning scheduled backups all use that folder.

- The folder is checked before it is saved: it must be an absolute path, writable, and not the data folder itself or inside its `servers`, `templates`, `fonts` or `cache` folders.
- Backups already in the old folder are moved to the new one if you leave the box ticked: each is copied, checked against its SHA-256 and only then removed from the old folder. If one fails, it stays where it was. Backups left behind (for example when you unticked the box) show a **Move them** button.
- If the folder is missing or not writable (a share that is not mounted), Settings and Status say so, scheduled backups fail with that message, and nothing is written elsewhere.
- Blank means the default (or `LAMINATOR_BACKUP_DIR`). `laminator backup list` shows the folder in use.
- The **second copy** below is separate: it gets a copy of each backup made in the primary folder.

## Scheduled backups

By default every 7 days, keeping the last 5 (Settings). Scheduled backups never include keys or the password, and only scheduled ones are ever pruned.

## A second copy

**Second copy** copies each backup to a folder (a mounted share, or a cloud drive mounted with rclone) or an SMB share. Each copy is read back and checked. If the destination is down, the local backup still works and the copy is retried hourly.

## Restoring from the trash

Cleanup moves things to a trash rather than deleting them. **Cleanup > Trash** (or `laminator trash restore`) puts them back. Nothing that exists is overwritten.
