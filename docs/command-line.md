# Command line

```
laminator [-c CONFIG] <command> [options]
```

In Docker: `docker exec laminator laminator <command>`. With both servers configured, add `--server emby|jellyfin|all`. `laminator <command> --help` lists every option.

| Command | What it does |
|---|---|
| `serve` | The web UI (port 5264) and the schedule. |
| `scan` | List library items. |
| `render` | Draw badges into a folder without uploading. |
| `apply --library L \| --item I \| --all` | Draw and upload badges. `--changed` only handles new or changed titles. |
| `restore --item I \| --all [--what posters\|meta\|all]` | Put originals and old metadata back. |
| `meta diff` / `meta apply` | Preview or write metadata overrides. |
| `maintain` | The monthly full pass. |
| `cleanup [--yes]` | Show the cleanup plan, or do it. |
| `trash list` / `trash restore` | See or restore what cleanup removed. |
| `export` | Make a poster zip. |
| `backup create \| verify \| restore \| list` | Back up laminator's own data. |
| `reset-password` | Remove the password saved in Settings; the next start asks for a new one. |
| `reset-mfa` | Turn off two-factor sign-in (if you lost your device). |

Exit codes: 0 fine, 1 problems, 2 bad input or configuration, 3 another run is busy.
