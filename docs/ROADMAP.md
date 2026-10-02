# Roadmap

An honest account of what exists, what does not, and in what order the rest
should be built. Nothing here is a stub: if something is not built, it is not
started, and the list says so.

## Done

**Server — complete and tested (497 tests, no failures).**

- Layered configuration, validation, and a `0600` config file
- SQLite schema: 24 tables, 3 migrations, FTS5 search, WAL
- Transactions, backup, restore with integrity checking, vacuum
- scrypt password and PIN hashing, signed access tokens, opaque rotating
  refresh tokens, per-title playback tokens
- Path containment, symlink-safe resolution, slug generation
- Rate limiting (token buckets) and account lockout
- Audit trail with scrubbing
- Filename parsing, library scanning, sidecar discovery, ffprobe integration
- Metadata providers: local sidecars (default, offline), TMDB, IMDb, OMDb, with
  a merge policy
- 73 documented API endpoints across auth, profiles, library, playback and
  administration, with a uniform error contract
- Full-text search, facets, browse, filter, sort, pagination
- Per-profile favourites, history, continue-watching, ratings
- Range streaming with the full RFC 9110 grammar
- HTTP Range streaming, progress reporting, resume
- 14 CLI subcommands sharing the same services as the API
- 497 tests: unit, integration, API, security, media, CLI

**Documentation — complete.**

`README`, `ARCHITECTURE`, `API`, `DATABASE`, `SECURITY`, `DEPLOYMENT`,
`DEVELOPMENT`, `TESTING`, `TROUBLESHOOTING`, `ROADMAP`, `AGENT`, `CLAUDE`.

## Not started

**The client. `client/` contains two empty directories.**

This is the largest remaining piece and the reason the project is not finished.
Qt 6 Quick and QML, C++ core, one codebase for desktop, tablet and phone.

### Client, in order

1. **Skeleton and build.** `CMakeLists.txt`, `main.cpp`, a `qt_add_qml_module`,
   and a verified configure-and-build on this machine (Qt 6.11.2 devel packages
   are installed and a trivial QML app compiles and links).
2. **HTTP API client in C++.** `QNetworkAccessManager` with a refresh-token
   interceptor, typed models over the v1 API, and an error taxonomy that maps
   every documented `error` code to a user-facing state. No sleeps, no
   polling where a signal will do.
3. **Secure credential storage.** Platform keychain/Keystore, never a plain
   preferences file. Server URL remembered per profile with the certificate
   policy stored alongside it.
4. **First-run flow.** Server configuration → login → setup (if the server says
   setup is required) → profile creation and selection → PIN.
5. **Design system.** One place defining colour, type scale, spacing, radii,
   motion and elevation; dark and light; no gradients, no glassmorphism, no
   purple-on-dark, no default-Qt-looking controls. Established before any
   screen is written, because retrofitting consistency is what produces the
   generic look the brief explicitly rejects.
6. **Navigation shell.** Sidebar on desktop, rail on tablet, bottom bar on
   phone — three arrangements of one component tree, not three UIs.
7. **Home.** Continue watching, recently added, recommended, favourites,
   popular, categories, recently watched. One request (`GET /library/home`), and
   every rail has a real loading, empty and error state.
8. **Library and search.** Browse with filters, detail screen with the full
   metadata record, cast, similar titles, audio/subtitle track pickers.
9. **Player.** `QMediaPlayer` with play/pause, seek, resume, volume, fullscreen,
   subtitle and audio selection, buffering and error states, next/previous.
10. **Settings and account management.** Server configuration, account, password,
    profiles, PINs, active sessions, logout.
11. **Resilience.** Server unreachable, timeout, DNS failure, TLS failure,
    network change mid-stream, expired session, wrong PIN, missing media file —
    each with a designed state, not a dialog.
12. **Client-side protections.** Screenshot blocking and `FLAG_SECURE` where the
    platform allows; documented limits where it does not.
13. **Packaging.** Linux, Windows 10+, Android 12+.

### After the client

- Series and season support in the UI (the schema already has `content_type` and
  `parent_media_id`; `media_seasons` is a future migration)
- Recommendations tuned against real usage
- Android TV
- Transcoding — explicitly *not* planned; see below

## Deliberately not planned

**Transcoding.** The server streams the file as stored. A design that transcodes
needs a job queue, a cache with eviction, a lot of CPU on a four-core box that
is also serving, and a whole new class of failure. If a device cannot play a
codec, the honest answer is "this file is not playable on this device", and the
UI should say so. A much cheaper mitigation exists and is worth doing: prefer a
compatible stream via the `media_streams` table where a file has one, and tell
the user plainly when it does not.

**DRM or any external anti-piracy service.** Out of scope by decision, and
[SECURITY.md](SECURITY.md) is explicit about why claiming otherwise would be
dishonest.

**Microservices, Kubernetes, Redis, a message broker.** One box, ten users.

**A cloud account, a phone-home, telemetry.** The server talks to nothing.

**Multi-writer scaling.** SQLite in WAL mode is a single-writer design. If this
ever needed to run as several writer hosts, the repositories are the only thing
that would change — the SQL is already isolated in one module.

## Sequenced next steps

1. Client skeleton and a green build.
2. API client and secure storage.
3. Design system.
4. First-run, login and profile flows — the acceptance script in the brief
   starts here.
5. Home and library.
6. Player and resume.
7. The full acceptance walkthrough, with every failure case in it.
8. Screenshot comparison against the supplied references, iterating until they
   match.
9. Windows and Android builds.
