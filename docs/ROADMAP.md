# Roadmap

An honest account of what exists, what does not, and in what order the rest
should be built. Nothing here is a stub: if something is not built, it is not
started, and the list says so.

## Done

**Server — complete and tested (607 tests, no failures).**

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
- 607 tests: unit, integration, API, security, media, CLI

**Client — builds, runs, and is tested (122 tests across 12 suites).**

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
- **Tests.** 104 unit assertions over eleven suites, plus `tst_e2e.cpp`: eighteen
  tests against the *production* server, started by `tests/e2e/harness.py` with
  a real config file, a real database, a real account and a real clip ffmpeg
  generated, sidecar and all. It has found fourteen defects the other suites
  could not — the last four of them in one commit, which is what it is for.
- **Capture protection.** `CaptureGuard` keeps the player out of screenshots,
  screen recordings and taskbar previews on the two platforms whose system
  enforces it, and reports — in the settings screen, per device — exactly what
  it got. See
  [SECURITY.md](SECURITY.md#screen-capture-and-window-thumbnails) for the
  per-platform table and why X11 gets nothing.

**Documentation — complete.**

`README`, `ARCHITECTURE`, `API`, `DATABASE`, `SECURITY`, `DEPLOYMENT`,
`DEVELOPMENT`, `TESTING`, `TROUBLESHOOTING`, `ROADMAP`, `AGENT`, `CLAUDE`.

## Not started

In order, with what each one needs.

1. **Packaging.** Linux first (the deployment target is a Fedora 44 server), then
   Windows 10+ and Android 12+. Needs an install target, an application icon,
   and — the part that is actually hard — the Qt runtime.

   The blocker is specific and worth recording, because it is not a missing
   package: **Qt's own deployment support does not work unmodified on this
   Qt and this distribution, and it fails *quietly*.** Four separate things
   are wrong, and each has to be worked around:

   - the generic deploy tool excludes `/lib` and `/lib64` from its dependency
     scan (the usual "do not bundle the system C library" default), and on
     Fedora that is where everything is — libc, libstdc++, every library Qt
     links. It resolves nothing, reports success, and writes an empty tree;
   - `POST_EXCLUDE_REGEXES` is *silently ignored*, because the function builds
     the exclusion list itself from a variable the caller cannot see;
   - the QML modules are collected by a different function,
     `qt6_deploy_qml_imports`, which `qt_generate_deploy_app_script` never
     calls on this path, and there is no `qt6_generate_deploy_qml_import_
     script` to wrap it — so the script that calls it has to be written by
     hand;
   - the generated support file unconditionally sets
     `__QT_DEPLOY_MUST_ADJUST_PLUGINS_RPATH`, and the adjustment is CMake's
     `file(RPATH_SET)`, which *refuses* a file that has no existing RUNPATH —
     which is every rpm-packaged Qt plugin. The deploy step dies after
     copying the tree.

   With all four worked around the tree does assemble and the application
   aborts at startup inside `QGuiApplication`'s constructor, which is the
   state this was left in rather than a shipped result. The likely remaining
   cause is that only the *xcb* platform plugin is deployed and the headless
   `offscreen` plugin the build machine needs is not among them; that is the
   next thing to establish, and the tree is 380 MB, which is the other thing
   worth fixing.

   The alternatives to working around it, in order of preference: a
   distribution-provided `linuxdeployqt`; a Qt build whose plugins carry a
   RUNPATH; or AppImage, which sidesteps the question by shipping a runtime
   nobody has to reconcile. Windows (`windeployqt`) and Android are expected
   to be simpler — neither goes near this code.
2. ~~**Client-side protections.**~~ **Done, and the honest answer is "on two of
   four platforms".** `CaptureGuard` sets `WDA_EXCLUDEFROMCAPTURE` on Windows
   10 2004+ and `FLAG_SECURE` on Android — the two platforms where the system
   enforces it. It sets nothing on X11, where the flag that works also stops the
   window receiving input, and nothing on Wayland, whose protocol has no such
   mechanism. The settings screen shows this device's own answer, and
   [SECURITY.md](SECURITY.md#screen-capture-and-window-thumbnails) has the
   per-platform table. What remains is a verification pass: the Windows and
   Android paths have never been *run*, only compiled for, because this machine
   is neither.
3. **The full acceptance walkthrough.** Partly done, and the part that is done
   found fourteen defects: the end-to-end suite now covers the filter row, a
   server that goes away mid-session, a profile behind a PIN, and the whole
   `X-Profile-Id` chain, against the real server. What remains is the *manual*
   pass — every failure case in the brief, driven through the UI by a person,
   with the application's own log read afterwards. It is blocked on packaging
   (item 1), because "run the packaged build" is the point of it, and the QML
   layer is not covered by any test: nothing renders an `Item` and asserts on
   it. Running the application and reading what Qt logged is what found six
   more defects, and it is not a substitute for a person pressing the buttons.
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