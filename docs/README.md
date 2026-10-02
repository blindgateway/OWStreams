# Stream

A personal media server and a Qt 6 / QML client for it. A small server that
holds your library, your accounts and your per-profile state; a client that
runs on a desktop, a phone, and eventually a TV.

**Status.** The server is complete and tested: 497 tests, no stubs, no TODOs in
core functionality. The client is not built yet — see [ROADMAP.md](ROADMAP.md).
Nothing in this repository is a mock-up or a placeholder; what is missing is
stated plainly rather than faked.

## What works today

- **Accounts and sessions** — login, remember-me, refresh rotation, logout,
  logout-everywhere, device list, per-device revocation, password change that
  kills other sessions.
- **Profiles** — many per account, each with a name, an avatar, an optional
  PIN (scrypt-hashed, never stored in the clear) and its own watch history,
  favourites, ratings and continue-watching. Each profile is a separate
  household member's view.
- **Library** — browse, filter by genre/tag/year/decade/rating/runtime/
  resolution/HDR/cast, sort, paginate. A title carries the full record: both
  titles, description, year, genres, tags, poster, backdrop, runtime, cast,
  director, rating *and its source*, release date, file, size, resolution,
  codec, audio and subtitle tracks, added and modified dates.
- **Search** — full-text over titles, descriptions, genres, tags and cast,
  ranked, with type-ahead. Usable at 1000+ titles because it is an FTS5 index,
  not a scan.
- **Playback** — short-lived per-title tokens, HTTP Range streaming with the
  full RFC 9110 range grammar, seek, resume, progress reporting, buffering and
  missing-file states.
- **Import** — point it at a directory. It walks the tree, parses real release
  names, finds sidecars, probes with ffprobe, fetches metadata and writes rows.
  Re-running updates rather than duplicating. Also driven by CLI, REST, or as
  a Python module.
- **Administration** — accounts, metadata editing, import, verify, reindex,
  audit log, backup, restore, vacuum, health.
- **Privacy and safety** — see [SECURITY.md](SECURITY.md), including an honest
  account of what the system does *not* protect against.

## Quick start

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'

streamserver init-config -c server.toml --root /srv/media/movies
streamserver init-db
streamserver add-user phuree --admin
streamserver serve
```

Then `http://localhost:8484/api/docs` for the API, and:

```bash
streamserver scan --root /srv/media/movies
streamserver list
```

Full instructions in [DEPLOYMENT.md](DEPLOYMENT.md); development setup in
[DEVELOPMENT.md](DEVELOPMENT.md).

## Repository layout

```
backend/     the server — Python, FastAPI, SQLite
client/      the Qt 6 / QML application (not built yet)
docs/        this documentation
scripts/     small maintenance utilities
```

## Documentation

| Document | What it covers |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | How the pieces fit, and why they are this way |
| [API.md](API.md) | Every endpoint, with its auth requirement, versioning policy and error contract |
| [DATABASE.md](DATABASE.md) | The schema, indexes, migrations, backup and restore |
| [SECURITY.md](SECURITY.md) | The threat model, what is enforced, and what is honestly not |
| [DEPLOYMENT.md](DEPLOYMENT.md) | Running it on the target box, backups, upgrades |
| [DEVELOPMENT.md](DEVELOPMENT.md) | Building, testing, conventions |
| [TESTING.md](TESTING.md) | What is tested, how, and what is not covered |
| [TROUBLESHOOTING.md](TROUBLESHOOTING.md) | Symptoms and causes |
| [ROADMAP.md](ROADMAP.md) | What is done, what is next, what is deliberately not |
| [AGENT.md](AGENT.md) | Working in this repository — for humans and for agents |
| [CLAUDE.md](CLAUDE.md) | The same, for Claude Code |

## Design commitments

A few things this project will not do, because they would be worse than the
alternative:

- **No external DRM, no anti-piracy service.** The client blocks screenshots
  and screen capture where the platform allows it, keeps credentials in the
  platform's secure storage, and does not log them. It does **not** claim to
  make the content unrecordable, because no client-side code can. The limits
  are documented rather than papered over.
- **No microservices.** One Python process, one SQLite file. At ten concurrent
  users on four cores, a message queue would add failure modes and no capacity.
- **No cloud services, no accounts, no telemetry.** It runs on your hardware and
  talks to nothing.
- **No client-side authorization.** Every check that matters is on the server,
  because a client-side check is a suggestion.
- **No fake documentation.** Every document here describes code that exists.
  Where something is not built, it says so.

## Requirements

- Server: Python 3.11+, Linux (developed on Fedora 44, targeting Debian
  stable), 4 cores / 4 GB RAM / 10 GB SSD + 1 TB HDD.
- Client: Qt 6.5+, a C++17 compiler, CMake 3.21+; targets Android 12+,
  Windows 10+, and Linux desktop.
- `ffprobe` recommended, optional.

## Licence

Private project. Not distributed, not licensed for redistribution.
