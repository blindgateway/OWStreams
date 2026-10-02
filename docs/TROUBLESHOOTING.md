# Troubleshooting

Start here:

```bash
streamserver doctor -c /etc/streamserver/server.toml
```

It checks the config, the database, the signing key, the media roots and
`ffprobe`, and reports each as OK or PROBLEM.

---

## The server will not start

**`no such table: users` / `no such column ...`**

The database is behind the code. Run the migration:

```bash
streamserver init-db
```

The server also migrates at startup; running it explicitly means you see the
error before the service comes back.

**`database is locked`**

Two writers, or a stale lock. Check for a second instance:

```bash
sudo systemctl status streamserver
ps aux | grep streamserver
```

A running import holds the write lock for its duration. If the lock is stale
because the process was killed, SQLite clears it automatically; if it persists,
the database file or its directory is probably not writable by the service user:

```bash
sudo -u streamserver ls -la /var/lib/streamserver/data
```

**`permission denied` on the data directory**

The service user must own the data directory. The media directory only needs
to be readable.

**`Address already in use`**

```bash
sudo ss -tlnp | grep 8484
```

Change `port` in the config, or stop whatever is holding it.

**`ffprobe: not found` (warning, not fatal)**

The server runs without it; media imports then carry no technical metadata and
the player cannot offer audio or subtitle track selection. Install ffmpeg, or
point `media.ffprobe_path` at it.

---

## The client cannot connect

**`Connection refused`**

The server is not listening on that address. It binds `127.0.0.1` by default,
which is unreachable from another machine. For LAN access set
`server.host = "0.0.0.0"` (or the specific interface address).

**`Connection timed out`**

Usually a firewall. Check what the server is actually listening on:

```bash
sudo ss -tlnp | grep 8484
```

**`No route to host` / DNS failures**

The name does not resolve, or resolves to the wrong address. Test from the
client machine:

```bash
curl -v http://<host>:8484/api/health
```

**`Invalid Host header`**

`server.allowed_hosts` does not include the name being used. Add it, or clear
the list to accept any Host (which is only sensible on a trusted network).

**Works on the server, not from the phone**

Almost always `host = 127.0.0.1`. Change it, and confirm the two are on the
same network or that a tunnel is up.

**Works at home, not away**

Do not expose a port to the internet. Use Tailscale — see
[DEPLOYMENT.md](DEPLOYMENT.md#7-remote-access).

---

## Accounts and login

**`423 Locked` / "too many attempts"**

The account is locked after 8 failed logins for 5 minutes. Wait, or clear it
server-side:

```bash
sqlite3 /var/lib/streamserver/data/streamserver.db \
  "DELETE FROM login_attempts WHERE username='phuree';"
```

**Forgot the administrator password**

```bash
streamserver set-password phuree
```

This flags `must_change_password` and revokes that account's sessions.

**`must_change_password` never clears**

The client is caching the flag from login rather than re-reading it after
`POST /auth/password`. Re-authenticate; the response to the password change
does not carry a token.

**Sessions keep disappearing**

`logout-all` or a password change revokes everything except the current
session — that is the intended behaviour after a suspected compromise. Check
the audit log:

```bash
sqlite3 /var/lib/streamserver/data/streamserver.db \
  "SELECT at, action, outcome, ip_address FROM audit_log ORDER BY at DESC LIMIT 20;"
```

**The client says "session expired" immediately**

Clock skew. Access tokens are validated against their `exp` claim; a client
more than a minute ahead of the server will see every token as expired.

---

## Profiles and PINs

**`404` on my own profile**

`X-Profile-Id` names a profile belonging to another account, or a deleted one.
`GET /api/v1/profiles` lists what this token can see. A 404 rather than a 403
is deliberate: the response must not confirm that another account's profile
exists.

**PIN rejected even though correct**

PINs are 4–8 digits and are not case- or whitespace-normalised; a stray space
from a paste is the usual cause. `pin_strength` scores weak PINs but does not
refuse them.

**Forgot a profile PIN**

PINs are scrypt-hashed and cannot be recovered. Clear it server-side:

```bash
sqlite3 /var/lib/streamserver/data/streamserver.db \
  "UPDATE profiles SET pin_hash=NULL, pin_salt=NULL WHERE id=<profile_id>;"
```

---

## The library

**A scan finds nothing**

```bash
streamserver doctor          # are the roots configured and readable?
streamserver scan --root /srv/media/movies --log-level DEBUG
```

A root that is not configured is not scanned, and neither is anything outside
it — that is the containment rule, not a bug. Check that the service user can
actually read the directory.

**Files are skipped**

The scanner skips, deliberately:

- zero-byte files (a stub or a failed copy)
- files over 200 GB
- directories named `extras`, `sample`, `trailers`, `subs`, `screenshots`,
  `proof`, `@eaDir`, and their common variants
- symlinked *directories* (a symlinked *file* inside the root is still scanned)

**A title imported with the wrong name**

```bash
streamserver scan --root /srv/media/movies --reimport-all
```

Or fix it and keep it:

```bash
streamserver import /srv/media/movies/Bad.Name.2021.mkv --title "Good Name" --year 2021
curl -X PATCH http://127.0.0.1:8484/api/v1/admin/media/42 \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"title":"Good Name","year":2021}'
```

**No cast, genres or descriptions**

The default metadata provider is `local`, which reads sidecar files only
(`Title.nfo`, `Title.json`, or a `Title/` folder). To use an external provider,
set `metadata.default_provider` and the API key. The server needs outbound
network access for that, and a personal API key is free from TMDB and OMDb.

**Search returns nothing for a title that exists**

Search is full-text over title, original title, overview, tagline, genres, tags
and cast. It does not search the *filename* or the *file path*. Reindex after
a manual import:

```bash
curl -X POST http://127.0.0.1:8484/api/v1/admin/reindex -H "Authorization: Bearer $TOKEN"
```

**Every title shows as missing**

Usually a library root that is not mounted, or a permissions change.

```bash
streamserver verify              # reports; changes nothing
streamserver scan --remove-missing
```

`verify` with no `--root` deliberately does not treat a configuration problem
as a mass deletion, which is why a scan without roots is refused rather than
silently emptying the library.

---

## Playback

**`410 Gone`**

The file is recorded missing. `verify` will confirm; if the file is there, the
row is stale:

```bash
streamserver scan --root /srv/media/movies
```

**`416 Requested Range Not Satisfiable`**

The offset is past the end of the file — usually a stale resume position after
the file was replaced by a shorter one. Clear the position for that profile:

```bash
curl -X DELETE http://127.0.0.1:8484/api/v1/library/history/42 \
  -H "Authorization: Bearer $TOKEN" -H 'X-Profile-Id: 1'
```

**`400` on a `Range` header**

The header does not parse. Supported: `bytes=a-b`, `bytes=a-`, `bytes=-n`. No
multi-range requests.

**Playback stutters or the buffer empties**

The media is on the slow HDD and the network is the other end of it. Check:

- read throughput of the source device
- whether the client is seeking far ahead (each seek is a new request)
- `server.stream_chunk_size` — 8 MiB is a reasonable default; a 4 GB RAM box
  does not want it much larger

**A file will not play on the device**

An unsupported codec or container. The server does not transcode, by design
([ROADMAP.md](ROADMAP.md)); the client should say the file is not playable
rather than failing silently. The title's streams are in `media_streams`:

```bash
sqlite3 /var/lib/streamserver/data/streamserver.db \
  "SELECT kind, codec, language, title FROM media_streams WHERE media_id=42;"
```

**Subtitles or audio track selection is empty**

Those come from `media_streams`, which is populated by ffprobe. If ffprobe was
missing during the import, re-import:

```bash
streamserver scan --root /srv/media/movies --reimport-all
```

---

## Performance

**The UI is slow with a large library**

Check the query plans behind the hot endpoints:

```bash
sqlite3 /var/lib/streamserver/data/streamserver.db "EXPLAIN QUERY PLAN
  SELECT * FROM media WHERE hidden=0 ORDER BY sort_title LIMIT 20;"
```

It should use `idx_media_sort_title` and not scan. If it does, run
`Database.optimize()` (`ANALYZE`) — reachable via the admin maintenance route.

**Memory grows over time**

```bash
systemctl show streamserver -p MemoryCurrent
curl -X POST http://127.0.0.1:8484/api/v1/admin/maintenance/vacuum \
  -H "Authorization: Bearer $TOKEN"
```

The page cache is capped per connection; a sustained climb is a leak worth
investigating rather than vacuuming away.

**The log fills the disk**

`RequestContextMiddleware` writes one line per *session*, not per Range request
— a 40 GB film is ~5000 requests and logging each would bury everything else. If
the log is still large, a client is hammering `/library/search`; the search
bucket allows 120/minute.

If the stream summary line never appears at all, the folding is not running.
It tests the request path for a `stream` *segment*; a change that compares the
path with `startswith("/api/v1/stream/")` is always false for `/api/v1/stream/7`
and every chunk gets its own line. `test_api_playback.py` asserts a stream
produces the summary, so this cannot come back without a test failing.

---

## The client

**"The server did not return a playable source" on a title the server has**

`playbackStarted` carried an empty URL when the client's media id did not match
the one it was streaming. The id is stamped onto the client *before* the URL is
built from it; if the two are reordered the URL comes out empty and the player
reports a playback failure for a title the server has just authorised.

**Favourites or ratings do not persist, and the UI updates anyway**

Every request body is a `StrictModel` with `extra="forbid"`. An id sent in the
body where the route takes it as a path parameter is a 422, and the client's
`handle()` emits `favoriteChanged` for the *intent* rather than for the
server's answer — so the tile fills in and nothing is stored. Watch the server
log for `422`, or assert on the bytes with `client/tests/tst_apiclient.cpp`.

**Everything looks like the wrong profile's data**

The client sends `X-Profile-Id` on every profile-scoped route, including `0`
before a profile is chosen. If the header is missing the server falls back to
the account's *first* profile and answers 200 — the one case where silence is
worse than an error. Per-profile routes answer 400 `profile_required` when no
profile is named, so a dropped header shows up immediately.

**The client aborts at startup with nothing on stderr**

That is what a missing platform plugin looks like: `QGuiApplication`'s
constructor calls `qFatal`, and on some builds the message never reaches a
terminal. `gdb -batch -ex run -ex "bt 12" ./build/stream-client` ends at
`init_platform`, which is Qt failing to find the plugin and nothing else.

Check the two things that decide it:

```bash
ls /usr/lib64/qt6/plugins/platforms/    # what exists
QT_QPA_PLATFORM=xcb ./stream-client     # name one explicitly
```

`QT_QPA_PLATFORM` has to name a plugin that is *deployed*, and on Fedora the
`xcb` plugin additionally needs `libxcb-cursor0` — Qt says so in a message
that is easy to miss among the rest. A tree assembled by Qt's deploy step
carries the plugins Qt selected for the *host*, which is `xcb` on a desktop
and is not what a headless build machine needs; the same tree then aborts
with `QT_QPA_PLATFORM=offscreen`, which is a different plugin entirely. See
[ROADMAP.md](ROADMAP.md#not-started) for the state of packaging.

**The QML loads but every object is `undefined`**

The build tree's `Stream` module is not on the library search path. Running the
binary from a shell that happens to have it works; running it under `ctest`
does not, because `add_test` puts the test's build directory on the path and not
the one below it. The symptom is `TypeError: Cannot call method 'value' of
undefined`, several layers from the cause. The e2e target sets `BUILD_RPATH`
for exactly this.

---

## Database and backups

**Restoring a backup**

```bash
systemctl stop streamserver
streamserver restore /var/lib/streamserver/data/backups/streamserver-2026-10-01.db.gz --force
systemctl start streamserver
```

Without `--force` it refuses if the target exists. It keeps the previous file
as `*.pre-restore.bak`, checks integrity before swapping, and removes stale
`-wal`/`-shm` sidecars.

**`integrity check failed` on a backup**

That backup is unusable; the message names the problems. Restore a different
one and run `PRAGMA integrity_check` on the live database.

**The database is large**

```bash
curl -X POST .../api/v1/admin/maintenance/prune   -H "Authorization: Bearer $TOKEN"
curl -X POST .../api/v1/admin/maintenance/vacuum  -H "Authorization: Bearer $TOKEN"
```

`prune` removes expired tokens, sessions, login attempts, finished playback
sessions and audit rows past `storage.audit_retention_days`.

---

## Getting more detail

```bash
streamserver serve --log-level DEBUG -c /etc/streamserver/server.toml
journalctl -u streamserver -f
```

Every response carries an `X-Request-Id` header, and every error body carries
the same value as `request_id`. That id appears in the server log, which is how
to correlate a client complaint with a stack trace:

```bash
grep '9f2c1ab34de55f10' /var/lib/streamserver/data/logs/*.log
```

The audit trail answers "what happened" without needing the logs:

```bash
sqlite3 /var/lib/streamserver/data/streamserver.db \
  "SELECT at, action, outcome, username, ip_address, detail FROM audit_log
   ORDER BY at DESC LIMIT 50;"
```
