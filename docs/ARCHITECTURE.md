# Architecture

## What this is

A personal media server and a Qt 6 / QML client for it. The server owns the
library, the credentials, the per-profile state and the bytes; the client owns
the presentation and the decoder. Everything interesting happens on the server,
because the server is the thing that has to keep working.

```
┌──────────────────────────────┐        ┌──────────────────────────────────┐
│  Stream client               │        │  streamserver (Python/FastAPI)   │
│                              │        │                                  │
│  Qt 6 Quick / QML UI         │  HTTPS │  api/          routes, deps      │
│  Qt Multimedia playback      │◄──────►│  services/     use cases         │
│  C++ core: API client,       │  JSON  │  db/           SQLite + SQL      │
│         config, tokens       │ +Range │  media/        scan, probe, serve │
│                              │        │  metadata/     providers         │
│  desktop │ Android phone/TV  │        │  security/     crypto, paths,    │
└──────────────────────────────┘        │                 audit, limits     │
                                        └──────────────┬───────────────────┘
                                                       │ absolute paths only
                                        ┌──────────────▼───────────────────┐
                                        │  library roots (HDD)            │
                                        │  /mnt/media/movies …            │
                                        └──────────────────────────────────┘
```

The client never names a file. It asks for a media id, gets a short-lived
playback token, and streams `/api/v1/stream/{id}` with HTTP Range requests. The
server resolves the id to a path and re-validates that path against the
configured roots at the moment of the request. There is no route that takes a
filesystem path from a client at all.

## Why these choices

**Python + FastAPI on the server.** The brief left the framework open. FastAPI
gives a typed, self-documenting API with request validation for free, runs on a
single process with no broker, and is fast enough to stream 10 concurrent
sessions of byte ranges off a single 4-core box — the work per request is a
`stat`, a `seek` and an 8 MiB read, not computation.

**SQLite in WAL mode.** The target is one box, one writer, ~10 users. WAL lets
readers (the API) run while the importer writes, which removes the whole class
of "the UI froze because a scan was running" failures. A separate database
daemon would add a thing to supervise, credentials on the network, and a backup
routine — for no gain at this size. See [DATABASE.md](DATABASE.md).

**Qt 6 Quick / QML on the client.** One QML codebase, three form factors. The
responsive work is in QML components with explicit breakpoints rather than three
separate UIs: a sidebar on desktop, a rail on a tablet, a bottom bar on a phone.
Playback is `QMediaPlayer` — the platform decoders are better than anything we
would write, and on Android they are the only ones with hardware acceleration.

**No microservices, no Redis, no message queue.** The rate limiter is
in-process token buckets with a bounded LRU; the audit trail is a SQLite table;
jobs are synchronous requests. At this scale each of those would be a separate
failure mode to operate for no throughput gain.

## Backend layout

```
backend/streamserver/
├── config.py            layered config (TOML → env → overrides) + validation
├── logging_setup.py     console and rotating file logs
├── cli.py               every subcommand; shares the services with the API
├── db/
│   ├── database.py      connection management, transactions, backup, restore
│   ├── migrations.py    3 migrations, 24 tables, the FTS5 index
│   └── repositories.py  all the SQL, in one place
├── media/
│   ├── naming.py        release-name → title, year, quality, audio, HDR, group
│   ├── scanner.py       library walk, sidecar discovery
│   ├── probe.py         ffprobe wrapper → technical metadata
│   └── streaming.py     Range parsing, chunked reads, headers
├── metadata/providers.py  local sidecars, TMDB, IMDb, OMDb, merge policy
├── security/
│   ├── passwords.py     scrypt for passwords and PINs
│   ├── tokens.py        signed access tokens, opaque refresh tokens, playback tokens
│   ├── paths.py         root registry, containment, slugify
│   ├── audit.py         security event log with scrubbing
│   ├── rate_limit.py    token buckets + account lockout
│   └── audit_sink.py    the file the audit trail is also written to
├── services/            auth, profiles, history, library, importer
└── api/
    ├── app.py           application factory and middleware
    ├── deps.py          authentication, authorization, rate limits
    ├── errors.py        the one error shape
    ├── schemas.py       request/response models
    └── v1/              auth, profiles, library, media+stream, admin
```

### The layering rule

`api/` never contains SQL. `db/repositories.py` never contains a business rule.
`services/` never touches `fastapi`. The CLI and the HTTP API call the same
services, which is why importing over `curl` and importing over `streamserver
import` cannot diverge — the CLI is not a second implementation, it is the same
one with a different front end.

## Request path

A browse request, end to end:

1. `RequestContextMiddleware` assigns a request id, times the request, and
   writes one log line per *session* rather than per Range request.
2. `SecurityHeadersMiddleware` and `TrustedHostMiddleware` add response
   headers and reject unexpected `Host` values.
3. `require_caller` verifies the bearer token's signature, type and expiry, then
   that the account is active and the session not revoked, then applies the
   `api` rate-limit bucket. Failure is a 401 with `WWW-Authenticate: Bearer`.
4. `require_profile` resolves `X-Profile-Id` to a profile *owned by this
   caller*. A foreign profile id is a 404, never a 403 — the response must not
   confirm that somebody else's profile exists.
5. The handler calls a service. The service runs one or more repository calls
   inside `db.read()` / `db.write()`.
6. Pydantic materialises the response. `sqlite3.Row` is not serialisable, so
   repositories return dicts; a row leaking to a response model is a 500 that
   the tests would catch.

A streaming request diverges at step 3: `require_stream_caller` accepts *either*
an access token or a playback token, and the handler additionally checks that a
playback token names this media id and this profile.

## Why authentication is a dependency and not a decorator

Every route takes its credentials as a typed parameter
(`caller: CallerDep`, `profile_id: ProfileDep`, `admin: AdminDep`). The reasons
are concrete:

- FastAPI resolves the dependency before the body of the handler runs, so a
  handler physically cannot forget to check. A route that forgot an
  `@authenticated` decorator would be a 500-line-of-boilerplate bug waiting to
  happen; a route that forgets a *parameter* is immediately visible in the
  signature.
- The OpenAPI document gets the security requirement for free.
- `tests/test_api_admin.py::test_non_admin_cannot_reach_any_admin_route` walks
  the route inventory and asserts every admin route is guarded, so a new one
  cannot slip in.

And the corollary, which cost more: **a dependency that no route takes is not a
control.** `require_uncapped` — the gate that stops an account still on the
shipped default password from writing anything — was written, documented,
exported and wired into nothing, so the account could rate, favourite, stream
and write watch history. `tests/test_client_routes.py` now walks the routers
and asserts that every mutating route resolves its caller through it. The same
argument applies to profile strictness: `ProfileDep` falls back to the
account's first profile, which is right for the catalogue and wrong for
anything that is per profile by definition, so those routes take
`OwnProfileDep`, which refuses an unnamed profile instead.

One subtlety, learned the hard way and encoded in `deps.py`: under
`from __future__ import annotations` an `Annotated[...]` alias written as a
string annotation is *never resolved* by FastAPI. The dependency is silently
not wired and FastAPI instead looks for a query parameter named after the
argument. The aliases are therefore defined at the bottom of `deps.py` and
imported by value everywhere. A test asserts the wiring, because this failure
mode is invisible until someone notices a 422.

## The streaming path

```
POST /api/v1/media/{id}/playback     →  playback token (10 min, bound to id+profile)
GET  /api/v1/stream/{id}             →  206 Partial Content, 8 MiB chunks
   Range: bytes=1048576-             →  open O_NOFOLLOW, seek, read, close
```

Details that matter:

- The token is bound to one media row and one profile. A token minted for one
  title cannot read the library or reach the admin API — the two route groups
  use *different* dependency aliases for exactly this reason (sharing one alias
  lets FastAPI's `use_cache=True` hand the session-authenticated caller to the
  stream route, which was a real privilege escalation, now covered by
  `tests/test_api_playback.py`).
- The path is re-resolved and re-validated against the roots on every request,
  not cached from import time, so moving a root or replacing a symlink after
  the fact takes effect immediately.
- The file is opened with `O_NOFOLLOW`, so a symlink swapped in between
  validation and open cannot redirect the read.
- Range parsing follows RFC 9110 §14: `bytes=a-b`, `bytes=a-`, `bytes=-n`, and
  416 with `Content-Range: bytes */size` for anything unsatisfiable.
- One request per 8 MiB means a 40 GB film is ~5000 requests. That is why
  `RequestContextMiddleware` collapses logging per session: at ~200 bytes a
  line, per-request logging would fill the 10 GB SSD and bury everything else.

## The client

The client is a thin shell over three C++ objects, and the shell is the only
part QML can see as presentation.

```
App.qml ── Main.qml ── Shell.qml (sidebar │ rail │ bottom bar)
                      └── one screen at a time:
                          Server, Login, ChangePassword, Profile,
                          Home, MediaGrid (library │ search │ favourites
                          │ history), Detail, Player, Settings

AppController          which screen, and what it asked for
  ├── SessionController   which state the session is in
  │     └── ApiClient     one HTTP request, one typed answer
  ├── MediaListModel      a page of titles
  ├── HomeScreenModel      eight rails, one request
  └── PlaybackController   the player's rules and its progress throttle
```

Three rules, each of which exists because breaking it was a bug a test found:

- **The QML never talks to the network.** Everything a screen shows comes from
  a C++ object, so every rule in those objects is testable without a window,
  a platform plugin or a display — which is what makes seven suites runnable
  on a build machine.
- **A credential is attached by one object, on a narrow scope.**
  `CredentialAccessManager` adds `Authorization` *and* `X-Profile-Id` to the
  artwork and streaming routes only, because those are the two `Image` and the
  media backend fetch by URL and cannot be given a header. Every other request
  is built by `ApiClient::send`, which writes the profile header itself. The
  scope is an explicit prefix list rather than "not `/api`", because the safe
  default for a request the code does not recognise is *no credential*.
- **An id travels in the path, and the path is what the answer is about.**
  Every request body is a `StrictModel` with `extra="forbid"`, so an id in a
  body is a 422 — and the client's `handle()` reports the *intent* rather than
  the server's answer, so the failure is invisible in the UI. Three routes end
  in a verb rather than the id, which is why `Pending` carries the id
  explicitly rather than reading it back out of the path.

## Media import

```
file → scan → parse name → find sidecars → probe with ffprobe → look up metadata → row
```

1. `scanner.iter_media_files` walks the roots. It is iterative rather than
   `os.walk` so a symlinked directory can be *skipped* (an `ln -s /` inside the
   media folder would otherwise turn a scan into a full-disk crawl), and a
   permission error on one directory does not abort the scan. Zero-byte files
   are skipped: they create library rows that can never stream.
2. `naming.parse_filename` extracts title, year, season/episode, quality,
   release, edition, audio codec, HDR format, bit depth and release group. It is
   a pure function with no I/O, because it is the code most likely to be wrong
   on real filenames and therefore the code most worth testing cheaply.
3. `scanner.find_sidecars` looks for `<name>.nfo`, `<name>.json` and the
   Plex/Emby `Title/movie.nfo` layout.
4. `probe.MediaProbe` runs `ffprobe` with `-protocol_whitelist file`, a size cap
   and a timeout. Every field is parsed defensively; a truncated container
   yields `ok=False`, never an exception, so one bad file cannot abort a
   1000-title import.
5. `metadata.providers` looks up the title. The default provider is
   `local` — offline, reading sidecars — because a server that cannot reach the
   internet must still work. TMDB/IMDb/OMDb are available and opt-in, and
   results are cached.
6. The row is written. `media_path` is unique, so a re-scan of an unchanged file
   updates rather than duplicates.

The same `Importer` is reachable from the CLI, from `POST /api/v1/admin/import`,
and from Python. All three share containment, validation and the audit trail.

## Data placement

| Data | Where | Why |
|---|---|---|
| `streamserver.db` | SSD (`storage.data_dir`) | indexed, small, hot |
| `images/` | SSD (`storage.image_dir`) | thousands of small files, read often |
| Backups | SSD (`storage.backup_dir`) | small, cheap to keep, restored often |
| Logs | SSD (`storage.log_dir`) | rotated, bounded |
| Media files | HDD (absolute paths in `media.library_roots`) | large, read once per playback |
| Signing key | `signing.key`, mode 0600 | must not be world-readable |

Nothing large is stored in SQLite. Posters are files keyed by a slug; the
database holds the key, not the bytes.

## Concurrency model

One process. `uvicorn` with async handlers; the only blocking work is
`ffprobe` (bounded by a timeout, run off the event loop) and the media read
inside the streaming response. SQLite access is per-thread with
`isolation_level=None`, so transactions are explicit. Every writer goes through
`Database.write()`, which takes a process-wide `RLock` and opens
`BEGIN IMMEDIATE` — immediate so that two writers fail fast into `busy_timeout`
instead of deadlocking at `COMMIT` with an unretryable `SQLITE_BUSY`.

`VACUUM` and `wal_checkpoint` deliberately take the lock directly rather than
going through `write()`: both need the transaction *closed*, and running them
inside `BEGIN IMMEDIATE` fails with "database table is locked".

## Extension points

- **New metadata source** — implement the provider protocol in
  `metadata/providers.py` and add it to `build_provider`. It must return
  `MediaMetadata` and must not return absolute paths or unescaped control
  characters; `merge_metadata` is where the priority policy lives.
- **New client** — the API is the contract. Use `POST /api/v1/auth/login`, keep
  the refresh token, send `X-Profile-Id` on profile-scoped routes, and request
  a playback token before streaming. Do not write to the database.
- **New API version** — mount a second `APIRouter` under `/api/v2` alongside
  the existing one. `API_VERSION` is the only place the version is written;
  see [API.md](API.md) for the compatibility policy.
- **Android TV** — the client is architected for it: the API has no
  platform-specific routes, playback goes through one token/Range flow, and
  navigation is a rail rather than a pointer-driven surface. The TV application
  is not built; see [ROADMAP.md](ROADMAP.md).

## See also

- [API.md](API.md) — every endpoint, with the auth requirement on each
- [DATABASE.md](DATABASE.md) — the schema, the indexes, migrations
- [SECURITY.md](SECURITY.md) — the threat model and what is *not* claimed
- [DEVELOPMENT.md](DEVELOPMENT.md) — building and running
- [TESTING.md](TESTING.md) — the test suite and what it covers
