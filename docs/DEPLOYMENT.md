# Deployment

Target: one Debian-stable (or Fedora) box, 4 cores, 4 GB RAM, ~10 GB free SSD
and a ~1 TB HDD holding the media. Five regular users, ten at a stretch.

The rule that follows from those numbers: **the SSD holds everything small and
hot, the HDD holds only video, and nothing runs that is not needed.**

## 1. Install the server

```bash
sudo apt install python3 python3-venv ffmpeg          # Debian
sudo dnf install python3 ffmpeg                       # Fedora
```

```bash
sudo useradd --system --create-home --home-dir /var/lib/streamserver \
             --shell /usr/sbin/nologin streamserver
sudo -u streamserver python3 -m venv /opt/streamserver
sudo -u streamserver /opt/streamserver/bin/pip install --no-cache-dir /path/to/Stream/backend
```

Installing the package (rather than copying files) puts `streamserver` on the
venv's `PATH` at `/opt/streamserver/bin/streamserver`, which is what the
`ExecStart` below expects.

## 2. Put the database on the SSD and the media on the HDD

```bash
sudo mkdir -p /var/lib/streamserver/data
sudo mkdir -p /srv/media/movies
sudo chown -R streamserver:streamserver /var/lib/streamserver
```

`/srv/media/movies` stays owned by whoever manages the media — the server only
needs read access, and giving it write access to the media directory is not
required for anything it does.

If the SSD is small, keep the data directory and the media on separate
filesystems and point `storage.data_dir` at the SSD explicitly. Do not put
`streamserver.db` on the HDD: every browse touches it, and the 1000-title
library is index-heavy.

## 3. Configure

```bash
sudo -u streamserver /opt/streamserver/bin/streamserver \
  init-config -c /etc/streamserver/server.toml --root /srv/media/movies
sudo chown streamserver: /etc/streamserver/server.toml
```

`init-config` writes `0600`. Then edit it. The settings that matter on a real
box:

```toml
[server]
host = "0.0.0.0"          # reachable on the LAN. Behind Tailscale, bind to
                          # the tailnet address instead and leave this as
                          # 127.0.0.1 — see "Remote access" below.
port = 8484
allowed_hosts = ["stream.local", "10.0.0.5"]   # empty accepts any Host
cors_origins = []                              # see SECURITY.md
trusted_proxies = []                           # only addresses you control

[media]
library_roots = ["/srv/media/movies"]
stream_chunk_size = 8388608
max_range_span = 536870912
completion_threshold = 0.97

[metadata]
default_provider = "local"    # offline sidecars. Change if you have a key.

[storage]
data_dir = "/var/lib/streamserver/data"
log_level = "INFO"
audit_retention_days = 180
```

**`host = 0.0.0.0` exposes the server to every network the box is attached to.**
If you are not sure the network is trusted, leave it on `127.0.0.1` and reach
it through Tailscale (below).

## 4. Create the database and the first account

```bash
sudo -u streamserver /opt/streamserver/bin/streamserver init-db
sudo -u streamserver /opt/streamserver/bin/streamserver add-user phuree --admin
```

`add-user` prompts for a password twice if `--password` is omitted, enforces
the password policy, and creates a "Main" profile. It creates exactly the
account named on the command line and no other — the `phuree` account is created
by you running this, not by the code, and nothing in the codebase holds that
password. See [SECURITY.md](SECURITY.md#the-default-administrator).

To force a password change at first login — useful when an account is created
by a script or a migration:

```bash
streamserver add-user someone --must-change-password
```

## 5. Import the library

```bash
sudo -u streamserver /opt/streamserver/bin/streamserver scan --root /srv/media/movies
```

A first scan of 1000 titles takes minutes: the walk is a `stat` per file, but
ffprobe runs once per new file and a 4K remux takes a moment to open. The
output is a JSON summary (`added`, `updated`, `skipped`, `failed`).

Schedule it. Library roots change slowly:

```cron
17 4 * * * streamserver scan --root /srv/media/movies
```

`--remove-missing` drops rows whose files have disappeared. Use it on a
schedule, not on every run, so a temporarily unmounted disk does not empty the
library — `verify` is the safer first step:

```bash
streamserver verify            # reports, changes nothing
streamserver scan --remove-missing
```

## 6. Run it as a service

`/etc/systemd/system/streamserver.service`:

```ini
[Unit]
Description=Stream media server
After=network-online.target
Wants=network-online.target

[Service]
Type=exec
User=streamserver
Group=streamserver
ExecStart=/opt/streamserver/bin/streamserver serve -c /etc/streamserver/server.toml
Restart=on-failure
RestartSec=5s

# The server needs to read the media and write its own data directory. It does
# not need anything else.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/streamserver
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
RestrictNamespaces=true
LockPersonality=true
MemoryDenyWriteExecute=false
MemoryMax=1G
TasksMax=256

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now streamserver
sudo systemctl status streamserver
journalctl -u streamserver -f
```

`MemoryMax=1G` is deliberate: a hard ceiling means an accidental full-text
search or a runaway scan cannot take the box down with the OOM killer. The
server's own working set is tens of MiB.

`ProtectSystem=strict` plus `ReadWritePaths` makes the filesystem read-only
except for the data directory. Media is read-only anyway.

## 7. Remote access

**Tailscale, or an internal network, is the recommended way in.** It is one
binary, no port forwarding, and the traffic is encrypted and authenticated at
the network layer — so there is nothing to misconfigure in a router.

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Then leave `host = 127.0.0.1` on the server and reach it over the tailnet
address. The client is pointed at `http://<tailnet-hostname>:8484`.

**Exposing a port directly** works and is supported, but:

- Serve it over HTTPS. The bearer token is a bearer token; over plain HTTP on a
  shared network it is a password in the clear. Terminate TLS in nginx or
  Caddy in front of uvicorn and forward to `127.0.0.1:8484`.
- `trusted_proxies` must list the proxy's address, and only the proxy's
  address — it is what makes `X-Forwarded-For` trustworthy, and setting it too
  broadly lets a client forge the source address that rate limiting and the
  audit log record.
- `allowed_hosts` should list the name clients actually use, so a DNS
  rebinding attempt does not reach an authenticated API.

A Caddy reverse proxy is about ten lines:

```
stream.example.com {
    reverse_proxy 127.0.0.1:8484
    # Streaming: do not buffer, do not cap the body.
    flush_interval -1
}
```

## 8. Backups

The database is small; the media is not, and the media is somebody else's
problem. Back up the database, and back up the config.

```bash
# Daily, 03:17 — snapshot the database.
17 3 * * * streamserver backup -c /etc/streamserver/server.toml
```

`backup` uses SQLite's online backup API, so it is safe to run against a live
server, and it writes gzipped snapshots into `storage.backup_dir` with mode
`0600`. Copy them off the box; a backup on the same disk is not a backup.

```bash
# Restore.
systemctl stop streamserver
streamserver restore /var/lib/streamserver/data/backups/streamserver-2026-10-01.db.gz --force
systemctl start streamserver
```

`restore` refuses without `--force`, keeps the previous file as
`*.pre-restore.bak`, integrity-checks the staging file before swapping, and
deletes stale `-wal`/`-shm` sidecars. Read [DATABASE.md](DATABASE.md#backups-restore-integrity)
before restoring over a live database.

## 9. Housekeeping

```cron
# Nightly: prune expired tokens, sessions, login attempts, playback sessions and
# old audit rows, then compact.
23 4 * * * curl -sX POST -H "Authorization: Bearer $TOKEN" \
         http://127.0.0.1:8484/api/v1/admin/maintenance/prune
41 4 * * * curl -sX POST -H "Authorization: Bearer $TOKEN" \
         http://127.0.0.1:8484/api/v1/admin/maintenance/vacuum
```

`vacuum` checkpoints the WAL and shrinks the file. On a 10 GB SSD with a small
database, this is cheap insurance rather than a necessity.

## 10. Health checks

```bash
curl -fsS http://127.0.0.1:8484/api/health
```

Returns `status`, `database`, `version`, `api_version`; 503 when the database
does not answer. A reasonable monitoring target is that endpoint plus the
`status` field of `GET /api/v1/admin/health`, which also reports library
counts, root readability and whether `ffprobe` is present.

## Resource expectations

| Operation | Memory | Notes |
|---|---|---|
| Server at rest | ~40–80 MiB | page cache is capped at 2 MB per connection |
| 10 concurrent streams | +~20 MiB | 8 MiB buffers, bounded |
| Full scan of 1000 titles | +~30 MiB | constant; nothing large is held |
| `reindex` | +~50 MiB | rebuilds the FTS index in batches |
| Database on disk | 50–200 MiB for 1000 titles | grows with streams and audit rows, not with video |

Nothing is loaded into memory to serve a browse request: listings are SQL
queries with `LIMIT`/`OFFSET` over indexed columns, and the HDD is touched once
per stream open, not once per request.

## Upgrading

```bash
systemctl stop streamserver
cd /path/to/Stream/backend
/opt/streamserver/bin/pip install --upgrade .
/opt/streamserver/bin/streamserver init-db        # applies pending migrations
/opt/streamserver/bin/streamserver verify
systemctl start streamserver
```

Migrations run automatically at startup too; running `init-db` explicitly
means you see a failure before the service comes back up. Take a backup first —
migrations are forward-only and do not get edited after release.
