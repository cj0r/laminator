# Troubleshooting

**I can't find the setup code.** It's printed in the container's log when it starts (`docker logs laminator`, or the Logs icon on Unraid). Restart the container to get a new one.

**Can't open it from another computer.** Check the port mapping and the host's firewall.

**"Not connected" in Settings.** Check the server address (`http://host:port`, no path) and API key, then press **Test**. From a container, use the server's network address, not `localhost`.

**"Another job is already running".** Only one run happens at a time. Wait for it on the Runs page.

**"Poster already carries a laminator overlay, but there is no saved original".** This data folder doesn't have the originals for posters that already have badges. Restore a [backup](backups.md), add a [custom poster](custom-posters.md), or replace the poster in your server.

**A title has no badge.** MDBList has no score for it, or the title has no IMDb or TMDb id in your server.

**A metadata change reverted.** A server refresh undid it. The Metadata page marks it *reverted by the server*; apply again. (Taglines can't be locked on Emby.)

**"Cross-site request refused" behind a reverse proxy.** Set `LAMINATOR_PUBLIC_URL`, or pass the `Host` header through. See [Security](security.md#reverse-proxy).

**Signed out right after signing in through the proxy.** You switched between the HTTPS and HTTP addresses. Use one.

**Lost the authenticator app.** Use a recovery code, or run `laminator reset-mfa` on the server.

**Second backup copy fails.** Use **Test it** in Settings to see why. Local backups keep working meanwhile.

**A run stopped with "MDBList's request limit is used up".** Your MDBList plan allows a fixed number of requests per day. Titles already finished keep their badges; run it again after the limit resets (daily). Lookups are cached on disk for 24 hours, so a second run only asks for what is missing. MDBList error messages never contain your API key.

**Every page says "The data folder is not responding".** laminator checks its data folder every 15 seconds (`LAMINATOR_STORAGE_PROBE_SECONDS`). If a check has not come back after 20 seconds (`LAMINATOR_STORAGE_STALL_SECONDS`), the disk or filesystem under it is stuck, and laminator answers this page at once instead of hanging. Nothing is lost, scheduled runs wait, and the page retries by itself; the health check (`/healthz`) fails with HTTP 503 while it lasts, so Docker shows the container as unhealthy. The check only reads (a stat, a folder listing and a few bytes of a file), mostly from the filesystem's cache, so it causes no disk wear; it catches a stuck filesystem, not every kind of failing drive. The cause is outside laminator. Look at the server's kernel log (`dmesg`) for filesystem errors, and see whether other containers on the same disk are also stuck. A process stuck in the kernel (state `D`) cannot be stopped, so a restart of the server may be the only way out.

**A file named `state.db.pre-persist` appeared.** An earlier version kept `state.db` in SQLite's WAL mode, which can trigger bugs in some filesystems (OpenZFS 2.4.2 kernel panics on one server). The current version converts it on the first start, and keeps one consistent copy first. Nothing reads it; delete it once you are happy with the result.
