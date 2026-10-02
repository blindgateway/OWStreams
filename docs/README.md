# Stream

A personal media server and a Qt 6 / QML client for it. A small server that
holds your library, your accounts and your per-profile state; a client that
runs on a desktop, a phone, and eventually a TV.

**Status.** Both halves are built and tested: the server at 607 tests, the
client at 116 across 11 suites — of which fifteen run the client against a real
server rather than a canned response. No stubs, no TODOs in core functionality.
Packaging and the screenshot comparison are outstanding; see
[ROADMAP.md](ROADMAP.md). Nothing here is a mock-up or a placeholder, and what
is missing is stated plainly rather than faked.

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
- **The client** — nine screens behind a responsive shell that is a sidebar on
  desktop, a rail on tablet and a bottom bar on phone; a player with resume,
  subtitles, audio-track selection and seek; a session machine that survives the
  server going away mid-session; and credentials in the platform keychain rather
  than a preferences file. See [ROADMAP.md](ROADMAP.md) for the full list.

## Quick start

**Server**

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

**Client**

```bash
cd client
cmake -S . -B build -G Ninja
cmake --build build
./build/stream-client
```

The client asks for a server address on first run. `qmllint` runs as part of
every build and fails it on any warning.

Full instructions in [DEPLOYMENT.md](DEPLOYMENT.md); development setup in
[DEVELOPMENT.md](DEVELOPMENT.md).

## Repository layout

```
backend/     the server — Python, FastAPI, SQLite
client/      the client — Qt 6 Quick, QML, C++ core, CMake
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

- **No external DRM, no anti-piracy service.** The client keeps credentials in
  the platform's secure storage and does not log them. It keeps the player out
  of screenshots and taskbar thumbnails on the two platforms whose system
  enforces it — Windows 10 2004+ and Android — and on X11 and Wayland it
  enforces nothing, because nothing there can be enforced without breaking
  playback. None of it makes the content unrecordable, because no client-side
  code can; the limits are documented rather than papered over. See
  [SECURITY.md](SECURITY.md#screen-capture-and-window-thumbnails).
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
