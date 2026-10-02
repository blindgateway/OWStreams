# API

Base URL: `http://<host>:<port>`. Everything versioned lives under
`/api/v1`. A machine-readable OpenAPI 3.1 document is served at
`/api/openapi.json` and an interactive browser at `/api/docs`; this file is the
prose contract, and it is the authority on behaviour the schema cannot express.

`GET /api/health` is deliberately outside the version prefix — a liveness probe
that changes shape when the API version changes is not a liveness probe.

## Conventions

### Authentication

All routes except `/api/health`, `/api/v1/auth/login`,
`/api/v1/auth/refresh`, `GET /api/v1/auth/setup`, `POST /api/v1/auth/setup` and
`GET /api/v1/auth/server` require a bearer token:

```
Authorization: Bearer ssat.<payload>.<signature>
```

Access tokens are HMAC-SHA256 signed and expire after 15 minutes by default
(`auth.access_token_ttl_seconds`). They are stateless — no database lookup on
the hot path — so a logout or a password change takes effect through the
revocation tables rather than by making existing tokens invalid on their own.

Refresh tokens are opaque (`ssrt.<random>`), stored as a SHA-256 hash, and
rotate on every use: `POST /auth/refresh` returns a new pair and revokes the
old one. `remember: true` at login extends the refresh token from 30 days to
one year.

`POST /api/v1/auth/password/reset` and everything under `/api/v1/admin`
additionally require `is_admin`.

### Profile scoping

A session token names an *account*. Per-profile routes need a *profile*, and
they get it from the `X-Profile-Id` header or from a playback token, which
carries one. Ownership failures return **404, not 403** — the response must not
confirm that another account's profile exists.

The routes divide into two kinds, and the difference is a 400:

| Kind | Routes | Header absent |
|---|---|---|
| Account-scoped | the catalogue, one title, search, suggest, categories, a poster | the account's first profile is used |
| Per-profile | `/library/favorites`, `/library/history`, `/library/continue-watching`, `/library/home`, ratings, playback | **400 `profile_required`** |

A per-profile route that fell back to the account's first profile would answer
one household member with another's favourites, and the client's own header
would be ignored while it still appeared to be doing the right thing. A
playback token is exempt because it names its profile. `X-Profile-Id: 0` — what
the client sends before a profile has been chosen — is a 404
`profile_not_found`, not a silent fallback.

`tests/test_client_routes.py` asserts the split by walking the routers, so a
new per-profile route cannot quietly take the lenient dependency.

### Pagination

List endpoints take `limit` and `offset`. `MediaPage` returns
`{items, total, offset, limit, has_more}`. Limits are capped server-side
(browse 200, search 200, people 500) so a client cannot ask for the whole
library in one response.

### Errors

Every failure, without exception, is:

```json
{
  "error": "not_found",
  "message": "That title is not in the library.",
  "detail": null,
  "request_id": "9f2c1ab34de55f10"
}
```

`error` is a stable machine code; `message` is for a human; `detail` is
structured and route-specific (`attempts_left` for a wrong PIN, a list of
`{field, reason}` for a validation failure). `request_id` is also returned in
the `X-Request-Id` header and appears in the server log.

| Status | `error` | When |
|---|---|---|
| 400 | `bad_request` | malformed request the schema allowed; `X-Profile-Id` that is not an integer |
| 400 | `profile_required` | a per-profile route was called with no `X-Profile-Id` and a session token |
| 401 | `unauthenticated` | missing, expired, malformed or revoked token |
| 403 | `forbidden` | valid token, wrong privilege — including a playback token presented to a non-stream route, and any mutation by an account still on the shipped default password (`X-Requires-Password-Change: true`) |
| 404 | `not_found` | absent, hidden, or owned by somebody else |
| 404 | `profile_not_found` | `X-Profile-Id` names a profile the caller does not own (including `0`) |
| 409 | `conflict` | duplicate username or profile name; setup already done |
| 413 | `payload_too_large` | upload or import body over the cap |
| 422 | `validation_error` | failed schema validation |
| 423 | `locked` | account locked out after repeated failures |
| 429 | `rate_limited` | bucket exhausted; `Retry-After` is set |
| 500 | `database_error` / `internal_error` | logged server-side, generic to the client |
| 503 | `unavailable` | a dependency the request needs is down |

**Nothing internal is ever returned.** No stack traces, no SQL, no filesystem
paths outside the library roots, no file contents. A 500 carries a request id
and a generic message; the traceback is in the log.

Validation errors return at most 20 problems and never echo the offending
input — for this API the input can be a password.

### Rate limits

Token buckets, in-process, keyed by account (or by address for login).

| Bucket | Default | Applied to |
|---|---|---|
| `login` | 10/min | login attempts |
| `api` | 600/min | every authenticated route |
| `search` | 120/min | `/library/search`, `/library/suggest` |
| `stream` | 120/min | `/stream/{id}` |
| `import` | 30/min | `/admin/import` |

Repeated login failures additionally lock the *account* for `lockout_seconds`
(300) after `max_failed_logins` (8).

## Versioning

The version is a path prefix, `/api/v1`, set once in `API_VERSION`.

- **Additive changes do not bump the version.** New routes, new optional
  request fields, new response fields, new enum values in a field the client
  already tolerates. Clients must ignore unknown fields in a response and
  unknown fields in a response-shaped object; request models set
  `extra="forbid"`, so a *new server* rejecting an old client's extra field is
  the expected failure mode and is reported as a 422.
- **Breaking changes bump the version.** Removing or renaming a route or field,
  narrowing a type, changing a status code, changing the meaning of an existing
  field. `/api/v2` is mounted alongside `/api/v1`; both are served until clients
  have moved.
- **A version is not removed** while a deployed client may still use it.

Version numbering is major-only. There is no `/api/v1.3`.

---

## Health

### `GET /api/health` — no auth

Liveness and readiness. `status`, `database`, `version`, `api_version`. 503 when
the database does not answer.

### `GET /` — no auth

A banner pointing at `/api/docs` and `/api/health`. Not in the OpenAPI
document.

---

## Authentication

Prefix `/api/v1/auth`, tag `authentication`.

### `POST /api/v1/auth/login` — no auth

Body: `LoginRequest` — `username`, `password`, optional `device_name`,
`client_version`, `remember`.

Returns `TokenResponse`: `access_token`, `token_type` (`Bearer`), `expires_in`,
`refresh_token`, `refresh_expires_at`, `session_id`, `must_change_password`,
`user`.

401 on bad credentials, 423 when the account is locked, 429 when the rate limit
or the failure count is exhausted. Failure and success are both audited.

### `POST /api/v1/auth/refresh` — no auth

Body: `RefreshRequest` — `refresh_token`. Rotates the pair. A reused refresh
token is rejected and the whole session family is revoked.

### `POST /api/v1/auth/logout` — session

Revokes this access token's `jti` and the session. `OkResponse`.

### `POST /api/v1/auth/logout-all` — session

Revokes every session for the account. `OkResponse`.

### `GET /api/v1/auth/me` — session

`{user, profiles[], server}`. The client calls this after login to populate the
profile picker and the server banner.

### `POST /api/v1/auth/password` — session

Body: `PasswordChangeRequest` — `current_password`, `new_password`,
`logout_other_sessions`. Rejects reusing the current password, enforces the
password policy, revokes all *other* sessions, audits
`account.password.changed`. `OkResponse`.

### `GET /api/v1/auth/sessions` — session

`SessionOut[]` — every device, active and revoked, with `last_used_at` and
`ip_address`.

### `DELETE /api/v1/auth/sessions/{session_id}` — session

Revoke one device. 404 if it is not this account's session. Cannot revoke the
current session this way — use logout.

### `GET /api/v1/auth/setup` — no auth

`ServerInfo` including whether first-run setup is still required. A client uses
this to decide between the setup screen and the login screen.

### `POST /api/v1/auth/setup` — no auth

Body: `SetupRequest` — `username`, `password`, optional `display_name`,
`profile_name`, `library_roots`.

Creates the first administrator and its default profile **in one transaction
that also re-checks that no user exists**, so two concurrent setup requests
cannot both win. 409 once any account exists. Returns `TokenResponse` (201) —
the caller is signed in immediately.

**This endpoint does not create the `phuree` / `phuree` account.** See
[SECURITY.md](SECURITY.md#the-default-administrator) for how that credential is
supplied and why it is not a code default.

### `GET /api/v1/auth/server` — no auth

`ServerInfo` — version, API version, library count, feature flags. Safe to show
on the connection screen before anyone has signed in.

### `POST /api/v1/auth/password/reset` — admin

Query `user_id`, body `PasswordResetRequest`. Administrative reset of another
account. Forces `must_change_password` and revokes that account's sessions. An
alias of `POST /api/v1/admin/users/{user_id}/password`.

---

## Profiles

Prefix `/api/v1/profiles`, tag `profiles`. All require a session; ownership is
re-checked per call and a foreign id is 404.

| Route | Auth | Body / query | Returns |
|---|---|---|---|
| `GET /profiles` | session | — | `ProfileOut[]` |
| `POST /profiles` | session | `ProfileCreate` | `ProfileOut` (201) |
| `GET /profiles/{profile_id}` | session | — | `ProfileOut` |
| `PATCH /profiles/{profile_id}` | session | `ProfileUpdate` | `ProfileOut` |
| `DELETE /profiles/{profile_id}` | session | — | `OkResponse` |
| `POST /profiles/reorder` | session | `ProfileReorder` | `ProfileOut[]` |
| `POST /profiles/{profile_id}/pin` | session | `ProfilePinSet` | `ProfileOut` |
| `PUT /profiles/{profile_id}/pin` | session | `ProfilePinChange` | `ProfileOut` |
| `POST /profiles/{profile_id}/unlock` | session | `ProfilePinVerify` | `OkResponse` |
| `GET /profiles/{profile_id}/locked` | session | — | `{profile_id, requires_pin}` |
| `POST /profiles/select` | session | `ProfileSelection` | `OkResponse` |

`ProfileCreate` accepts an optional `pin` matching `^\d{4,8}$`. The PIN is
hashed with scrypt; the plaintext is never stored, logged or returned. A weak
PIN is accepted at the storage layer and scored by `pin_strength` so the client
can warn — a 4-digit PIN on a kids profile is a speed bump, not a security
boundary, and the API does not pretend otherwise.

`POST /profiles/{id}/unlock` takes an `X-Pin-Attempt` header. A wrong PIN
audits `profile.pin.failed` and returns 401 with `detail.attempts_left`, so the
client can show a countdown instead of hammering the endpoint.

`POST /profiles/select` audits `profile.selected` and does **not** reissue a
token — the profile id in the `X-Profile-Id` header is what selects it.

---

## Library

Prefix `/api/v1/library`, tag `library`. Routes marked *profile* take
`X-Profile-Id`.

### Browsing

| Route | Auth | Notes |
|---|---|---|
| `GET /library/media` | session+profile | `limit`, `offset`, `sort`, `direction`, `content_type`, `genre`, `tag`, `year`, `year_from`, `year_to`, `decade`, `min_rating`, `max_rating`, `min_runtime`, `max_runtime`, `resolution`, `hdr`, `parental_rating`, `person_id`, `collection_id`, `include_missing` → `MediaPage` |
| `GET /library/media/{media_id}` | session+profile | `MediaOut` — full metadata |
| `GET /library/media/by-slug/{slug}` | session+profile | same, addressed by stable slug |
| `GET /library/home` | session+profile | every rail the home screen needs, in one request: continue watching, recently added, recommended, favourites, popular, recently watched. `limit` (1–50, default 20) |
| `GET /library/categories` | session | genres, years, tags and counts |
| `GET /library/stats` | session | library totals |
| `GET /library/collections/{id}` | session | one collection |
| `GET /library/people` | session | `search`, `limit` |
| `GET /library/people/{person_id}` | session | one person's credits |

`sort` is a whitelist: `title`, `year`, `rating`, `added`, or relevance when a
search term is present. `direction` is `asc` or `desc`. Anything else is a 422
— the value is never interpolated into SQL.

Hidden titles never appear. `include_missing=true` adds titles whose files are
recorded as missing, for the operator's benefit.

### Search

`GET /library/search` — session+profile. `q` (≤200), `limit`, `offset`, `sort`,
plus the same filters as browse. Full-text over title, original title, overview,
tagline, genres, tags, cast and directors, ranked with `bm25()`. Own rate-limit
bucket.

`GET /library/suggest` — session+profile. `q` required (1–100), `limit`
(1–25, default 10). Type-ahead. Never raises on a partial or nonsense query.

The FTS query builder quotes and escapes every term, so user text is never
interpreted as FTS syntax.

### Favourites — profile

| Route | Notes |
|---|---|
| `GET /library/favorites` | `sort`, `direction`, `limit` (≤200, default 50), `offset` → `MediaPage` |
| `PUT /library/favorites/{media_id}` | idempotent add |
| `DELETE /library/favorites/{media_id}` | idempotent remove |

### History — profile

| Route | Notes |
|---|---|
| `GET /library/history` | `limit` (≤200, default 50), `offset`, `completed` → `MediaPage` |
| `GET /library/continue-watching` | progress but not finished, `limit` (≤100, default 20) → `MediaOut[]` |
| `GET /library/history/{media_id}` | `ProgressOut`; a zeroed object when there is no entry, so the client does not need a 404 branch |
| `PUT /library/history/{media_id}` | body `ProgressUpdate` → `ProgressOut` |
| `DELETE /library/history/{media_id}` | remove one entry; 404 if absent |
| `DELETE /library/history` | clear this profile's history |

`ProgressUpdate` is `position_seconds` (≥0), optional `duration_seconds`,
optional `completed`. The server computes `percent` and flips `completed` when
progress passes `media.completion_threshold` (0.97), so "finished" is one
definition everywhere.

### Ratings — profile

| Route | Notes |
|---|---|
| `GET /library/media/{media_id}/rating` | this profile's rating; `score: 0.0` when unrated |
| `PUT /library/media/{media_id}/rating` | `RatingUpdate` — `score` 0.5–5.0 in half stars |
| `DELETE /library/media/{media_id}/rating` | remove |

These are the *user's* ratings and are separate from `media.rating_value`,
which is the cached provider rating (IMDb or whichever provider is configured)
with `rating_source` naming it. A user rating never overwrites the shared one.

---

## Playback and artwork

Prefixes `/api/v1/media` and `/api/v1/stream`, tag `playback`.

### `POST /api/v1/media/{media_id}/playback` — session+profile

Query `start_offset` (≥0, default 0.0). Returns `PlaybackTokenOut`:
`playback_token`, `expires_in` (600), `stream_url`, `session_id`, `start_offset`.

The token authorises **one media item for one profile** for ten minutes, and
opens a playback session valid six hours. It resumes from stored progress unless
`start_offset` says otherwise. 404 if the title is hidden or absent; 410 if the
file is recorded missing.

### `GET /api/v1/stream/{media_id}` — media token or session

The bytes. Honours `Range` per RFC 9110 §14:

| Request | Response |
|---|---|
| no `Range` | 200, `Content-Length`, `Accept-Ranges: bytes` |
| `bytes=a-b` | 206, `Content-Range: bytes a-b/size` |
| `bytes=a-` | 206 from `a` to the end |
| `bytes=-n` | 206, the last `n` bytes |
| unsatisfiable | 416 with `Content-Range: bytes */size` |
| unparseable, or multi-range (`bytes=0-9, 20-29`) | 416 with `Content-Range: bytes */size` |

Also: 403 when a playback token names a different title or profile, 404 when
hidden, 410 when missing. A range span over `media.max_range_span` (512 MiB) is
refused — a client asking for half a film in one request is either broken or
hostile.

The path is re-resolved against the library roots **on every request**, and the
file is opened `O_NOFOLLOW`.

This route uses its own dependency aliases, not the ones the rest of the API
uses. Sharing an alias would let FastAPI's dependency cache hand the
session-authenticated caller to a route that believes it accepted a playback
token — a real privilege escalation, now covered by a test.

### Progress

`GET /api/v1/media/{media_id}/progress` and `PUT` of the same are the same
operation as `/library/history/{media_id}`. Both exist because a client
grouping by "media" and a client grouping by "history" should both read
naturally; they call the same service.

### `POST /api/v1/media/{media_id}/playback/finish` — session+profile

Query `position_seconds`, optional `session_id`. Ends the session, marks the
title watched, updates the library's `last_played_at` and `play_count`.

### Artwork

`GET /api/v1/media/{media_id}/poster` and `/backdrop` — session. Served from the
server-side image directory, re-checked against that directory, `Cache-Control`
set, 404 with a short cache lifetime when absent. Images are never served from
the media roots, so a `.jpg` next to a film is not automatically web-accessible.

---

## Administration

Prefix `/api/v1/admin`, tag `administration`. **Every route requires an
administrator account** — enforced as a dependency on the router itself, so a
route added later is protected unless somebody deliberately removes it. Every
action is audited.

### Accounts

| Route | Notes |
|---|---|
| `GET /admin/users` | includes inactive accounts. Never returns `password_hash` — the column list is explicit for exactly this reason |
| `POST /admin/users` | `AdminUserCreate`; creates a "Main" profile too. 409 on duplicate |
| `PATCH /admin/users/{user_id}` | activate, deactivate or promote. Only `is_active` and `is_admin` are honoured; 400 if neither is supplied. Refuses self-demotion and self-deactivation (409). Deactivating revokes the account's sessions |
| `DELETE /admin/users/{user_id}` | 409 for your own account, or the last remaining administrator |
| `POST /admin/users/{user_id}/password` | reset; forces a change and revokes sessions |

### Library administration

| Route | Notes |
|---|---|
| `GET /admin/media` | all titles including hidden and missing. `limit` (≤200), `offset` |
| `PATCH /admin/media/{media_id}` | `MediaPatch` — only supplied, non-null fields are written; 400 if none |
| `DELETE /admin/media/{media_id}` | removes the row, **leaves the file**. `delete_file=true` is always refused with 400: the server never deletes media |
| `GET /admin/library-roots` | configured roots with existence, readability and entry counts |
| `POST /admin/library-roots` | validate and add a root. In-memory until written to the config file |
| `POST /admin/import` | `ImportRequest` — `paths` (≤1000 absolute server paths) and/or `scan`, `remove_missing`, `reimport_all`, `refresh_metadata`. Paths outside the configured roots are refused. 400 if neither paths nor scan |
| `POST /admin/verify` | re-check every file against the filesystem. `deep` re-probes (slow) |
| `POST /admin/reindex` | rebuild the FTS index |

### Operations

| Route | Notes |
|---|---|
| `GET /admin/health` | `HealthOut` — uptime, library counts, root status, whether ffprobe is available |
| `GET /admin/audit` | `limit` (≤500), `action` filter |
| `GET /admin/settings` | displayable settings. Reports only *whether* an API key is configured, never the key |
| `POST /admin/maintenance/prune` | expired tokens, sessions, login attempts, playback sessions, old audit rows |
| `POST /admin/backup` | gzipped snapshot into the backup directory |
| `POST /admin/maintenance/vacuum` | WAL checkpoint + `VACUUM` |

## Working with the API

```bash
BASE=http://127.0.0.1:8484

# First run: create the administrator.
curl -sX POST $BASE/api/v1/auth/setup \
  -H 'Content-Type: application/json' \
  -d '{"username":"phuree","password":"<a real password>"}'

TOKEN=$(curl -sX POST $BASE/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"phuree","password":"<a real password>"}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')

# Browse, scoped to a profile.
curl -s "$BASE/api/v1/library/home?limit=20" \
  -H "Authorization: Bearer $TOKEN" -H 'X-Profile-Id: 1'

# Search.
curl -sG "$BASE/api/v1/library/search" --data-urlencode 'q=dune' \
  -H "Authorization: Bearer $TOKEN" -H 'X-Profile-Id: 1'

# Play: mint a token, then stream a byte range.
PT=$(curl -sX POST "$BASE/api/v1/media/1/playback" \
  -H "Authorization: Bearer $TOKEN" -H 'X-Profile-Id: 1' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["playback_token"])')

curl -s -D- -o /dev/null "$BASE/api/v1/stream/1" \
  -H "Authorization: Bearer $PT" -H 'Range: bytes=0-1048575'
```

## Writing a client

- Store the refresh token in the platform's secure storage — the Android
  Keystore, the Windows credential manager, an OS keyring. Not in a plain
  preferences file.
- Never persist the access token; it is valid for 15 minutes and the refresh
  flow replaces it.
- Treat `must_change_password: true` in any token response as "show the
  change-password screen now".
- Handle 401 by refreshing once and replaying; on a second 401, sign out.
- Handle 429 by honouring `Retry-After` rather than retrying immediately.
- Do not write to the database. There is an API for everything, and the database
  is on the server's disk, not yours.
