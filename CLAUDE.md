# CLAUDE.md

Guidance for Claude Code working in this repository.

**Read [AGENT.md](AGENT.md) first** — it holds the full working agreement for
this codebase: layout, layering rules, security rules, testing requirements,
conventions, and how to add a route or a migration. This file is a summary
and a pointer, not a substitute.

Then read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the design and
[docs/TESTING.md](docs/TESTING.md) for what the suite covers.

## State

- `backend/` — complete, 607 tests passing, no stubs.
- `client/` — builds and runs, 116 tests passing across 11 suites. Packaging and
  the screenshot comparison are outstanding; see [docs/ROADMAP.md](docs/ROADMAP.md).
- `docs/` — complete.

## Non-negotiables

1. **Run the tests before and after any change.** `cd backend && python -m pytest`
   (607 tests, about two and a half minutes) and, for the client,
   `cmake --build build-test && (cd build-test && ctest)` (116 tests, seconds).
   **And start the client** — `QT_QPA_PLATFORM=offscreen ./build/stream-client`
   for ten seconds — because a QML layout that cannot work is a runtime warning
   no linter and no unit test produces. If the tests were failing on a clean
   checkout, say so.
2. **Read the code, not the docstring.** Docstrings in this repository have
   been wrong before, and a test caught it.
3. **Authorization is server-side.** A client-side check is not a check.
4. **Never commit a secret.** No real credentials in a config, no `signing.key`,
   no database, no backup. Examples use placeholders.
5. **Document only what exists.** If a feature is not built, the docs say so.
   Update the docs in the same commit as the code that changes them.
6. **One logical change per commit**, with a message that says what was wrong
   and why.
7. **A fix ships with the test that found it.** Every bug fixed in this
   repository was found by a test, and every one of those tests is still in the
   suite.

## Gotchas that have already bitten

- **`Annotated[...]` dependency aliases must be imported by value.** Under
  `from __future__ import annotations` a string-typed alias is silently
  unwired by FastAPI; it then looks for a query parameter named after the
  argument. This caused a real privilege escalation.
- **Do not share one dependency alias across routes with different credential
  requirements.** FastAPI's `use_cache=True` hands the first-resolved caller to
  the second route. The stream route uses its own aliases for this reason.
- **An unset dependency is worse than a wrong one.** `require_uncapped` was
  written, documented, exported — and wired into nothing, so an account on the
  shipped default password could rate, favourite and stream. A test now walks
  the routers and refuses to let a mutating route come back ungated. The same
  applies to `OwnProfileDep`: a per-profile route that takes the lenient
  `ProfileDep` answers with the account's first profile, so somebody else's
  favourites.
- **Ids belong in the path, not the body.** Every request body is a
  `StrictModel` with `extra="forbid"`, so a stray `media_id` is a 422. The
  client's `handle()` reports the *intent* rather than the server's answer, so
  the failure is invisible in the UI. `tests/tst_apiclient.cpp` asserts on the
  bytes.
- **`/media/7/playback/finish` does not end in the id.** Any rule that reads the
  last path segment is wrong for the three routes that end in a verb;
  `Pending::mediaId` carries it explicitly instead.
- **Test the *route*, not the request line, for `/api/v1/stream/`.** It is the
  one GET that writes — it records "started watching" before it serves a byte.
  `_is_stream` is the single rule; three places had three different ones.
- **`sqlite3.Row` is not Pydantic-serialisable.** Repositories return dicts.
- **`Path.with_suffix()` replaces only the last suffix** and rejects a dotted
  one — do not use it to derive sidecar names or a media directory name.
- **`VACUUM` and `wal_checkpoint` must run outside a transaction.**
- **Set `journal_mode = WAL` before the first write** on a new database, or the
  pragma is a silent no-op.
- **`-p no:xvfb` in the pytest addopts is not optional.** Without it a globally
  installed plugin blocks for 30 seconds per run.
- **`QTEST_MAIN` aborts on a headless machine.** Every suite is
  `QTEST_GUILESS_MAIN`, and ctest sets `QT_QPA_PLATFORM=offscreen` anyway.
- **The e2e test target needs `BUILD_RPATH` for the build tree's `Stream`
  module.** Without it the QML plugin fails to load under ctest while the same
  binary works from a shell — as `TypeError: Cannot call method 'value' of
  undefined`, several layers from the cause.
- **`http_proxy` is set in this environment.** Loopback requests go to the
  proxy unless `no_proxy` names the address; `tests/e2e/harness.py` sets it for
  every child process.
- **There is no network egress in this environment.** External metadata
  providers cannot be tested against their live APIs; do not claim they have
  been.

## The client

Qt 6 Quick / QML, C++ core, CMake. It builds and runs on this machine (Qt
6.11.2 devel packages, CMake 4.3.0, Ninja), and `qmllint` runs on every build
and fails it on any warning — see `client/lint.sh` and `client/.qmllint.ini`
for which warnings are disabled and why.

The suite that found the most is `client/tests/tst_e2e.cpp`: fifteen tests
against the *production* server, started by `client/tests/e2e/harness.py`. No
canned responses, because a response table cannot see a request that is wrong.
Remaining work in [docs/ROADMAP.md](docs/ROADMAP.md).