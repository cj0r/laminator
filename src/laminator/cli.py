"""Command line front end. Thin: parse arguments, call actions/meta, print the events."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import httpx

from . import actions, audit, backup, cleanup, destinations, export, maintenance, schedule, secrets_store, trash
from . import meta as metamod
from .assets import load_template
from .config import (MDBLIST_ENV, ConfigError, data_dir, enabled_servers, load_config, load_dotenv, load_secret_files, servers)
from .server import connect


# ---- choosing servers ---------------------------------------------------------------------------------
def chosen_servers(cfg: dict, args, default_all: bool = False) -> list[str]:
    """Which servers a command runs on. With two configured, a command must say so (or be a whole-system command)."""
    enabled = enabled_servers(cfg)
    if not enabled:
        raise ConfigError("no media server is configured: add one under Settings in the web UI, or in the config file")
    sel = getattr(args, "server", None)
    if sel == "all":
        return enabled
    if sel:
        if sel not in servers(cfg):
            raise ConfigError(f"no {sel} server is configured (configured: {', '.join(servers(cfg))})")
        if sel not in enabled:
            raise ConfigError(f"the {sel} server is switched off in Settings")
        return [sel]
    if len(enabled) == 1 or default_all:
        return enabled
    raise ConfigError(f"{' and '.join(enabled)} are both configured: say which with --server {'|'.join(enabled)}, or --server all")


def _emit(events, tag: str = "") -> int:
    """Print events (problems to stderr); return the number of problems. `tag` marks which server a line is about."""
    bad = 0
    for e in events:
        print(f"{tag}{e.line()}", file=sys.stderr if e.problem else sys.stdout)
        bad += e.problem
    return bad


def _config(args, required: bool = True) -> dict:
    return load_config(Path(args.config), required=required)


def _tag(kinds: list[str], kind: str) -> str:
    return f"[{kind}] " if len(kinds) > 1 else ""


def _overrides(args, cfg):
    """The overrides file lives in the shared data folder unless you point at another one."""
    p = Path(args.overrides)
    if args.overrides == "overrides.yaml" and not p.is_absolute():
        return data_dir(cfg) / "overrides.yaml", False  # the default may simply not exist
    return p, True


# ---- commands --------------------------------------------------------------------------------------------
def cmd_scan(args) -> int:
    cfg = _config(args)
    kinds = chosen_servers(cfg, args)
    for kind in kinds:
        ws = actions.open_workspace(cfg, args.library, need_mdblist=False, kind=kind)
        t = _tag(kinds, kind)
        if args.list_libraries:
            for lib in ws.srv.libraries():
                print(f"{t}{lib.get('Name')}\t{lib.get('CollectionType', '')}")
            continue
        for it in ws.srv.items(ws.scfg.library, args.limit):
            ids = ",".join(f"{k}={v}" for k, v in sorted(it.provider_ids.items())) or "-"
            print(f"{t}{it.type:7} {it.name} ({it.year or '?'}) poster={'y' if it.has_poster else 'n'} {ids}")
    return 0


def cmd_render(args) -> int:
    cfg = _config(args)
    kinds = chosen_servers(cfg, args)
    for kind in kinds:
        ws = actions.open_workspace(cfg, args.library, kind=kind)
        tpl, _ = load_template(args.template, ws.root)
        path, required = _overrides(args, cfg)
        _emit(actions.render_posters(ws, tpl, path, required, args.limit), _tag(kinds, kind))
    return 0


def cmd_apply(args) -> int:
    cfg = _config(args)
    kinds = chosen_servers(cfg, args)
    bad = 0
    for kind in kinds:
        ws = actions.open_workspace(cfg, args.library, kind=kind)
        actions.require_scope(args.item, args.library, args.all, ws.scfg.library)
        tpl, sig = load_template(args.template, ws.root)
        path, required = _overrides(args, cfg)
        with actions.run_lock(ws.root):
            bad += _emit(actions.apply_posters(ws, tpl, sig, path, required, args.item,
                                               bool(cfg.get("critic_rating", False)), args.changed), _tag(kinds, kind))
    return 1 if bad else 0


def cmd_maintain(args) -> int:
    """The monthly full pass: every library, every item, scores re-read and rounded to one decimal; poster and CriticRating are
    updated where the rounded score changed; then cleanup of what is no longer needed. Safe to run from cron. Defaults to every server."""
    cfg = _config(args)
    kinds = chosen_servers(cfg, args, default_all=True)
    bad = 0
    for kind in kinds:
        ws = actions.open_workspace(cfg, kind=kind)
        tpl, sig = load_template(args.template, ws.root)
        path, required = _overrides(args, cfg)
        t = _tag(kinds, kind)
        seen = []
        with actions.run_lock(ws.root):
            for e in maintenance.run(ws, tpl, sig, path, required, bool(cfg.get("critic_rating", False)), not args.no_cleanup,
                                     cleanup.Limits.from_cfg(cfg)):
                seen.append(e)
                print(f"{t}{e.line()}", file=sys.stderr if e.problem else sys.stdout)
        bad += any(e.problem for e in seen)
    return 1 if bad else 0


def cmd_cleanup(args) -> int:
    """Clean up what laminator no longer needs. Without --yes it only shows the plan."""
    cfg = _config(args)
    kinds = chosen_servers(cfg, args, default_all=True)
    lim = cleanup.Limits.from_cfg(cfg, args.safety_days)
    bad = 0
    for kind in kinds:
        ws = actions.open_workspace(cfg, need_mdblist=False, kind=kind)
        t = _tag(kinds, kind)
        if args.yes:
            with actions.run_lock(ws.root):
                bad += _emit(cleanup.run(ws, lim=lim), t)
        else:
            bad += _emit(cleanup.run(ws, lim=lim, dry_run=True), t)
    if not args.yes:
        print("This was only a plan. Run again with --yes to do it (anything removed from your records goes to trash/ first).")
    return 1 if bad else 0


def cmd_reset_password(args) -> int:
    """Recovery if the UI password was forgotten: removes the one saved from Settings."""
    from .web import passwords
    data = data_dir(_config(args, required=False))
    if passwords.remove(data):
        print("Removed the saved UI password. LAMINATOR_ADMIN_PASSWORD (.env / environment) applies again."
              if os.environ.get("LAMINATOR_ADMIN_PASSWORD") else
              "Removed the saved UI password. The next start asks you to choose a new one (with a setup code in the log).")
        from .web import sessions
        sessions.clear(data)  # signs out every session
        audit.log(data, "saved password removed (command line)")
    else:
        print("No password was saved from Settings; nothing to do.")
    return 0


def cmd_reset_mfa(args) -> int:
    """Recovery if the authenticator is lost and no recovery code is left: turns two-factor sign-in off and signs out every session."""
    from .web import mfa, sessions
    data = data_dir(_config(args, required=False))
    if mfa.disable(data):
        sessions.clear(data)
        audit.log(data, "two-factor sign-in turned off (command line)")
        print("Two-factor sign-in is off and every session was signed out. Sign in with the password, then set it up again in Settings.")
    else:
        print("Two-factor sign-in was not on; nothing to do.")
    return 0


def cmd_restore(args) -> int:
    cfg = _config(args)
    kinds = chosen_servers(cfg, args)
    actions.require_scope(args.item, None, args.all)
    bad = 0
    for kind in kinds:
        ws = actions.open_workspace(cfg, need_mdblist=False, kind=kind)
        t = _tag(kinds, kind)
        with actions.run_lock(ws.root):
            if args.what in ("meta", "all"):
                bad += _emit(actions.restore_metadata(ws, args.item, args.force), t)
            if args.what in ("posters", "all"):
                bad += _emit(actions.restore_posters(ws, args.item), t)
    return 1 if bad else 0


# ---- metadata overrides ---------------------------------------------------------------------------
def _print_plan(plans, tag: str = "") -> int:
    n_changes = 0
    short = actions._short
    for p in plans:
        print(f"{tag}{p.name}" + (f" [id {p.item_id}]" if p.item_id else ""))
        if p.absent:
            print(f"{tag}  not on this server (it is on {p.absent}); nothing to do")
            continue
        if p.error:
            print(f"{tag}  ERROR  {p.error}")
            continue
        for c in p.changes:
            mark = {"change": "", "reapply": "", "lock": "", "ok": "ok     ", "hand-edit": "SKIP   "}[c.action]
            arrow = f"{short(c.old)} -> {short(c.new)}" if c.action != "ok" else short(c.new)
            print(f"{tag}  {mark}{c.key}: {arrow}" + (f"   ({c.note})" if c.note else ""))
            n_changes += c.action in ("change", "reapply", "lock")
    return n_changes


def _why(e: Exception) -> str:
    """A short, readable reason for an HTTP failure (never the full URL's query or any key)."""
    r = getattr(e, "response", None)
    return f"HTTP {r.status_code}" if r is not None else type(e).__name__


def _elsewhere(cfg, kinds, kind):
    """The other chosen servers' libraries, read only if an override turns out to match nothing on this one."""
    def get():
        out = {}
        for other in kinds:
            if other != kind:
                out[other] = actions.all_libraries(actions.open_workspace(cfg, need_mdblist=False, kind=other).srv)
        return out
    return get


def _meta_setup(args, cfg, kind):
    ws = actions.open_workspace(cfg, need_mdblist=False, kind=kind)
    path = Path(args.overrides)
    if args.overrides == "overrides.yaml":
        path = ws.root / "overrides.yaml"
    return ws, metamod.load_overrides(path)


def cmd_meta_diff(args) -> int:
    cfg = _config(args)
    kinds = chosen_servers(cfg, args)
    for kind in kinds:
        ws, entries = _meta_setup(args, cfg, kind)
        t = _tag(kinds, kind)
        n = _print_plan(metamod.plan_all(ws.srv, ws.store, entries, args.item, kind, elsewhere=_elsewhere(cfg, enabled_servers(cfg), kind)), t)
        print(f"\n{t}{n} field(s) would be written. Nothing was changed (this is a dry run).")
    return 0


def cmd_meta_apply(args) -> int:
    cfg = _config(args)
    kinds = chosen_servers(cfg, args)
    bad = 0
    for kind in kinds:
        ws, entries = _meta_setup(args, cfg, kind)
        t = _tag(kinds, kind)
        plans = metamod.plan_all(ws.srv, ws.store, entries, args.item, kind, elsewhere=_elsewhere(cfg, enabled_servers(cfg), kind))
        _print_plan(plans, t)
        print()
        with actions.run_lock(ws.root):
            for p in plans:
                if p.absent:
                    continue
                if p.error:
                    bad += 1
                    continue
                try:
                    r = metamod.apply_item(ws.srv, ws.store, p, args.force)
                except httpx.HTTPError as e:  # one title the server refuses must not stop the rest
                    bad += 1
                    print(f"{t}WARN     {p.name}: the server refused the update ({_why(e)})", file=sys.stderr)
                    continue
                if not (p.writes or (args.force and any(c.action == "hand-edit" for c in p.changes))):
                    print(f"{t}nothing to do  {p.name}")
                elif r.ok:
                    print(f"{t}applied  {p.name}: verified by reading it back")
                else:
                    bad += 1
                    for m in r.messages:
                        print(f"{t}WARN     {p.name}: {m}", file=sys.stderr)
    return 1 if bad else 0


# ---- backup of laminator itself ------------------------------------------------------------------------
def _passphrase(args, confirm: bool = False) -> str | None:
    """From the environment (default LAMINATOR_BACKUP_PASSPHRASE) or a hidden prompt; never from the command line."""
    env = os.environ.get(args.passphrase_env)
    if env:
        return env
    if sys.stdin.isatty():
        import getpass
        p = getpass.getpass("Backup passphrase: ")
        if confirm and getpass.getpass("Again: ") != p:
            raise ConfigError("the two passphrases do not match")
        return p or None
    return None


def _print_report(rep: backup.Report) -> None:
    mb = rep.size / 1e6
    print(f"{rep.path}  ({mb:.1f} MB, {rep.files} files, made {rep.created})")
    for kind, i in rep.servers.items():
        print(f"  {kind}: {i['posters']} poster record(s), {i['meta']} metadata record(s), {i['originals']} saved original(s)")
    print(f"  API keys and password: {'included, encrypted' if rep.has_secrets else 'not included'}")
    for n in rep.notes:
        print(f"  note: {n}")


def cmd_backup_create(args) -> int:
    cfg = _config(args, required=False)
    root = data_dir(cfg)
    if args.auto and (args.include_secrets or args.out):
        raise ConfigError("--auto makes a scheduled-style backup: no secrets, standard name. Do not combine it with --include-secrets or --out")
    pw = _passphrase(args, confirm=True) if args.include_secrets else None
    cf = Path(args.config)
    with actions.run_lock(root):  # no job may be mid-write while the state is captured
        if args.auto:  # what the scheduler does: verified, no secrets, and older *scheduled* backups beyond --keep are removed
            rep, pruned = backup.create_auto(root, args.keep, cf if cf.is_file() else None)
            if pruned:
                print(f"removed {len(pruned)} older scheduled backup(s) beyond the {args.keep} kept")
        else:
            rep = backup.create(root, Path(args.out) if args.out else None, args.include_secrets, pw, cf if cf.is_file() else None)
    _print_report(rep)
    dest = destinations.from_cfg(cfg)
    if dest:
        try:
            gone = destinations.push(rep.path, dest, args.keep if args.auto else None)
            print(f"copied to {dest.describe()} and checked there" + (f"; removed {len(gone)} older scheduled backup(s) there" if gone else ""))
        except destinations.DestError as e:
            print(f"the local backup is fine, but the copy to {dest.describe()} failed: {e}", file=sys.stderr)
            return 1
    if not args.include_secrets:
        print("  (re-run with --include-secrets to also keep your API keys and UI password, encrypted with a passphrase)")
    return 0


def cmd_backup_destination(args) -> int:
    cfg = _config(args, required=False)
    dest = destinations.from_cfg(cfg)
    if dest is None:
        print("no backup destination is set (Settings > Backup destination, or backup_destination: in the config)")
        return 1
    try:
        print(dest.check())
    except destinations.DestError as e:
        print(f"{dest.describe()}: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_backup_verify(args) -> int:
    rep = backup.verify(Path(args.file), _passphrase(args) if not args.no_passphrase else None)
    print("OK: every file matches its checksum, nothing missing, nothing extra.")
    _print_report(rep)
    return 0


def cmd_backup_restore(args) -> int:
    cfg = _config(args, required=False)
    root = data_dir(cfg)
    f = Path(args.file)
    pw = _passphrase(args) if backup.read_manifest(f).get("has_secrets") and not args.no_passphrase else None
    with actions.run_lock(root):
        rep = backup.restore(f, root, force=args.force, dry_run=args.dry_run, passphrase=pw)
    _print_report(rep)
    if not args.dry_run:
        print("Restored. If the web UI is running, restart it to pick the restored state up.")
    return 0


def cmd_backup_list(args) -> int:
    root = data_dir(_config(args, required=False))
    rows = backup.list_backups(root)
    loc = backup.location_info(root)
    print(f"backup folder: {loc['path']}" + ("" if loc["source"] == "default" else f"  (set by {'Settings' if loc['source'] == 'settings' else 'LAMINATOR_BACKUP_DIR'})"))
    if not loc["ok"]:
        print(f"  problem: {loc['error']}")
    if loc["stranded"]:
        print(f"  {loc['stranded']} backup(s) are still in {loc['default']}; move them in Settings > Backup and restore")
    if not rows:
        print("no backups yet (laminator backup create)")
    for r in rows:
        if r.notes and not r.created:
            print(f"{r.path.name}: {r.notes[0]}")
        else:
            print(f"{r.path.name}  {r.size / 1e6:.1f} MB  {r.created}  " + ", ".join(f"{k}:{v['posters']}p/{v['meta']}m" for k, v in r.servers.items())
                  + ("  [with encrypted keys]" if r.has_secrets else ""))
    return 0


def cmd_export(args) -> int:
    cfg = _config(args)
    kinds = chosen_servers(cfg, args)
    bad = 0
    for kind in kinds:
        ws = actions.open_workspace(cfg, need_mdblist=False, kind=kind)
        dest = Path(args.out) if args.out and len(kinds) == 1 else None
        bad += _emit(export.run_export(ws, ws.root, args.which, args.layout, args.naming, args.library, dest), _tag(kinds, kind))
    return 1 if bad else 0


def cmd_trash(args) -> int:
    cfg = _config(args)
    kinds = chosen_servers(cfg, args, default_all=True)
    lim = cleanup.Limits.from_cfg(cfg)
    bad = 0
    for kind in kinds:
        ws = actions.open_workspace(cfg, need_mdblist=False, kind=kind)
        t = _tag(kinds, kind)
        if args.tcmd == "list":
            batches = trash.list_batches(ws, lim=lim)
            if not batches:
                print(f"{t}the trash is empty")
            for b in batches:
                print(f"{t}{b.stamp} ({b.scope}): {len(b.items)} item(s), {len(b.orphans)} orphan poster(s), {len(b.safety)} safety copy(ies), "
                      f"{b.bytes / 1e6:.1f} MB; deleted for good in {max(b.keep_days_left, 0)} day(s)")
                for it in b.items:
                    print(f"{t}    {it.item_id}  {it.name}  ({len(it.files)} poster file(s))")
            continue
        if not args.item and not args.all:
            raise ConfigError("say what to restore: --item ID, or --all for everything in that batch")
        with actions.run_lock(ws.root):
            bad += _emit(trash.restore(ws, args.stamp, args.item or None, lim=lim), t)
    return 1 if bad else 0


PRIVATE = ("settings.yaml", "secrets.env", "admin.hash", ".secret", "mfa.json", "sessions.json", "audit.log", "audit.log.1",
           "overrides.yaml", "overrides.yaml.prev", "schedule.json", "servers", "backups", "exports", "trash", "cache", "templates", "fonts", "assets")


def tighten_permissions(data: Path) -> int:
    """Make laminator's own files private (files 600, folders 700): older versions created some of them readable by every user on the host.
    Only the names laminator owns are touched (the data folder may be a project folder), and symlinks are never followed."""
    n = 0
    for name in PRIVATE:
        top = data / name
        if not top.exists() or top.is_symlink():
            continue
        for p in [top, *top.rglob("*")] if top.is_dir() else [top]:
            try:
                if p.is_symlink():
                    continue
                want = 0o700 if p.is_dir() else 0o600
                if p.stat().st_mode & 0o777 != want:
                    p.chmod(want)
                    n += 1
            except OSError:
                pass
    return n


def cmd_serve(args) -> int:
    import uvicorn

    from .web import passwords
    from .web.app import build_runtime, create_app

    cfg = _config(args, required=False)  # a fresh container has no config file: the UI sets everything up
    data = data_dir(cfg)
    password = os.environ.get("LAMINATOR_ADMIN_PASSWORD")
    saved = passwords.load(data) is not None  # a password set from Settings also counts
    setup = False
    if not password and not saved:
        if args.insecure_no_auth:
            if args.host not in ("127.0.0.1", "localhost", "::1"):
                raise ConfigError("--insecure-no-auth is only allowed together with --host 127.0.0.1")
        else:
            setup = True  # first start: the UI only offers choosing a password, with a one-time code printed below
    tmp = os.environ.get("TMPDIR")
    if tmp:  # the image points TMPDIR at the data volume (uploads spool there with a read-only root filesystem)
        Path(tmp).mkdir(parents=True, exist_ok=True)
        for old in Path(tmp).glob("*"):
            if old.is_file() and not old.is_symlink():
                old.unlink(missing_ok=True)  # leftovers of an interrupted upload
    tighten_permissions(data)
    if password and len(password) < 12 and not saved:
        print("warning: LAMINATOR_ADMIN_PASSWORD is shorter than 12 characters; use a longer one (or set one in Settings) "
              "before making the UI reachable from outside your network", file=sys.stderr)
    try:
        from .web.app import trusted_networks
        trusted_networks(os.environ.get("LAMINATOR_TRUSTED_PROXIES", ""))
    except ValueError as e:
        raise ConfigError(f"LAMINATOR_TRUSTED_PROXIES: {e}")
    runtime = build_runtime(cfg, data)
    sched = None if args.no_schedule or (args.insecure_no_auth and not (password or saved)) else schedule.parse(cfg)  # starts once a password exists
    app = create_app(cfg, runtime[0], data, password or None, runtime[1], sched, run_scheduler=True,
                     rebuild=lambda: build_runtime(cfg, data), config_path=Path(args.config), setup=setup)
    connected = ", ".join(runtime[0]) or "none"
    if setup:
        print("\n" + "=" * 62 + f"\n  FIRST START: open the web UI and enter this setup code\n  to choose your password:   {app.state.setup_code}\n" + "=" * 62 + "\n")
    print(f"laminator UI on http://{args.host}:{args.port}  (data folder {data.resolve()}; "
          f"{'schedule on' if sched else 'no schedule'}{'' if (password or saved or setup) else ', read-only'}; "
          f"servers connected: {connected}{'' if runtime[0] else ' - set them up under Settings'})")
    # proxy headers are handled by the app itself (only from LAMINATOR_TRUSTED_PROXIES); no "server: uvicorn" banner
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning", proxy_headers=False, server_header=False)
    return 0


# ---- argument parsing ---------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="laminator")
    p.add_argument("-c", "--config", default="config.local.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)

    srv_parent = argparse.ArgumentParser(add_help=False)
    srv_parent.add_argument("--server", choices=["emby", "jellyfin", "all"],
                            help="which media server (required when both are configured)")

    s = sub.add_parser("scan", help="list library items and their artwork", parents=[srv_parent])
    s.add_argument("--library")
    s.add_argument("--limit", type=int)
    s.add_argument("--list-libraries", action="store_true")
    s.set_defaults(fn=cmd_scan)

    def poster_args(sp):
        sp.add_argument("--library")
        sp.add_argument("--template", default="default", help="template name or YAML file")
        sp.add_argument("--overrides", default="overrides.yaml")

    r = sub.add_parser("render", help="render overlays into <server data>/out (uploads nothing)", parents=[srv_parent])
    poster_args(r)
    r.add_argument("--limit", type=int)
    r.add_argument("--dry-run", action="store_true", default=True)
    r.set_defaults(fn=cmd_render)

    a = sub.add_parser("apply", help="upload overlays to the server", parents=[srv_parent])
    poster_args(a)
    a.add_argument("--item", help="only this item (name or id)")
    a.add_argument("--all", action="store_true", help="really apply to every library")
    a.add_argument("--changed", action="store_true",
                   help="only new items and items whose poster changed; skips everything else without per-item requests")
    a.set_defaults(fn=cmd_apply)

    ex = sub.add_parser("export", help="a zip of posters laid out next to the media files they belong to", parents=[srv_parent])
    ex.add_argument("--which", choices=export.WHICH, default="current", help="current = as on the server now (with scores); original = saved originals, no scores")
    ex.add_argument("--layout", choices=export.LAYOUTS, default="location", help="top folder: the library's folder name (default), the library name, or none")
    ex.add_argument("--naming", choices=export.NAMINGS, default="auto", help="auto: poster.jpg when a folder holds one film, <file>-poster.jpg otherwise")
    ex.add_argument("--library", help="only this library")
    ex.add_argument("--out", help="file to write (default: <data>/exports/laminator-posters-<server>-<time>.zip)")
    ex.set_defaults(fn=cmd_export)

    tr = sub.add_parser("trash", help="see what cleanup moved to the trash, and put it back")
    tsub = tr.add_subparsers(dest="tcmd", required=True)
    t1 = tsub.add_parser("list", help="what is in the trash and how long it is kept", parents=[srv_parent])
    t1.set_defaults(fn=cmd_trash)
    t2 = tsub.add_parser("restore", help="put an item (or a whole batch) back; nothing is overwritten", parents=[srv_parent])
    t2.add_argument("stamp", help="the batch name from `trash list`, like 20261101-161956")
    t2.add_argument("--item", help="only this item id")
    t2.add_argument("--all", action="store_true", help="everything in that batch")
    t2.set_defaults(fn=cmd_trash)

    cl = sub.add_parser("cleanup", help="remove what laminator no longer needs (plan first; --yes to do it)", parents=[srv_parent])
    cl.add_argument("--yes", action="store_true", help="actually clean up (default: only show the plan)")
    cl.add_argument("--safety-days", type=int, help="also move archive-*/pre-restore-* safety copies older than this many days to trash")
    cl.set_defaults(fn=cmd_cleanup)

    mt = sub.add_parser("maintain", help="full pass over every library (what the monthly schedule runs); every server by default",
                        parents=[srv_parent])
    poster_args(mt)
    mt.add_argument("--no-cleanup", action="store_true", help="skip the cleanup step")
    mt.set_defaults(fn=cmd_maintain)

    bk = sub.add_parser("backup", help="back up / restore laminator's own data (posters' originals, state, settings)")
    bsub = bk.add_subparsers(dest="bcmd", required=True)
    pp = argparse.ArgumentParser(add_help=False)
    pp.add_argument("--passphrase-env", default="LAMINATOR_BACKUP_PASSPHRASE",
                    help="environment variable holding the passphrase (default %(default)s); otherwise you are asked. Never a command-line argument.")
    b1 = bsub.add_parser("create", help="make a backup archive", parents=[pp])
    b1.add_argument("--out", help="file to write (default: <data>/backups/laminator-backup-<time>.tar.gz)")
    b1.add_argument("--include-secrets", action="store_true", help="also keep API keys and the UI password hash, encrypted with a passphrase")
    b1.add_argument("--auto", action="store_true", help="a scheduled-style backup (verified, no secrets); older scheduled ones beyond --keep are removed")
    b1.add_argument("--keep", type=int, default=5, help="with --auto: how many scheduled backups to keep (default 5)")
    b1.set_defaults(fn=cmd_backup_create)
    bd = bsub.add_parser("destination", help="test the configured backup destination (writes and deletes a small test file)")
    bd.set_defaults(fn=cmd_backup_destination)
    b2 = bsub.add_parser("verify", help="check a backup: names, checksums, nothing missing or extra (writes nothing)", parents=[pp])
    b2.add_argument("file")
    b2.add_argument("--no-passphrase", action="store_true", help="do not check the secrets block")
    b2.set_defaults(fn=cmd_backup_verify)
    b3 = bsub.add_parser("restore", help="restore a backup into the data folder", parents=[pp])
    b3.add_argument("file")
    b3.add_argument("--dry-run", action="store_true", help="verify and show what would happen; write nothing")
    b3.add_argument("--force", action="store_true", help="replace existing data (the old files are moved to pre-restore-<time>/, not deleted)")
    b3.add_argument("--no-passphrase", action="store_true", help="restore everything except the encrypted keys/password")
    b3.set_defaults(fn=cmd_backup_restore)
    b4 = bsub.add_parser("list", help="list the backups in <data>/backups")
    b4.set_defaults(fn=cmd_backup_list)

    rp = sub.add_parser("reset-password", help="remove the UI password saved from Settings (recovery)")
    rp.set_defaults(fn=cmd_reset_password)
    rm = sub.add_parser("reset-mfa", help="turn two-factor sign-in off (recovery when the authenticator is lost)")
    rm.set_defaults(fn=cmd_reset_mfa)

    x = sub.add_parser("restore", help="put original posters and metadata back", parents=[srv_parent])
    x.add_argument("--item", help="only this item (name or id)")
    x.add_argument("--all", action="store_true", help="really restore every item we changed")
    x.add_argument("--what", choices=["posters", "meta", "all"], default="all")
    x.add_argument("--force", action="store_true", help="also revert metadata that was edited in the server since")
    x.set_defaults(fn=cmd_restore)

    m = sub.add_parser("meta", help="metadata overrides")
    msub = m.add_subparsers(dest="mcmd", required=True)
    for name, fn, hlp in (("diff", cmd_meta_diff, "show what the overrides file would change"),
                          ("apply", cmd_meta_apply, "write the overrides and lock those fields")):
        mp = msub.add_parser(name, help=hlp, parents=[srv_parent])
        mp.add_argument("--overrides", default="overrides.yaml")
        mp.add_argument("--item", help="only this item (name or id)")
        if name == "apply":
            mp.add_argument("--force", action="store_true", help="overwrite values edited in the server since")
        mp.set_defaults(fn=fn)
    v = sub.add_parser("serve", help="web UI")
    v.add_argument("--host", default="127.0.0.1")
    v.add_argument("--port", type=int, default=5264)
    v.add_argument("--insecure-no-auth", action="store_true", help="no password; localhost only")
    v.add_argument("--no-schedule", action="store_true", help="do not run the scheduled checks/maintenance")
    v.set_defaults(fn=cmd_serve)
    return p


def main(argv=None) -> int:
    os.umask(0o077)  # everything laminator writes (state, posters, settings, backups) is private to its own user
    args = build_parser().parse_args(argv)
    load_dotenv()
    try:
        load_secret_files()  # NAME_FILE (Docker secrets) -> NAME
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    try:  # keys saved from the web UI override .env and the environment
        secrets_store.load_into_env(data_dir(_config(args, required=False)))
    except (ConfigError, OSError):
        pass
    try:
        return args.fn(args)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    except metamod.OverrideError as e:
        print(f"overrides error: {e}", file=sys.stderr)
        return 2
    except actions.Busy as e:
        print(f"busy: {e}", file=sys.stderr)
        return 3
    except FileNotFoundError as e:
        print(f"not found: {e}", file=sys.stderr)
        return 2
    except backup.BackupError as e:
        print(f"backup error: {e}", file=sys.stderr)
        return 2
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except RuntimeError as e:  # e.g. an unsafe state migration was refused
        print(f"error: {e}", file=sys.stderr)
        return 2
    except httpx.HTTPError as e:
        print(f"server error: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
