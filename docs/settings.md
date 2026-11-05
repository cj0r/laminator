# Settings and the data folder

Everything can be set in **Settings**; no files need editing. Values saved there override `config.local.yaml`, which is never rewritten. **Back to config file...** undoes them (it asks first).

## Sections

- **Media servers**: address, library filter and API key for Emby and Jellyfin. **Save** also tests the connection. The address can be typed plainly: `192.168.1.10:8096` and `home.local` become `http://...`, other domains become `https://...`, and a scheme you type is kept. A mistake shows a message and keeps everything you typed.
- **MDBList**: the API key, shared by both servers.
- **Options**: CriticRating (off by default), how often to check for changes, the template for scheduled runs, and maintenance timing.
- **Custom posters**: the assets folder.
- **Backup and restore**: schedule, second copy, and backups.
- **Sign-in and security**: password, two-factor sign-in, and sessions.

API keys are masked and never sent back to the browser. A key saved here overrides `.env` and the environment; removing it brings the environment's key back. Optional environment variables: `API_EMBY`, `API_JELLYFIN`, `API_MDBLIST`, and `LAMINATOR_ADMIN_PASSWORD` to preset the password (each also as `NAME_FILE` for Docker secrets). You can set all of these in the app instead.

## Emby and Jellyfin together

Configure both in Settings. A switcher in the header picks which server the pages show. Scheduled runs (the check for new or changed posters, and monthly maintenance) only cover servers that are **included**: a server that already has saved posters stays included, and a newly connected one is left out until you tick **Include in scheduled runs** on its card. That way connecting a second server never draws badges on its whole library by surprise; run *Apply* yourself first, on a few titles, then include it. On the command line, add `--server emby|jellyfin|all` when both are set up. Each server's data is kept separately.

## The data folder

`/data` in the container. It holds:

```
settings.yaml, secrets.env, admin.hash   settings, saved keys, the password (hashed)
mfa.json, sessions.json, audit.log       two-factor sign-in, sessions, the security log
overrides.yaml, templates/, fonts/, assets/
backups/, exports/, trash/, cache/
servers/emby/, servers/jellyfin/         each server's original posters and records
```

Back it up: `servers/*/originals/` and `state.db` are the only way to restore original posters.
