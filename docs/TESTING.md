# Testing

```bash
cd backend
python -m pytest          # 497 tests, about 60 seconds
```

Every test runs offline, needs no root, and touches no network. The suite is
the reason the server can be trusted to run unattended; this document says
what is covered, how, and — just as importantly — what is not.

## Philosophy

**Every bug found by a test is fixed and the test stays.** Each of the fixes
below was found by a test that is still in the suite. A test that documents a
failure you have not fixed yet is a failing test, and failing tests do not get
committed.

**Tests are named after behaviour, not after functions.** `test_a_non_positive_id_is_rejected_by_validation`
says what the server does. `test_parse_filename_2` says nothing.

**Hostile input is normal input.** Filenames, sidecars, ffprobe output and HTTP
bodies all come from outside, and all of them are tested as if hostile.

**No test may depend on wall-clock timing, the network, or a real media file.**
ffprobe is invoked only against files the test itself creates.

## Layout

```
tests/
├── conftest.py                  fixtures: config, database, app, client, users
├── test_config.py               configuration layering and validation
├── test_database.py             migrations, transactions, backup/restore, integrity
├── test_security_passwords.py   scrypt, PINs, strength, constant-time compare
├── test_security_paths.py       containment, traversal, slugify, symlinks
├── test_media_units.py          filename parsing, scanning, ffprobe parsing, providers
├── test_api_auth.py             login, refresh, sessions, passwords, setup
├── test_api_profiles.py         CRUD, ordering, PIN lifecycle, ownership
├── test_api_library.py          browse, filter, sort, search, favourites, history
├── test_api_playback.py         tokens, ranges, streaming, progress, 404/410
├── test_api_admin.py            every admin route, and that a non-admin reaches none
└── test_cli.py                  every subcommand, including failure paths
```

## Fixtures

| Fixture | What it gives |
|---|---|
| `data_dir` | A private directory per test |
| `library_root` | A private media root |
| `config` | A `Config` with `scrypt_n=1024` (still a real KDF, ~1 ms) and one library root |
| `db` | A migrated `Database` |
| `app` / `client` | A `TestClient` over a fully wired app with its own database and signing key |
| `perform_setup` / `setup` | The first-run administrator flow |
| `owner` / `admin_profile` | A signed-in administrator and its default profile |
| `second_user` / `second_session` | A *different* account, for IDOR tests |
| `library` | Three imported titles |
| `autouse_scratch` | Keeps scratch files inside `$TMPDIR` |

Nothing is module-global, so each test gets an independent application
instance — which is what makes the tests order-independent and parallelisable.

The admin credentials in `conftest.py` are `phuree` / `phuree-2024-secure`. That
is a test fixture, not the shipped default; see
[SECURITY.md](SECURITY.md#the-default-administrator).

## What is covered

### Configuration
Layer precedence (defaults → file → environment → overrides), validation
rejects bad values, `dump_config()` round-trips and produces a `0600` file, path
derivation from `data_dir`.

### Database
Migration from empty and from a previous version; a migration is atomic (a
failure leaves the previous version applied); the statement splitter handles
semicolons inside comments, strings and bracket identifiers; `write()`
rolls back on exception; `busy_timeout`; `foreign_keys` actually enforced;
WAL really is enabled; backup produces a restorable file; restore refuses
without `force`, keeps a `.pre-restore.bak`, integrity-checks before swapping,
and deletes stale sidecars; `verify_integrity` on a non-database file returns
`(False, …)` rather than raising; `VACUUM` and `wal_checkpoint` work outside a
transaction.

### Passwords and PINs
scrypt round-trip; wrong password rejected; a malformed stored hash is rejected
without raising; the KDF runs even for a malformed hash (timing uniformity);
`needs_rehash`; constant-time comparison; password policy (length, and the weak
ones that are rejected by name); PIN hashing and verification; PIN strength
scoring; `random_password` excludes ambiguous characters.

### Paths
`safe_join` under a base with `..`, absolute paths, and NUL bytes; traversal
detection; `slugify` on hostile input; `MediaRootRegistry` refuses a path
outside its roots, a symlink escaping a root, and a non-existent path where one
is required; `relativize` and `root_for` are consistent.

### Media
Twenty-three real release names parse to the right title, year, season and
episode — including `Dune.Part.Two.2024.2160p.UHD.BluRay.x265-GROUP.mkv`,
`[SubsPlease] Frieren - Beyond Journey's End - 12 (1080p) [A1B2C3D4].mkv`,
`Blade.Runner.2049.2017.2160p.WEB-DL.ddp5.1.Atmos.DV.HDR.H.265-EVO.mkv`,
`1917.2019.1080p.BluRay.x264-SPARKS.mkv`, `3.10.to.Yuma.1957...`, and
`the-thing-1982.mkv`. Quality, audio codec, release type, edition, HDR, bit
depth, release group; multi-episode ranges in all three spellings, and a bare
episode number that must *not* be read as a range; hostile filenames (empty,
`..`, NUL bytes, 5000 characters, emoji, Japanese) parse without crashing and
cannot produce a title containing a separator; a full path is reduced to its
basename; the scanner finds media, skips samples, extras and zero-byte files,
skips symlinked directories and follows symlinked files inside the root;
`scan_tree` is deterministic; sidecar discovery for both layouts; ffprobe
rational parsing (`30000/1001`, `N/A`, `nan`, `inf`, `1/0`, garbage); a
truncated container reports a failure instead of raising; an empty file and a
missing file are reported distinctly; HDR is derived from the colour transfer
without over-claiming Dolby Vision; metadata providers read JSON and Kodi NFO,
survive malformed input, refuse to smuggle a path through a title or an artwork
field, and cap an oversized sidecar; provider priority and merge semantics.

### API — auth
Login success and failure; the lockout after repeated failures; refresh
rotation and reuse detection; logout and logout-all; `GET /auth/me`;
`GET /auth/setup` before and after setup; `POST /auth/setup` succeeds once and
409s thereafter; concurrent setup cannot create two administrators; password
change rejects a wrong current password, rejects reuse, enforces the policy and
revokes other sessions; session listing and per-device revocation; a revoked
token is rejected immediately; an expired token is rejected; a token signed with
another key is rejected; a playback token is rejected on a session route.

### API — profiles
Create, list, get, update, delete; duplicate name is 409; reorder; **every
route returns 404 for another account's profile**; PIN set, change, clear,
verify; wrong PIN audits and returns `attempts_left`; `GET /locked`.

### API — library
Browse with every filter and sort combination; pagination and `has_more`;
unknown id, unknown slug and a non-positive id; search relevance and
type-ahead; `GET /home`; categories and stats; favourites add/remove/list and
idempotency; history record, resume, remove, clear, continue-watching;
completion threshold behaviour; ratings set/get/remove and the 0.5–5.0 bound;
every profile-scoped route returns another profile's data as 404, never as
someone else's data.

### API — playback
Token minting; the token is bound to one title and one profile; **a playback
token cannot reach the library, the admin API, or another title** (this was a
real privilege escalation — see below); `Range: bytes=a-b`, `bytes=a-`,
`bytes=-n`, unsatisfiable → 416 with `Content-Range: bytes */size`, unparseable
→ 400; a span over `max_range_span` is refused; a hidden title is 404 and a
missing file is 410; the path is re-validated at request time; progress
reporting and completion; `finish`.

### API — admin
Every route, happy and unhappy. **A non-admin receives 403 from every route in
the admin inventory** — the test walks the route list rather than one
hand-picked endpoint, so a new route cannot slip in. Cannot delete yourself;
cannot delete the last administrator; cannot demote or deactivate yourself;
deactivating revokes sessions; `password_hash` never appears in a response;
`delete_file=true` is always refused; a path outside the roots is refused;
`/admin/settings` reports whether a key is configured, never the key.

### CLI
Every subcommand, including: `init-db` is idempotent; `init-config` refuses to
clobber and writes `0600`; `doctor` passes on a healthy install and reports a
missing root; `add-user` enforces the password policy and rejects a duplicate;
`set-password` revokes sessions and flags a forced change; `scan` is idempotent;
`scan` without roots fails clearly; `import` refuses a path outside the roots
and a missing file; `verify` exits non-zero on a missing file; `verify` with no
roots does **not** mass-mark the library missing; `backup` and `restore` round
trip; `restore` refuses without `--force`.

## Bugs this suite found

Every one of these was a real defect in code that looked correct:

- **Playback-token privilege escalation.** The stream route shared a
  dependency alias with every other route, so FastAPI's dependency cache handed
  the session-authenticated caller to a route that believed it had verified a
  playback token. A token minted to stream one title could read the entire
  library and reach the admin API.
- **Silently unwired dependencies.** Under `from __future__ import annotations`,
  `Annotated[...]` aliases written as string annotations are never resolved by
  FastAPI. The dependency was not applied and FastAPI looked for a query
  parameter named after the argument.
- **`update_user()` bound the wrong number of parameters**, breaking *every*
  password change and every account deactivation.
- **WAL was never enabled.** `migrate()` began a transaction on a brand-new
  file, so the later `PRAGMA journal_mode=WAL` was a silent no-op and readers
  blocked the writer.
- **`restore()` orphaned the caller's handle**, so the process kept reading the
  pre-restore inode.
- **Migrations were split on `;`**, truncating any statement with a semicolon
  in a comment or a string.
- **Contentless FTS5 refused `DELETE`** without `contentless_delete=1`, so the
  delete path was broken.
- **`sqlite3.Row` reached Pydantic** on four routes, giving a 500.
- **`/admin/users` returned `password_hash`.**
- **Adding a library root wrote to a config attribute that does not exist**, so
  the new root was silently ignored.
- **`AuditLogger.record()` did not accept `media_id`**, so every playback
  request 500'd.
- **Release markers were stripped before the technical scan**, so
  `Movie.2020.2160p.WEB-DL.mkv` reported release "web", `Movie.DTS-HD.MA.5.1`
  reported no audio codec, and `Dune.2160p.DV.HDR` reported no HDR at all.
- **A multi-episode range was read from the wrong capture group**, and the
  dash-spelled form made `S01E05.1080p` claim episodes 5 to 1080.
- **`find_sidecars` used `Path.with_suffix`**, which replaces only the last
  suffix — it looked for `Movie.2020.nfo` but a `Movie.2020/` folder.
- **The scanner yielded zero-byte files**, creating library rows that can never
  stream.
- **`_hdr_format` reported HDR10+ for any bt2020 file.**
- **The admin router's docstring claimed a router-level administrator check
  that did not exist** — each handler had to remember it.
- **A duplicated block of helper definitions in `naming.py`** was shadowed by a
  later identical copy, so a fix applied to the first had no effect.

## What is not covered

Stated plainly, because a test suite that pretends is worse than none.

- **The client.** There is no client yet, so there are no QML tests, no
  screenshot comparison, no end-to-end user walkthrough. This is the largest
  gap and it is the reason the project is not finished.
- **Live external metadata providers.** `TmdbProvider`, `ImdbProvider` and
  `OmdbProvider` are implemented and unit-tested for their parsing and
  availability logic, but have never been run against a live API — the
  development environment has no network egress. Their error handling for a
  timeout, a 429 or a changed response shape is written but unexercised.
- **Windows and Android builds.** The server is platform-independent Python;
  the client is unbuilt. Nothing has been compiled for a second platform.
- **Load.** Correctness at 1000 titles is tested; throughput at 10 concurrent
  streams is not. The design work for it is done (indexed queries, bounded
  page cache, 8 MiB chunks, per-session logging) but it is untested.
- **ffprobe against genuinely broken media.** The tests use synthetic
  truncated files, which exercises the failure path; they do not include a
  corpus of real damaged containers.
- **TLS termination, reverse proxy configuration, Tailscale.** The server binds
  plain HTTP and expects a proxy in front when exposed; the proxy
  configuration in [DEPLOYMENT.md](DEPLOYMENT.md) is a recommendation, not
  something tested here.

## Writing a test

```python
def test_a_bare_episode_number_is_not_a_range():
    """Every one of these has a number after the episode marker.  Reading it as
    the end of a range would tell the importer to look for episode 1080."""
    parsed = parse_filename("Show.S01E05.1080p.mkv")
    assert parsed.episode == 5
    assert parsed.episode_end is None
```

State the behaviour, show the consequence of getting it wrong, and assert the
specific thing — not "does not raise". If the assertion needs a comment to
explain why it matters, that comment belongs in the test.
