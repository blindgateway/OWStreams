# CLAUDE.md

Guidance for Claude Code working in this repository.

**Read [AGENT.md](AGENT.md) first** — it holds the full working agreement for
this codebase: layout, layering rules, security rules, testing requirements,
conventions, and how to add a route or a migration. This file is a summary and
a pointer, not a substitute.

Then read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the design and
[docs/TESTING.md](docs/TESTING.md) for what the suite covers.

## State

- `backend/` — complete, 497 tests passing, no stubs.
- `client/` — **not started.** This is the outstanding work.
- `docs/` — complete.

## Non-negotiables

1. **Run the tests before and after any change.** `cd backend && python -m pytest`.
   About a minute. If they were failing on a clean checkout, say so.
2. **Read the code, not the docstring.** Docstrings in this repository have
   been wrong before, and a test caught it.
3. **Authorization is server-side.** A client-side check is not a check.
4. **Never commit a secret.** No real credentials in a config, no `signing.key`,
   no database, no backup. Examples use placeholders.
5. **Document only what exists.** If a feature is not built, the docs say so.
   Update the docs in the same commit as the code that changes them.
6. **One logical change per commit**, with a message that says what was wrong
   and why.
7. **A fix ships with the test that found it.** All twenty-odd bugs fixed in
   this repository were found by a test, and every one of those tests is still
   in the suite.

## Gotchas that have already bitten

- **`Annotated[...]` dependency aliases must be imported by value.** Under
  `from __future__ import annotations` a string-typed alias is silently
  unwired by FastAPI; it then looks for a query parameter named after the
  argument. This caused a real privilege escalation.
- **Do not share one dependency alias across routes with different credential
  requirements.** FastAPI's `use_cache=True` hands the first-resolved caller to
  the second route. The stream route uses its own aliases for this reason.
- **`sqlite3.Row` is not Pydantic-serialisable.** Repositories return dicts.
- **`Path.with_suffix()` replaces only the last suffix** and rejects a dotted
  one — do not use it to derive sidecar names or a media directory name.
- **`VACUUM` and `wal_checkpoint` must run outside a transaction.**
- **Set `journal_mode = WAL` before the first write** on a new database, or the
  pragma is a silent no-op.
- **`-p no:xvfb` in the pytest addopts is not optional.** Without it a globally
  installed plugin blocks for 30 seconds per run.
- **There is no network egress in this environment.** External metadata
  providers cannot be tested against their live APIs; do not claim they have
  been.

## The client

Not started, and it is the bulk of the remaining work. Qt 6 Quick / QML, C++
core, CMake. Qt 6.11.2 devel packages, CMake 4.3.0 and Ninja are installed and
verified: a trivial QML application configures, compiles and links on this
machine. Sequence in [docs/ROADMAP.md](docs/ROADMAP.md).
