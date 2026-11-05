# laminator

[![Docker pulls](https://img.shields.io/docker/pulls/cj0r/laminator?style=flat-square&cacheSeconds=86400)](https://hub.docker.com/r/cj0r/laminator)
[![Tests](https://github.com/cj0r/laminator/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/cj0r/laminator/actions/workflows/tests.yml)
[![License: GPL-3.0](https://img.shields.io/badge/license-GPL--3.0-blue?style=flat-square)](https://github.com/cj0r/laminator/blob/main/LICENSE)

MDBList score badges on your Emby and Jellyfin posters, and metadata overrides on existing items, from a web UI. Everything it changes can be put back. Emby and Jellyfin can be connected at the same time.

- Draws a rating from MDBList (IMDb, TMDb, Trakt, Letterboxd, Rotten Tomatoes, Metacritic or MDBList's combined score) on each poster, always from the untouched original, so re-running never stacks badges.
- Overrides content ratings, titles, sort titles, tags, genres, studios and more from a file you keep. Every change shows as a diff first, is backed up, locked against refreshes where the server allows it, and can be restored.
- Custom base posters, named templates, scheduled checks, backups, password and two-factor sign-in.

## Quick start

```yaml
services:
  laminator:
    image: cj0r/laminator:latest
    container_name: laminator
    restart: unless-stopped
    ports:
      - "5264:5264"
    security_opt:
      - no-new-privileges:true
    environment:
      - TZ=Etc/UTC          # your time zone, for the schedule
    volumes:
      - ./data:/data        # settings, saved original posters, state, backups: back this up
    user: "1000:1000"       # the user and group that own ./data (run `id` to find yours); on Unraid: "99:100"
```

Run `docker compose up -d`, then read the one-time setup code in `docker logs laminator`, open `http://<host>:5264` and choose your password. Images are built for `linux/amd64` and `linux/arm64`.

laminator changes posters and metadata on your media server: try it on a few titles first.

## Tags

- `latest`: the newest release.
- `0`, `0.2`, `0.2.3`: a major, minor or exact release, to update on your own terms.

The same images are on GitHub's registry as `ghcr.io/cj0r/laminator`.

## More

User guide: https://github.com/cj0r/laminator/blob/main/docs/README.md. Source: https://github.com/cj0r/laminator. License: GPL-3.0.
