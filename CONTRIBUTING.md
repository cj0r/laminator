# Contributing

Thanks for helping out. Bug reports, ideas and pull requests are all welcome.

## Reporting a bug or asking for a feature

Open an issue with the matching form. For a bug, include the laminator version, your media server and its version, and the log of the run that went wrong (**Runs** in the web UI). Logs don't print API keys, but check before you paste.

Security problems go through [SECURITY.md](SECURITY.md), not an issue.

## Working on the code

laminator is Python 3.11 or newer with a FastAPI web UI and plain HTML, CSS and JavaScript (no build step). Set up and run it from a checkout:

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt -e '.[dev]'
.venv/bin/laminator serve          # http://localhost:5264, data in ./
```

Run the tests from the repo root before opening a pull request:

```bash
.venv/bin/pytest -q
```

The tests use small fake Emby and Jellyfin servers and an MDBList stand-in, so they need no real server or API keys. Never put real keys in tests, docs or examples.

## Pull requests

- Keep each pull request to one change, with a test for new behavior or a fixed bug.
- Write settings, messages and docs in plain words for someone who isn't a developer.
- Document new settings in the user guide in `docs/`.
- Add a line to `CHANGELOG.md` under the next version.

## How changes are merged

This repository is a published copy: releases are prepared in a private development repository and copied here as one commit per version. A pull request made here is reviewed and discussed as usual; if it is accepted, the change is applied in the development repository and ships in the next release, with you credited in the changelog. Your commit itself won't appear in this repository's history, and a change made only here would be replaced by the next release.

By contributing you agree your work is released under the project's license, GPL-3.0.
