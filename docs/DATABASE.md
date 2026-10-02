# Database

SQLite, in WAL mode, one file, on the SSD. This document describes what is
actually created by `backend/streamserver/db/migrations.py` and
`backend/streamserver/db/database.py`.

## Why SQLite

The server is one box with four cores, 4 GB of RAM and about ten concurrent
users. SQLite in WAL mode handles that comfortably:

- Readers never block the writer. A browse request during a 1000-title import
  is a normal read, not a queued write.
- One process, no daemon, no network credentials, no second thing to supervise
  at 3am.
- A backup is one file.
- A 4 GB RAM budget is not a problem: every connection caps its page cache at
  2 MB and the library lives on disk, not in memory.

PostgreSQL would only earn its cost if more than one *writer host* were ever
needed. That is not this deployment.

## Migrations

Three, applied in order by version. `migrate()` reads `PRAGMA user_version`,
skips anything already applied, and runs each remaining migration **inside its
own transaction together with its `schema_version` row**. A crash mid-migration
therefore leaves the database on the previous version, not half-applied.

| Version | Name | What it does |
|---|---|---|
| 1 | `initial_core` | The whole initial schema: accounts, profiles, sessions, token revocation, login attempts, the library, the FTS5 index, per-profile state, audit and scan bookkeeping |
| 2 | `external_ids_and_availability` | External provider ids in their own table, per-media stream inventory, `users.playback_preference`, `media.availability` |
| 3 | `recommendations_and_playback_sessions` | Computed per-profile recommendations, in-flight playback sessions with byte/request counters |

A migration is never edited after release. Statements are written so that a
partially applied one can be replayed by hand (`IF NOT EXISTS`, `OR IGNORE`),
and destructive changes (`DROP`) get their own migration.

Bookkeeping is doubled on purpose: the `schema_version` table holds a readable
history with names and timestamps, while `PRAGMA user_version` is the fast path
that `migrate()` reads. Both are written in the same transaction.

### Splitting SQL

`split_statements()` in `database.py` is a character-level scanner, not
`sql.split(";")`. A migration legitimately contains a semicolon inside a `--`
comment, a `'string'`, a `"identifier"`, a `` `identifier` ``, a `[identifier]`
or a `/* block comment */`, and a naive split truncates the statement at the
wrong place — which fails on the *first* migration with a syntax error pointing
at a comment. The scanner tracks all of those, and treats a doubled closing
quote as an escaped quote.

## Tables

24 tables. `PRAGMA foreign_key_check` is part of `verify`; every foreign key
cascades.

### Accounts and identity

**`users`** — accounts, credentials, per-account playback preferences.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER | PK |
| `username` | TEXT | NOT NULL, UNIQUE, `COLLATE NOCASE` |
| `email` | TEXT | UNIQUE, nullable |
| `display_name` | TEXT | NOT NULL DEFAULT '' |
| `password_hash` | TEXT | NOT NULL — scrypt, self-describing encoding |
| `password_changed_at` | TEXT | |
| `must_change_password` | INTEGER | NOT NULL DEFAULT 0 |
| `is_admin` | INTEGER | NOT NULL DEFAULT 0 |
| `is_active` | INTEGER | NOT NULL DEFAULT 1 |
| `autoplay` | TEXT | NOT NULL DEFAULT 'all' |
| `preferred_audio_lang`, `preferred_subtitle_lang` | TEXT | |
| `playback_preference` | TEXT | NOT NULL DEFAULT 'server' (migration 2) |
| `created_at`, `updated_at`, `last_login_at` | TEXT | |

Index: `idx_users_active(is_active)`.

**`profiles`** — per-user viewing profiles. A kids profile, a PIN, a watch
limit.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER | PK |
| `user_id` | INTEGER | FK → users, CASCADE |
| `name` | TEXT | NOT NULL; UNIQUE(user_id, name) |
| `avatar_id` | TEXT | |
| `pin_hash` | TEXT | scrypt hash only — never a plaintext PIN |
| `pin_salt` | TEXT | |
| `pin_updated_at` | TEXT | |
| `is_kids` | INTEGER | NOT NULL DEFAULT 0 |
| `max_watch_minutes` | INTEGER | NULL = unlimited |
| `sort_order` | INTEGER | NOT NULL DEFAULT 0 |
| `created_at` | TEXT | |

Index: `idx_profiles_user(user_id, sort_order)`.

**`sessions`** — one row per refresh-token session, i.e. per signed-in device.

Columns: `id` (TEXT PK), `user_id` (FK CASCADE), `refresh_token_hash` (NOT
NULL — the token itself is never stored), `device_name`, `client_version`,
`ip_address`, `user_agent`, `created_at`, `last_used_at`, `expires_at`,
`revoked_at`, `revoked_reason`.

Indexes: `idx_sessions_user(user_id, revoked_at)`, `idx_sessions_expires(expires_at)`.

**`revoked_tokens`** — the access-token denylist, so logout takes effect before
the 15-minute access token would have expired on its own.

Columns: `jti` (TEXT PK), `expires_at`, `reason`, `revoked_at`.
Index: `idx_revoked_expiry(expires_at)`. Purged by `maintenance/prune`.

**`login_attempts`** — brute-force throttling for both username and source
address. Columns: `id`, `username`, `ip_address`, `successful`, `reason`,
`created_at`. Indexes on `(username, created_at)` and `(ip_address, created_at)`.

### Library

**`media`** — the central table. Everything the detail screen shows lives here,
except the artwork, which is referenced by key.

*Description:* `id`, `slug` (UNIQUE), `title`, `original_title`, `sort_title`,
`overview`, `tagline`, `year`, `release_date`, `runtime_minutes`,
`content_type` (CHECK movie/series/episode), `parental_rating`, `rating_value`,
`rating_source`, `rating_vote_count`.

*File:* `media_path` (UNIQUE, the absolute path on the HDD), `media_root`,
`file_size_bytes`, `file_modified_at`, `file_inode`, `integrity_state`
(CHECK unknown/ok/missing/corrupt), `availability`.

*Technical (from ffprobe):* `container`, `video_codec`, `video_profile`, `width`,
`height`, `aspect_ratio`, `frame_rate`, `bitrate_bps`, `hdr_format`,
`color_space`, `audio_codec`, `audio_channels`, `audio_languages`,
`subtitle_languages`.

*Artwork:* `poster_key`, `backdrop_key`, `logo_key` — keys into `images/`, not
blobs.

*Structure:* `parent_media_id` (FK self, CASCADE) for series → episodes.

*Provenance:* `source` (CHECK scan/import/api/manual), `external_ids` (JSON),
`extra` (JSON), `searchable_text`.

*State:* `added_at`, `updated_at`, `last_played_at`, `play_count`, `hidden`.

Indexes: `sort_title`, `added_at DESC`, `year`, `(content_type, hidden)`,
`rating_value DESC`, `parent_media_id`, `last_played_at DESC`. Every one exists
because a browse screen sorts or filters on that column; there are no
speculative indexes.

**`genres`** (`id`, `name` UNIQUE NOCASE, `slug` UNIQUE) and **`media_genres`**
(PK `(media_id, genre_id)`, FK CASCADE). Index `idx_media_genres_genre(genre_id,
media_id)` so "everything in this genre" is an index scan, not a table scan.

**`tags`** and **`media_tags`** — the same shape, for free-form tags.

**`people`** — cast and crew. `id`, `name` (NOCASE), `slug` (UNIQUE), `role`
(CHECK cast/director/writer), `tmdb_id`, `imdb_id`, `photo_key`, `birth_date`,
`place`. Indexes `idx_people_name(name)` and a UNIQUE `idx_people_name_role(name,
role)` — the same person can be a director on one title and cast on another, so
uniqueness is on the pair.

**`media_credits`** — PK `(media_id, person_id)`, plus `character`, `job`,
`ord`. Index `idx_credits_person(person_id)` for "everything this person is in".

**`collections`** / **`collection_media`** — manual collections with an
explicit `position`.

**`media_fts`** — the search index. A contentless FTS5 virtual table:

```sql
CREATE VIRTUAL TABLE media_fts USING fts5(
    title, original_title, overview, tagline,
    genres, tags, cast, directors,
    content='',
    contentless_delete=1,
    tokenize='unicode61 remove_diacritics 2'
);
```

`content=''` because the `media` table already stores every one of those
columns — a second copy on the SSD would only cost space.
`contentless_delete=1` is what allows a `DELETE` against a contentless index
(SQLite refuses without it, which is how the delete path was found to be broken).
`remove_diacritics 2` means a search for "Amelie" finds "Amélie".

There are **no triggers**. The index is written by
`repositories._index_media` and removed by `_delete_fts_row`, in the same
transaction as the row they describe, so the index cannot drift from the table.

### Per-profile state

All three are keyed `(profile_id, media_id)` with cascading deletes, and all
three are indexed for "this profile, newest first".

**`favorites`** — PK `(profile_id, media_id)`, `created_at`. Index
`(profile_id, created_at DESC)`.

**`watch_history`** — the resume data. `position_seconds`, `duration_seconds`,
`percent`, `completed`, `first_watched_at`, `last_watched_at`, `play_count`,
`finished_at`. Indexes `(profile_id, last_watched_at DESC)` and
`(profile_id, completed, last_watched_at DESC)` — the second one is what makes
Continue Watching a single index scan rather than "fetch everything, filter in
Python".

**`ratings`** — `score` REAL CHECK 0.5–5.0 (half stars), `created_at`,
`updated_at`. Index `(profile_id, score DESC)`.

**`recommendations`** (migration 3) — PK `(profile_id, media_id)`, `score`,
`reason`, `computed_at`. Index `(profile_id, score DESC)`.

**`playback_sessions`** (migration 3) — one row per in-flight playback:
`id` (TEXT PK), `profile_id`, `media_id`, `user_id`, `token_hash`, `started_at`,
`last_seen_at`, `expires_at`, `bytes_served`, `requests`, `start_offset`,
`ended`, `client_ip`. Indexes `(profile_id, ended)` and `(expires_at)`. This is
what lets a client resume after a network blip without re-authorising, and
gives the server a place to record what was actually watched.

### Streams and external ids

**`media_streams`** (migration 2) — PK `(media_id, stream_id)`, `kind`
(CHECK video/audio/subtitle), `index_no`, `language`, `title`, `codec`,
`channels`, `channel_layout`, `is_default`, `is_forced`, `external_id`. Index
`(media_id, kind, index_no)`. This is the audio/subtitle track picker on the
detail screen, stored per title rather than re-probed per request.

**`media_external_ids`** (migration 2) — PK `(media_id, provider)`, `value`.
Index `(provider, value)`. Extracted out of the `external_ids` JSON column so
matching a title against several providers is an index lookup rather than JSON
parsing in application code.

### Operations

**`audit_log`** — `id`, `at`, `user_id`, `username`, `profile_id`, `action`,
`outcome` (CHECK ok/denied/error), `ip_address`, `device_name`, `detail` (JSON),
`request_id`. Deliberately has **no foreign keys** on `user_id`/`profile_id`, so
the trail survives the account being deleted. Deliberately coarse: it never
stores tokens, passwords or file contents. Indexes on `(at DESC)`, `(user_id,
at DESC)`, `(action, at DESC)`.

**`scan_jobs`** — `id`, `root`, `status` (CHECK queued/running/done/failed/
cancelled), `started_at`, `finished_at`, `added`, `updated`, `skipped`,
`removed`, `error`.

**`settings`** — `key` PK, `value`, `updated_at`.

## Connection handling

`Database` keeps a `threading.local()` so each thread gets its own connection,
a `threading.RLock` for writes, and a list of every connection it has handed out
so `close()` can close them all.

PRAGMAs, on every connection:

| PRAGMA | Value | Why |
|---|---|---|
| `foreign_keys` | ON | Off by default in SQLite |
| `busy_timeout` | `timeout × 1000` (15 s) | a concurrent importer must not become "database is locked" |
| `journal_mode` | WAL (not read-only) | readers during writes |
| `synchronous` | NORMAL (not read-only) | durable across process crash; only at risk from OS-level power loss — the right trade for a personal media server |
| `temp_store` | MEMORY | |
| `cache_size` | −2048 | 2 MB per connection: ten concurrent users cost tens of MiB, not gigabytes |
| `mmap_size` | 64 MiB | |

`migrate()` sets `journal_mode = WAL` *before the first write*, explicitly. On a
brand-new file there is no journal to switch; letting SQLite pick its default
(DELETE) and changing it later is a silent no-op, so the server would run
without WAL and every reader would block the writer. This was a real bug, found
by `tests/test_database.py`.

Connections use `isolation_level=None` — explicit transactions only.

```python
with db.write() as conn:      # BEGIN IMMEDIATE … COMMIT / ROLLBACK
    ...
with db.read() as conn:       # no BEGIN; WAL readers never block writers
    ...
```

`BEGIN IMMEDIATE` rather than the default `DEFERRED`: two concurrent writers
then fail fast into `busy_timeout` instead of deadlocking at `COMMIT` with an
unretryable `SQLITE_BUSY`.

`vacuum()` and `wal_checkpoint()` take the write lock directly, outside
`write()` — both require the transaction to be closed.

## Search

`searchable_text` is a denormalised blob of title, original title, overview,
genres, tags, cast and directors, mirrored into `media_fts`. Free-text search
goes through `bm25()` with per-column weights (title and original title 8.0,
overview 2.0, tagline 4.0, genres and tags 3.0, cast and directors 1.5).

User input never becomes FTS syntax. `_fts_query()` strips the characters the
tokenizer would choke on, then double-quotes every term and appends a prefix
wildcard, so `title:" OR 1=1 --` is inert text and `"dune par"` becomes
`"dune"* AND "par"*`.

## Backups, restore, integrity

**Backup** uses SQLite's own online backup API rather than copying the file. A
`cp` of a live WAL database is not a backup: the WAL may hold committed pages
the main file does not. `backup()` writes to a staging file, optionally gzips
it, `chmod 0600`, and returns the path.

**Restore** is deliberately paranoid:

1. Refuse if the backup is missing, or if the destination exists without
   `force`.
2. Close the caller's open handle first. An open descriptor keeps the *old*
   inode alive, so the process would go on reading and writing a file no longer
   reachable by any path.
3. Decompress to `*.restoring`, then `PRAGMA integrity_check` the staging file
   and refuse to proceed if it fails.
4. Keep the previous file as `*.pre-restore.bak`, then `os.replace` — atomic.
5. Delete the `-wal` and `-shm` sidecars. Leaving them would let SQLite graft
   stale pages onto the restored database.
6. `chmod 0600`.

**Integrity check** opens the file read-only and reports
`(ok, detail)`. A file whose header is not a database raises from `connect`
rather than returning `False`; that is caught so every entry point reports the
same "not ok". `PRAGMA integrity_check(20)` caps the output at 20 problems
rather than dumping a 1000-line report for a corrupt file.

## Housekeeping

`POST /api/v1/admin/maintenance/prune` removes expired revoked tokens, expired
sessions, old login attempts, finished playback sessions and audit records past
`storage.audit_retention_days` (default 180).

`POST /api/v1/admin/maintenance/vacuum` runs `wal_checkpoint(TRUNCATE)` then
`VACUUM`, which returns the WAL to the main file and shrinks it. Worth running
nightly on a small box; not needed often, since SQLite reuses freed pages.

`Database.optimize()` runs `ANALYZE` then `PRAGMA optimize`. Cheap, and it
keeps the query planner honest as the library grows.

## Migrations still to be written

The schema is designed to extend without a rewrite, and these are the changes
anticipated by the current column set:

- **Series/season metadata.** `media.content_type` and `parent_media_id` exist;
  a `media_seasons` table (series, season number, title, artwork) would slot in
  without touching anything else.
- **User ratings vs. provider ratings.** `ratings.score` is the user's;
  `media.rating_value` / `rating_source` is the provider's. Keeping them
  separate is deliberate — a user must not be able to overwrite the shared
  rating, and the provider rating is cached, not owned.
- **Image variants.** `poster_key` is a single key. Thumbnails at several sizes
  would become a `media_images(media_id, kind, variant, key, width, height)`
  table; the key columns already hold the slug namespace.
- **Watch limits as a schedule rather than a daily total.** `max_watch_minutes`
  is the simple version. A `profile_watch_limits` table with a reset time would
  be the upgrade.

Each of these is a new migration. Do not edit migration 3.
