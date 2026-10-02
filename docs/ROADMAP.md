# Roadmap

An honest account of what exists, what does not, and in what order the rest
should be built. Nothing here is a stub: if something is not built, it is not
started, and the list says so.

## Done

**Server — complete and tested (602 tests, no failures).**

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
- 602 tests: unit, integration, API, security, media, CLI

**Client — builds, runs, and is tested (101 tests across 9 suites).**

Qt 6 Quick / QML, C++ core, CMake. Verified on this machine (Qt 6.11.2 devel
packages, CMake 4.3.0, Ninja); `qmllint` runs as a `POST_BUILD` step and fails
the build on any warning.

- **Build and structure.** `qt_add_qml_module`, `qt_standard_project_setup`,
  `QT_NO_CAST_FROM_ASCII`, `pragma Singleton` for the theme, one `Stream` QML
  module for the C++ types.
- **Design system.** `qml/Theme.qml` — one amber accent, three dark surfaces, a
  4 px spacing scale, five type sizes, three radii, three motion durations.
  Light and dark. No gradients, no glassmorphism, no purple-on-dark, no default
  Qt controls.
- **HTTP client.** `ApiClient` with transparent token refresh and replay, one
  `ApiError` taxonomy for every documented error code, and a
  `CredentialAccessManager` that attaches a bearer token *only* to the artwork
  and streaming routes — the two `Image` and the media backend fetch by URL and
  cannot be given a header.
- **Secure credential storage.** `CredentialStore` over the platform keychain
  (libsecret, Windows Credential Manager, macOS Keychain), never a preferences
  file, with the remembered server URL stored alongside it.
- **Session state machine.** `SessionController` — no server, connecting,
  login, forced password change, profiles, PIN unlock, home, and back, with the
  server-unreachable and mid-session-loss paths designed rather than dialogged.
- **Screens.** Server, login, change password, profile picker, home, library,
  search, detail, player, settings — nine screens, each with real loading, empty
  and error states.
- **Responsive shell.** Sidebar on desktop, rail on tablet, bottom bar on
  phone: three arrangements of one component tree.
- **Player.** `QMediaPlayer` with play/pause, seek, resume, volume, fullscreen,
  subtitle and audio selection, buffering and error states, next/previous, and
  a progress report every fifteen seconds of *watching*.
- **Models.** `MediaListModel` (paged, sortable, filtered), `HomeScreenModel`
  (eight rails from one request), `PlaybackController`.
- **Tests.** 86 unit assertions over eight suites, plus `tst_e2e.cpp`: fifteen
  tests against the *production* server, started by `tests/e2e/harness.py` with
  a real config file, a real database, a real account and a real clip ffmpeg
  generated. It found nine defects the other suites could not.

**Documentation — complete.**

`README`, `ARCHITECTURE`, `API`, `DATABASE`, `SECURITY`, `DEPLOYMENT`,
`DEVELOPMENT`, `TESTING`, `TROUBLESHOOTING`, `ROADMAP`, `AGENT`, `CLAUDE`.

## Not started

In order, with what each one needs.

1. **Packaging.** Linux first (the deployment target is a Fedora 44 server), then
   Windows 10+ and Android 12+. Needs `qt_deploy_runtime_dependencies`, an
   icon, and a per-platform install target. The client currently builds and
   runs from the source tree and nothing more.
2. **Client-side protections.** Screenshot blocking and `FLAG_SECURE` where the
   platform allows it, with the limits documented where it does not. The
   brief is explicit that a limitation must be documented rather than papered
   over: a desktop screenshot is a desktop screenshot, and the honest claim is
   "raises the cost of an accidental capture", not "prevents capture".
3. **The full acceptance walkthrough**, every failure case in the brief run
   against a packaged build rather than from the source tree.
4. **Screenshot comparison** against the supplied references, iterating until
   they match. Blocked on the references: no image has been supplied yet, so
   the visual design has been built to the brief's stated constraints and not
   yet checked against anything.
5. **Series and season support in the UI.** The schema already has
   `content_type` and `parent_media_id`; `media_seasons` is a future migration.
6. **Recommendations tuned against real usage.** The rails are populated from
   the local heuristics the server implements; nothing has been tuned against a
   real viewing history, because there is not one yet.
7. **Android TV.** Architected for, not built.

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