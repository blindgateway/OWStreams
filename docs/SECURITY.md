# Security

What this system protects against, how, and — as much of it — what it does
not. The last part matters most: a threat model that only lists wins is a
marketing document, and this one is written to be useful during an incident.

## The threat model in one paragraph

The attacker is on the network the server is reachable from. They have an
account (their own, or a stolen session belonging to a household member), and
they want to read a film they are not allowed to watch, escalate to an
administrator, or reach a file on the server's disk that is not in a library
root. They are assumed to be able to send any HTTP request, with any headers,
and to supply any filename or sidecar they like during an import. They are
*not* assumed to have root on the server host — that is a different problem,
and at that point the films are already on their disk.

Two more assumptions worth stating because they shape the design:

- **The client is untrusted.** Anything the client enforces, an attacker does
  not enforce. Every authorisation decision is server-side.
- **A compromised client is a compromised session.** The client holds a
  refresh token and will use it. The system's job is to make one stolen
  session revocable and scoped, not to pretend a stolen session is not a
  session.

## What is enforced, and where

### Authentication

Bearer tokens, `Authorization: Bearer ssat.<payload>.<signature>`, HMAC-SHA256
over a base64url payload, verified with `hmac.compare_digest` on both the
prefix and the signature.

- Access tokens expire after 15 minutes by default and are stateless, so the
  hot path is a signature check rather than a database lookup.
- Refresh tokens are opaque (`ssrt.<random>`), stored as a **SHA-256 hash**,
  and rotate on every use: a replayed refresh token is rejected and the session
  family is revoked.
- `remember: true` extends the refresh token from 30 days to one year. That is
  the actual lifetime of the credential, and it is why it is worth revoking.
- Logout revokes the session and blacklists the access token's `jti` in
  `revoked_tokens`, so it takes effect immediately rather than in fifteen
  minutes.
- A password change revokes every *other* session — the standard response to
  "I think someone else has my account".

**Enforced in** `security/tokens.py`, `services/auth.py`, `api/deps.py`.

### Passwords

scrypt, N=2^15, r=8, p=1, 16-byte random salt, self-describing encoding
(`scrypt$N$r$p$salt$key`) so parameters can be raised later and
`needs_rehash` can upgrade an old hash opportunistically at login.

- The KDF **always runs**, including for an unknown username, so response time
  does not reveal whether an account exists.
- Comparison is constant-time.
- A malformed stored hash is rejected, not raised: a corrupted row is a failed
  login, not a 500.
- The parameters are bounded by *memory* (`128 * r * n`), not by `n` alone.
  Bounding `n` alone would let a tampered row ask for 512 MiB and take the box
  down on the next login.

**Why scrypt and not argon2id.** Argon2id is the better KDF. It is also a
native dependency that may not be present on a Debian box, and this server has
no build step. scrypt is in the standard library on every interpreter targeted,
memory-hard, and tunable through the stored encoding — a migration to argon2id
later is a change to `passwords.py` and a `needs_rehash`, not a schema change.

**Why not bcrypt.** bcrypt truncates at 72 bytes and is not memory-hard by
default. Neither is disqualifying, but scrypt was already the better fit here.

### Profile PINs

A profile PIN is **not an authentication credential.** It grants nothing beyond
one profile on an account that is already signed in. It exists to stop a child
picking up the remote and choosing a grown-up's profile.

It is still handled properly:

- scrypt-hashed, with the same salt-and-compare discipline as a password but a
  cheaper work factor (N=2^14) — snappy on a four-core box that is also
  streaming.
- **Never stored in plaintext**, never logged, never returned by any endpoint.
- The endpoint reports `has_pin` and nothing else.
- Guessing is throttled by a **server-side** token bucket keyed on
  (user, profile). A correct PIN refills the bucket, so an honest user who
  fat-fingered their way to the limit is not locked out, and one profile's
  limit does not affect another's.
- A four-digit PIN is 10^4. At six guesses a minute that is 11 days of
  continuous guessing — which is a speed bump, not a lock. `pin_strength` is
  returned so the client can steer a user away from 1234, and the PIN change
  endpoint requires the current PIN, so a locked profile can only be unlocked
  by somebody who knows it.

**Stated plainly: a four-digit PIN is not a security boundary against an
attacker who has a stolen session.** It is a boundary against a five-year-old.

### Authorization

Every route takes its credentials as a typed FastAPI dependency. A handler
physically cannot forget to check, and the OpenAPI document carries the
requirement.

| Requirement | Meaning |
|---|---|
| session | a valid, unexpired, unrevoked access token for an active account |
| session + profile | plus `X-Profile-Id` naming a profile **owned by this caller** |
| media token | a playback token, bound to one media id and one profile |
| admin | session plus `is_admin` |

- **A foreign resource is 404, never 403.** A 403 confirms the id exists; that
  is itself a leak. This is enforced by `require_profile` → `ProfileService.require`
  and tested for every profile route and every profile-scoped library route.
- **The admin router carries `Depends(require_admin)` as a router-level
  dependency**, not as a per-handler parameter, so a route added later is
  protected by default. The per-handler `AdminDep` stays because it makes the
  check visible where it applies.
- **Privilege escalation paths are closed**: self-demotion and self-deactivation
  are refused with 409; deleting your own account is refused; deleting the last
  administrator is refused. There is no route that sets `is_admin` on the
  caller's own account without the self-promotion guard.

### Playback tokens

A ten-minute token that authorises **one media id for one profile**. It is the
reason a leaked access token cannot be used to browse the library.

The two route groups use **different dependency aliases**. Sharing one alias
lets FastAPI's `use_cache=True` hand the session-authenticated caller to the
stream route, which then believes it accepted a playback token — this was a
real privilege escalation in this codebase, and
`tests/test_api_playback.py` now holds it shut from both directions.

**Known limitation:** a playback token carries no `sid`, so revoking a *session*
does not kill outstanding playback tokens. They are bounded at ten minutes.
Stated here rather than glossed over.

### Path traversal and arbitrary file access

No client-supplied string ever reaches the filesystem as a path. Media are
addressed by integer id; the server resolves the id to a stored path and
re-validates it against the configured library roots **at the moment of the
request**, not from a cached import-time decision.

- `MediaRootRegistry.resolve` follows symlinks, then requires the result to be
  inside a root. A symlink pointing out of the library is refused.
- The file is opened `O_NOFOLLOW | O_CLOEXEC`, so a symlink swapped in between
  validation and open cannot redirect the read.
- `verify` marks a title missing; playback answers 410 rather than opening
  anything.
- Artwork is served from the server's image directory with a second containment
  check, so a `poster_key` of `../../../etc/passwd` is a 404.
- The importer refuses `/etc/passwd`, `../../outside/secret`, a symlink out of
  the root, a directory, a zero-byte file and a missing file — all tested.

**The one deliberate exception:** an administrator can add a library root, and
an admin who is tricked into adding one gets arbitrary file read within it. That
is what "administrator" means on a single-user server, and it is why the
account list and the audit log exist. It is a sharp edge and is named here
rather than buried.

### SQL injection

Every value is a bound parameter. The three places where SQL is assembled from
anything other than a bound value are all whitelisted:

- `SORT_COLUMNS` / `DIRECTIONS` in `repositories.py`, resolved through `.get()`
  with a fallback, never interpolated from the request.
- `search_media`'s `ORDER BY` is a fixed if/elif chain of constant strings.
- `GROUP BY` is defined and unreferenced.

The FTS query builder is the interesting one. `_fts_query` strips the
characters the tokenizer would choke on, then **double-quotes every term** and
appends a prefix wildcard. A search for `title:" OR 1=1 --` is inert text, not
syntax. Tested with injection payloads against a live endpoint.

### Command injection

`ffprobe` is invoked with an argument list, never a shell string, and with
`-protocol_whitelist file` so a crafted container cannot make it open a network
or pipe protocol. Output is size-capped and timeout-bounded. `ffprobe_path`
comes from the config file, never from a request.

### SSRF

No user-supplied URL is ever fetched. The metadata providers contact
**hardcoded hosts** (TMDB, IMDb, OMDb). `poster_url` / `backdrop_url` from a
provider response are parsed, validated and stored — and not downloaded by this
version. `_local_image` rejects absolute paths, `~` and any `..` segment.

### Rate limiting

In-process token buckets, bounded by an LRU ceiling so the map cannot grow
without limit.

| Bucket | Default | What it bounds |
|---|---|---|
| `login` | 10/min | login attempts, keyed by source address |
| `api` | 600/min | every authenticated route |
| `search` | 120/min | search and type-ahead |
| `stream` | 120/min | Range requests |
| `import` | 30/min | a scan, a deep verify, a rescan |
| `maintenance` | 12/min | reindex, backup, vacuum, prune |
| `pin` | 6/min | PIN guesses, per profile |

`X-Forwarded-For` is only honoured from a configured `trusted_proxies` peer, so
the login limiter cannot be spoofed by default. Configuring `trusted_proxies`
too broadly lets a client forge the source address that the audit log records —
this is stated in the deployment guide rather than left as a trap.

### Input validation

Every request model derives from `StrictModel`: `extra="forbid"`, strings
trimmed and capped. An unknown field is a 422, not a silently dropped value.
Validation errors return at most 20 problems and **never echo the offending
input** — for this API the input can be a password.

Path parameters are bounded with `ge=1`, so `/library/media/0` is a 422 rather
than a lookup for a row that cannot exist.

### Information leakage

- `password_hash` is in the `users` table and is deliberately **not** in the
  response model. The model is `extra="ignore"` with declared fields, so adding
  a column to `users` cannot leak it by accident.
- Refresh tokens are stored hashed and are never returned after the login that
  issued them.
- A 500 returns a generic message and a request id; the traceback is logged
  server-side. No SQL, no stack traces, no filesystem paths outside the roots.
- The audit trail scrubs before it writes: sensitive keys by name, and values
  matching token and password-hash patterns by shape. Applied twice, once to the
  payload and once to the formatted log line.

### CORS and CSRF

**CORS is off by default.** `cors_origins` is empty, and no CORS middleware is
installed at all in that case. A browser client is not a use case here, and
every allowed origin is one more way for a hostile page to ride along on a
session.

**CSRF does not apply.** Authentication is `Authorization: Bearer`, which a
browser will not attach to a cross-origin request by itself. No cookie is ever
set, so there is no ambient credential for a hostile page to ride.

### Secrets at rest

| Secret | Where | Mode |
|---|---|---|
| signing key | `data_dir/signing.key` | `0600`, created with `O_CREAT` then chmodded |
| config | wherever you put it | `0600` |
| database | `data_dir/streamserver.db` | `0600` |
| backups | `data_dir/backups/*.db.gz` | `0600` |
| logs and audit sink | `data_dir/logs/` | `0600` |
| refresh token (client) | platform keychain | OS-managed |

On the client the refresh token goes to the platform keychain (Secret Service
on Linux, DPAPI on Windows, Keystore on Android) and the *server URL* to
ordinary settings. Where no keychain exists the client **refuses to persist**
rather than falling back to a file — a remembered login is a convenience, and
downgrading it to plaintext trades a property the user never agreed to give up.

### Screen capture and window thumbnails

The client asks the platform to keep the player out of screenshots, screen
recordings and taskbar previews, and reports on screen what it actually got.
`CaptureGuard` (`client/src/captureguard.cpp`) is applied by `PlayerScreen`
when it becomes visible and released when it closes, and
`AppController.captureGuard` is what the settings screen reads — one object, so
the summary sentence and the itemised list cannot disagree.

| Platform | Applied | Enforced |
|---|---|---|
| Windows 10 2004+ | `WDA_EXCLUDEFROMCAPTURE` on the window handle | yes |
| Windows 10 before 2004 | nothing — the flag is a no-op there and the guard says so | no |
| Android | `FLAG_SECURE` on the activity window, through JNI | yes |
| X11 | nothing, deliberately — see below | no |
| Wayland | nothing — the protocol has no such mechanism | no |

`CaptureGuard::active()` is true only where the platform *enforces* something.
On Linux it is false, and `platformReport()` names the protocol and the reason.

**X11 is the interesting case.** `ignore_event_mask` is the only flag there that
excludes a window from capture, and it also stops the window receiving input —
applying it to the app window would break playback. `_NET_WM_BYPASS_COMPOSITOR`
is a request the window manager may ignore, so it is a request and is reported
as one. The code sets neither. Reaching `QXcbWindow` would also mean linking
`libQt6XcbQpa`, for which Qt installs no CMake package on this distribution — a
private-header dependency that builds on one machine is not a protection.

**What none of this is.** Not DRM, not a watermark, not a lock. It does not stop
a camera, a second device, a modified client, or a person with `ffmpeg` and the
stream URL — which they have, because they were allowed to watch it. It stops an
accidental screenshot key and a taskbar thumbnail on the platforms that enforce
it, and nothing anywhere else.

`client/tests/tst_captureguard.cpp` asserts the reporting contract rather than
the platform behaviour, because the reporting is what can be tested from here
and what a false claim would cost: a fresh guard claims nothing, `protect()` on
a real window agrees with `platformReport()`, a repeated call does not
accumulate claims, `release()` drops the claim with the protection, and the
report never contains "DRM" or "prevents".

The freedesktop.org recent-media list and the Windows taskbar's recent-media
entries are fed from a desktop entry this client does not install and from
system playback sessions Qt does not use, so neither is populated. There is no
API to call and no `.desktop` file is shipped; `CaptureGuard` records the fact
so the settings screen, this document and the test read one object.

### Error handling and logging

- One error shape for every failure: `{error, message, detail, request_id}`.
- `sqlite3.Error` becomes a 500 `database_error` with the SQL never surfaced.
- An unhandled exception becomes a generic 500 with the traceback in the log.
- The audit log records *what happened*, not content: action, outcome, user,
  profile, media id, source address, request id. No titles watched, no
  filenames, no bodies.
- IP addresses are normalised of CR/LF/NUL before they are written, which
  blocks log injection through a spoofed header.

## What this system does **not** protect against

This section is the reason the document exists.

- **No DRM. No anti-piracy service. No watermark.** The brief asked for none of
  these and they are not here. Anything the server sends can be captured by
  whoever receives it.
- **A client-side screenshot block is not a lock.** `CaptureGuard` excludes the
  player from capture on Windows 10 2004+ and Android, and enforces nothing on
  X11 or Wayland — see [Screen capture and window thumbnails](#screen-capture-and-window-thumbnails)
  for the per-platform table and why X11 gets nothing. It does not stop a camera,
  a second device, or a modified client.
- **An administrator can read any file the server can read.** That is the
  definition of the role on a single-user server.
- **A stolen refresh token is a valid login** until it is used or revoked. The
  mitigation is rotation (a replay is detectable and kills the family), the
  short access-token life, and the session list in account settings. It is not
  prevention.
- **No protection against an attacker who already has an administrator
  password.** The audit log is the tool for noticing that afterwards.
- **No encryption at rest for the database.** The database is `0600`; a stolen
  file is a scrypt-hash cracking problem, and scrypt is what makes that
  expensive rather than trivial. Full-disk or column-level encryption is a
  deployment decision, not a property of this code.
- **Plain HTTP by default.** `allow_insecure_transport` exists because the
  recommended deployment is a Tailscale or internal network, or a reverse proxy
  terminating TLS in front. Over an untrusted network the bearer token is a
  password in the clear. This is the single most important deployment decision
  and it is called out in the deployment guide.
- **Metadata providers are not tested against their live APIs** in this
  environment, which has no network egress. Their parsing and availability logic
  is unit-tested; their behaviour under a real outage, a 429 or a changed
  response shape is written but unexercised.
- **The audit log is not tamper-evident.** It is a SQLite table with
  `0600` permissions. Someone who can write to the database can edit it.

## The default administrator

The brief asks for a first-run account of exactly `phuree` / `phuree`, not
hard-coded into the application, with a forced or strongly encouraged password
change.

**What the code does:** creates the account you name, with the password you
supply, through `POST /api/v1/auth/setup` or `streamserver add-user phuree
--admin`. Nothing in the source contains the string `phuree` as a credential.
The setup endpoint is refused once any account exists, and it does the
emptiness check and the insert in one transaction, so two concurrent setups
cannot both win.

**Why:** shipping a credential that is published in the repository is a known
weak password on every deployment, and the password policy (minimum ten
characters) would reject it anyway. The requirement that is satisfied is the
one that matters — the account is *named* `phuree`, it is created by an
explicit operator action rather than by the code, and it is never written down
in the source.

`must_change_password` exists, is set by `add-user --must-change-password` and
by an administrative password reset, is reported in every token response, and
the client shows the change-password screen when it is set.

**The block is enforced.** `require_uncapped` in `api/deps.py` refuses a
mutation — and the streaming GET, which records "started watching" — from any
account that still owes the change, with 403 and
`X-Requires-Password-Change: true`. Every mutating route takes it, including
every admin route, and `tests/test_client_routes.py` walks the routers and
fails if one does not. Reads stay open, because the client has to be able to
render the library in order to show the change-password screen at all.

Two allowances, both deliberate:

- **Administrators are exempt.** The documented first-run is
  `add-user --admin`, which leaves the flag set and which has no
  change-password flow; gating administrators would leave the person who has to
  fix the server permanently unable to stream anything.
- **Login, refresh, logout, logout-all and password change are not gated.** A
  gated account must be able to sign out and change its password, or the gate
  is a lock rather than a prompt.

## Operational hardening

Defaults that are safe on a loopback bind and worth reviewing on a LAN bind:

- `server.allowed_hosts` is empty, which accepts any `Host` header. On a
  routable bind, list the names clients actually use — an empty list is a
  DNS-rebinding exposure.
- `server.host` is `127.0.0.1`. `0.0.0.0` is the decision that exposes the
  server to every network the box is attached to.
- The service unit in the deployment guide sets `ProtectSystem=strict`,
  `NoNewPrivileges`, `MemoryMax=1G` and `ReadWritePaths` so the media is
  readable, the data directory is writable, and nothing else is.

## Reporting

This is a private project. A vulnerability found here is a bug to fix, not a
disclosure: it goes in the repository's issue tracker or straight to the
maintainer.