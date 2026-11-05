# Security

## Before exposing it to the internet

1. Turn on **two-factor sign-in** (Settings > Sign-in and security).
2. Put it behind a **reverse proxy with HTTPS**, on its own subdomain.
3. Use a **long password** (10 characters or more; it is stored as a salted hash, never the password itself).
4. Make the container reachable only by the proxy.

## Sign-in

- **Two-factor sign-in**: after the password, a 6-digit code from an authenticator app. You get 10 single-use recovery codes when you set it up. Lost your device and codes? Run `laminator reset-mfa` on the server.
- **First start:** until a password exists, the UI shows only a setup page, and it needs a one-time **setup code** printed in the container's log, so someone who merely reaches the page can't claim the app.
- **Forgot the password?** `laminator reset-password`; the next start asks you to choose a new one.
- Repeated wrong passwords or codes from one address are blocked for a few minutes.
- **Sessions** end on logout, after 2 hours idle, or after 12 hours. Changing the password or two-factor setting signs out all other sessions.
- Sign-ins and security changes are recorded in the **security log** on the Status page.

## Reverse proxy

Set two environment variables:

- `LAMINATOR_TRUSTED_PROXIES`: your proxy's address or network (for example `172.18.0.0/16`). Only it may tell laminator the real client address and whether HTTPS is used.
- `LAMINATOR_PUBLIC_URL`: the address you use, for example `https://laminator.example.com`.

Serving it under a sub-path (`example.com/laminator`) is not supported.

**nginx**

```nginx
location / {
    proxy_pass http://laminator:5264;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-Host $host;
}
```

**Caddy**

```
laminator.example.com {
    reverse_proxy laminator:5264
}
```

**Traefik**

```yaml
labels:
  - traefik.enable=true
  - traefik.http.routers.laminator.rule=Host(`laminator.example.com`)
  - traefik.http.routers.laminator.tls=true
  - traefik.http.services.laminator.loadbalancer.server.port=5264
```

For large backup uploads, raise the proxy's upload limit (nginx: `client_max_body_size 2g;`).

## The container

The image runs as an unprivileged user with no capabilities and no setuid programs, and can't modify its own files. Nothing extra needs to be configured. Everything laminator writes is private to that user.

Secrets can be passed as files (Docker secrets) with `LAMINATOR_ADMIN_PASSWORD_FILE`, `API_EMBY_FILE`, `API_MDBLIST_FILE` and so on, so they don't show in `docker inspect`.

Found a security problem? Please report it privately rather than in a public issue.
