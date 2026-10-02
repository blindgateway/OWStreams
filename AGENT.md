# AGENT.md

Working in this repository — for humans and for coding agents. It describes
what is true of the code, so you do not have to rediscover it.

`CLAUDE.md` is a short pointer to this file.

## State of the repository

- `backend/` — **complete and tested.** 607 tests, no failures, no stubs, no
  TODOs in core functionality.
- `client/` — **builds, runs and is tested.** 113 tests across 11 suites, of
  which 15 run against a real server. Packaging for Windows and Android, and
  the screenshot comparison against the supplied references, are outstanding.
- `docs/` — complete.

Do not assume a feature is missing because it is hard to find. Do not assume it
is present because a docstring says so: several docstrings in this codebase
were once wrong, and a test caught it. **Read the code.**

## Before you change anything

```bash
cd backend
python -m pytest                       # 607 tests, about 2m30s

cd ../client
cmake -S . -B build-test -G Ninja -DSTREAM_BUILD_TESTS=ON   # once
cmake --build build-test && (cd build-test && ctest)       # 113 tests, seconds

# And start the application.  qmllint cannot see a QML layout that cannot
# work: `NavItem.qml` anchored a Row's children as well as the Row, which Qt
# reports once per frame at runtime and nothing reports at build time.
cmake --build build && QT_QPA_PLATFORM=offscreen timeout 10 ./build/stream-client
```

Both suites must be green before you start, and the application must start.
If either is not green on a clean checkout, that is a bug to report, not to
work around.

## Layout and layering

```
backend/streamserver/
├── config.py            layered config + validation
├── cli.py               subcommands; shares services with the API
├── db/                  database.py, migrations.py, repositories.py  (all SQL here)
├── media/               naming, scanner, probe, streaming
├── metadata/            providers
├── security/            passwords, tokens, paths, audit, rate_limit
├── services/            auth, profiles, history, library, importer
└── api/                 app, deps, errors, schemas, v1/

client/
├── src/                 apiclient, sessioncontroller, appcontroller,
│                        playbackcontroller, the models, the access manager
├── qml/                 Theme, App, Main, Shell, screens/, ui/
├── tests/               one executable per suite, plus tests/e2e/
└── lint.sh              runs qmllint; wired into the build as a POST_BUILD step
```

**The rules, and they are load-bearing:**

- `api/` contains no SQL. `db/repositories.py` contains no business rules.
  `services/` never imports `fastapi`.
- If a change needs to cross a layer, the change probably belongs elsewhere.
- The CLI and the HTTP API call the same services. They are not two
  implementations; keep it that way, or import over `curl` and over
  `streamserver import` will diverge.

## Security rules

- **Authorization is server-side.** A check the client can skip is not a check.
- **Every route takes its credentials as a typed parameter** (`caller:
  CallerDep`, `profile_id: ProfileDep`, `admin: AdminDep`). A route that forgot
  a decorator is a bug; a route that forgot a parameter is visible in the
  signature. Keep it that way.
- **A dependency that nothing takes is not a control.** `require_uncapped` was
  written, documented and exported, and wired into nothing — so an account on
  the shipped default password could rate, favourite, stream and write watch
  history. Every mutating route now takes `UncappedCallerDep`, `require_admin`
  sits on top of it, and `tests/test_client_routes.py` walks the routers and
  fails if one comes back ungated.
- **Per-profile routes take `OwnProfileDep`, not `ProfileDep`.** `ProfileDep`
  falls back to the account's *first* profile when the request names none,
  which is right for the catalogue and wrong for favourites, history, ratings
  and the home rails: two people sharing an account would see each other's
  lists.
- **A foreign resource is 404, never 403.** The response must not confirm that
  somebody else's profile exists.
- **No user input reaches SQL as syntax.** Sort columns and directions are
  whitelisted. FTS terms are quoted. `ORDER BY` values come from a fixed map.
- **No user input reaches the filesystem as a path.** Media are addressed by
  id. The path is resolved and re-validated against the library roots at the
  moment of the request, and opened `O_NOFOLLOW`.
- **No secret is logged, returned or stored in the clear.** Passwords and PINs
  are scrypt-hashed. Refresh tokens are stored as SHA-256. The audit trail
  scrubs before it writes. `/admin/users` has an explicit column list that
  excludes `password_hash`.
- **Errors do not leak.** No stack traces, no SQL, no paths outside the roots.

## Python conventions

- `from __future__ import annotations` at the top of every module.
- Type hints on public functions.
- Dataclasses over dictionaries for structured values.
- `Annotated[...]` dependency aliases are **imported by value**, never
  re-declared. Under postponed annotation evaluation a string-typed alias is
  silently not resolved by FastAPI — the dependency is not applied and FastAPI
  looks for a query parameter with the argument's name. This has already
  happened here once and cost a privilege escalation.
- No ORM. The SQL is in `repositories.py` where it can be read, indexed and
  audited for injection safety in one place.

## Comments

Comment the *why*, never the *what*. A comment that records a decision, a
failure mode, or a bug that would otherwise come back is the most valuable line
in the file. Several exist here because a test found the bug the comment
describes. A comment that restates the code is noise.

## Testing

- Every fix ships with the test that found it. No exceptions.
- A test names a *behaviour*, not a function: `test_a_non_positive_id_is_rejected_by_validation`,
  not `test_get_media`.
- Assert the specific thing. "Does not raise" is not an assertion of correctness.
- Hostile input is normal input: filenames, sidecars, ffprobe output and request
  bodies are all tested as if hostile, because all of them are.
- No test may depend on wall-clock timing, the network, or a real media file —
  with one deliberate exception: `client/tests/tst_e2e.cpp` starts the real
  server over loopback on a real generated clip. It is not a unit test and does
  not pretend to be; see [docs/TESTING.md](docs/TESTING.md).
- `pyproject.toml` sets `-p no:xvfb`; that is not optional.
- **Client: assert on the bytes.** A suite that drives the client against a
  loopback server and reads what it *sent* is the only kind that can see a
  request which is wrong. `client/tests/tst_apiclient.cpp` and
  `client/tests/tst_e2e.cpp` between them found nine defects the deciding-suites
  could not: four requests the server rejected with 422, a profile header that
  was never sent, two routes whose id is not where the client read it from, an
  empty stream URL, and an unenforced password gate.
- Every suite is `QTEST_GUILESS_MAIN`. `QTEST_MAIN` aborts on a headless
  machine, which reports as "Subprocess aborted" rather than as a failure.

Run both suites before committing.

## Documentation

**The documentation describes the actual implementation. Do not write
documentation for features that do not exist.** When architecture changes,
update the document in the same commit.

If you find a docstring that disagrees with the code, one of them is wrong:
check which, and fix it. That has happened more than once here.

## Adding an API route

1. Add the model to `api/schemas.py`. Request models derive from `StrictModel`
   (`extra="forbid"`, strings trimmed and capped) — unknown fields are
   rejected, not dropped.
2. Add the route to the right module in `api/v1/`. Use the path-parameter
   aliases (`MediaPathId`, `ProfilePathId`, `AdminId`) so an id of 0 or -1 is a
   422 rather than a pointless lookup.
3. Take `caller: CallerDep` as a parameter, `profile_id: OwnProfileDep` where
   the answer is per profile and `ProfileDep` where it is not, and
   `caller: UncappedCallerDep` on anything that mutates. Return 404 for a
   resource owned by somebody else.
4. Put the SQL in `repositories.py`, the rule in `services/`, the shape in
   `schemas.py`.
5. Audit it if it is a security-relevant action, using a name from
   `SecurityEventTypes`.
6. Add tests: happy path, unauthenticated, wrong owner, malformed input.
7. Update [docs/API.md](docs/API.md).

## Adding a migration

Append to `MIGRATIONS` in `db/migrations.py` with the next version. **Never
edit a released migration.** Write statements so a partially applied one can be
replayed by hand (`IF NOT EXISTS`, `OR IGNORE`). Destructive changes get their
own migration. Update [docs/DATABASE.md](docs/DATABASE.md).

## Git

- One logical change per commit, and the message says what was wrong and why —
  not just what changed.
- Never commit `server.toml` with real credentials, a `signing.key`, a database,
  a backup, or a log. Example configuration contains placeholders only.
- Before declaring anything finished: `git status`, `git diff`, and a check that
  a clean checkout builds and tests green.

## The brief

The project's requirements are in the conversation that started this
repository. The short version: production application, not a prototype; Qt 6 /
QML client, Python server; SQLite with proper indexes and migrations; profiles
with real PINs; no client-side authorization; no external DRM; no cloud
services; a UI that must not look generated; screenshot-driven iteration
against supplied references; real end-to-end testing, not just unit tests.

The client's UI is built and tested; what remains is packaging for Windows and
Android, and the screenshot comparison against the supplied references.
[docs/ROADMAP.md](docs/ROADMAP.md) sequences the rest.
