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

## Scheduled backups

By default every 7 days, keeping the last 5 (Settings). Scheduled backups never include keys or the password, and only scheduled ones are ever pruned.

## A second copy

**Second copy** copies each backup to a folder (a mounted share, or a cloud drive mounted with rclone) or an SMB share. Each copy is read back and checked. If the destination is down, the local backup still works and the copy is retried hourly.

## Restoring from the trash

Cleanup moves things to a trash rather than deleting them. **Cleanup > Trash** (or `laminator trash restore`) puts them back. Nothing that exists is overwritten.
