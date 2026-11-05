# Security

## Reporting a problem

Please don't open a public issue for a security problem. Report it privately through GitHub instead: on the repository's **Security** tab, choose **Report a vulnerability**. Say what you found, how to reproduce it, and which version you're running (shown next to the logo in the web UI).

You'll get an answer within a week. Once a fix is out, the release notes credit you unless you'd rather they didn't.

## Supported versions

Only the latest release gets security fixes. Update by pulling the newest image and recreating the container; your `/data` folder carries over.

## What laminator protects

- The web UI holds your media server and MDBList API keys, so it asks for a password (chosen on first start with a one-time setup code), offers two-factor sign-in, and ends sessions on logout, after 2 hours idle or after 12 hours. See [Security](docs/security.md) in the user guide for sign-in and reverse proxy setup.
- Passwords and recovery codes are stored only as hashes. API keys you enter in Settings are kept in `secrets.env` in the data folder (mode 600), so keep that folder private and include it only in backups you protect. Keys are never logged or shown in error messages.
- The container runs as an unprivileged user with no setuid programs and needs no extra capabilities. Dependencies are pinned and checked for known vulnerabilities on every build.

Problems in Emby, Jellyfin, MDBList or other services laminator connects to belong with those projects.
