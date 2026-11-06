# Getting started

## Docker

```yaml
services:
  laminator:
    image: cj0r/laminator:latest
    restart: unless-stopped
    ports: ["5264:5264"]
    environment:
      - TZ=America/New_York
    volumes:
      - ./data:/data
    user: "1000:1000"   # the user and group that own ./data (run `id` to find yours)
```

1. `docker compose up -d`, then run `docker logs laminator` to see the **setup code**.
2. Open `http://<host>:5264`, enter the setup code and choose your password. It is stored as a salted hash in your data folder.
3. In **Settings**, add your media server's address and API key, then your MDBList key.
4. Open a title in the **Library**, press **Apply overlay...**, and check it in your media server. Happy? Apply a whole library.

The setup code only exists until you choose a password, so nobody else on your network can claim a fresh install. Prefer to set the password up front? Use `LAMINATOR_ADMIN_PASSWORD` (or `LAMINATOR_ADMIN_PASSWORD_FILE`) instead.

Update with `docker compose pull && docker compose up -d`. Your data stays in `./data`. See [docker-compose.example.yml](../docker-compose.example.yml) for optional settings.

## Unraid

Add a container with:

- **Repository**: `cj0r/laminator:latest` (the newest release; `cj0r/laminator:dev` is the latest test build, `cj0r/laminator:0.3.1` pins one release)
- **Port**: 5264 → 5264
- **Path**: `/mnt/user/appdata/laminator` → `/data`

The image runs as `99:100` (`nobody:users`), the owner Unraid gives new appdata folders, so no user setting is needed. After it starts, open the container's log (the **Logs** icon) for the setup code, then open the web UI and choose your password.

Optional paths (under **Show more settings**): **Custom Posters Folder** (`/assets`, for [custom posters](custom-posters.md)); **Backup Folder** (`/backups`, to keep the [backups](backups.md#where-backups-are-kept) outside appdata: also set **Backup Folder Path** to `/backups`, or choose `/backups` in Settings, which can move existing backups for you); and **Second Backup Folder** (`/backups-secondary`, for a [second copy](backups.md#a-second-copy): in Settings choose Second copy > Local folder, path `/backups-secondary`). Include the appdata folder in your backups.

## Other container managers

Any tool that runs Docker images works. Create a container with:

- **Image**: `cj0r/laminator:latest` (the newest release; `cj0r/laminator:dev` is the latest test build, `cj0r/laminator:0.3.1` pins one release)
- **Port**: 5264 → 5264
- **A persistent folder** mounted at `/data`, owned by the user the container runs as (set that user with the manager's *user* option)
- **Time zone**: the `TZ` environment variable

After it starts, open the container's log for the setup code, then open the web UI and choose your password.

Optional folders: one for [custom posters](custom-posters.md) mounted at `/assets`, one for the [backups](backups.md#where-backups-are-kept) themselves (choose its path in Settings, or set `LAMINATOR_BACKUP_DIR`) and one for a [second backup copy](backups.md#a-second-copy). Include the data folder in your backups.

## From source

```
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/laminator serve --host 0.0.0.0     # prints a setup code; open the UI and choose your password
```

## Building the image yourself

```
docker build -t laminator .
```

Then use `laminator` as the image name in the examples above.

## Jellyfin

Supported and unit-tested, but **not yet verified against a real Jellyfin server**.
