# Development

## Layout

```
Stream/
├── backend/          the server: Python, FastAPI, SQLite
│   ├── pyproject.toml
│   ├── streamserver/
│   └── tests/
├── client/           the Qt 6 / QML application
├── docs/             this documentation
└── scripts/
```

## Requirements

**Server** — Python 3.11 or newer. `ffprobe` (from ffmpeg) is optional but
strongly recommended: without it, media imports carry no technical metadata
(resolution, codec, audio tracks) and the player cannot offer track selection.

```bash
# Fedora
sudo dnf install python3 python3-pip ffmpeg
# Debian / Ubuntu
sudo apt install python3 python3-pip ffmpeg
```

**Client** — Qt 6.5 or newer with the Quick, Qml, QuickControls2, Network,
Multimedia and Svg modules, a C++17 compiler, CMake 3.21+ and Ninja.

```bash
# Fedora
sudo dnf install qt6-qtbase-devel qt6-qtdeclarative-devel \
                 qt6-qtmultimedia-devel qt6-qtsvg-devel \
                 qt6-qtwayland-devel gcc-c++ cmake ninja-build
# Debian / Ubuntu
sudo apt install qt6-base-dev qt6-declarative-dev qt6-multimedia-dev \
                 qt6-svg-dev cmake ninja-build g++
```

Verified working on Fedora 44 with Qt 6.11.2, CMake 4.3.0, Ninja 1.13.2.

## Getting the server running

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
```

Create a config and a database:

```bash
streamserver init-config -c server.toml --root /srv/media/movies
streamserver init-db
streamserver add-user phuree --admin          # prompts for a password
```

`init-config` writes `0600` and refuses to clobber an existing file without
`--force`. The generated file is the reference for every setting; see
[DEPLOYMENT.md](DEPLOYMENT.md) for what to change on a real box.

First run through the API creates the administrator instead, if you would
rather not use the CLI:

```bash
streamserver serve
curl -X POST localhost:8484/api/v1/auth/setup \
  -H 'Content-Type: application/json' \
  -d '{"username":"phuree","password":"<a real password>"}'
```

Then import something and browse it:

```bash
streamserver scan --root /srv/media/movies
streamserver list
curl localhost:8484/api/health
```

Interactive API docs are at `http://localhost:8484/api/docs`.

## Everyday commands

```bash
streamserver serve                      # run the server (uvicorn)
streamserver init-db                    # create or migrate the database
streamserver init-config                # write a config file
streamserver add-user NAME --admin      # create an account (prompts)
streamserver set-password NAME          # reset without the old password
streamserver scan --root /srv/media     # import new files
streamserver import /path/file.mkv --title "Dune" --year 2021
streamserver list                       # what is in the library
streamserver verify --deep              # re-probe every file
streamserver backup                     # write a .db.gz snapshot
streamserver restore snapshot.db.gz --force
streamserver doctor                     # diagnose a deployment
streamserver token USERNAME             # mint a pair for scripting
```

`streamserver --help` and `streamserver <command> --help` for the full set.
`-c/--config`, `--data-dir` and `--root` work both before and after the
subcommand.

Exit codes: `0` success, `1` an expected failure (with a message on stderr),
`2` argparse usage error, `130` interrupted.

## Configuration

Layered, later wins:

1. Built-in defaults.
2. The TOML config file (`-c`, or `STREAMSERVER_CONFIG`).
3. Environment variables (`STREAMSERVER_*`).
4. Explicit overrides, passed programmatically.

`streamserver/config.py` holds the dataclasses; every field has a default, so
a minimal file works. Keys are `section.field`. The important ones:

| Section | Key | Default | Notes |
|---|---|---|---|
| `server` | `host` | `127.0.0.1` | `0.0.0.0` to expose on the LAN |
| `server` | `port` | `8484` | |
| `server` | `cors_origins` | `[]` | empty = no CORS at all. See SECURITY.md |
| `server` | `allowed_hosts` | `[]` | empty = accept any Host |
| `server` | `trusted_proxies` | `[]` | only set this to addresses you actually control |
| `auth` | `access_token_ttl_seconds` | `900` | |
| `auth` | `password_min_length` | `10` | |
| `auth` | `scrypt_n` | `32768` | raising it costs login time on a 4-core box |
| `media` | `library_roots` | `[]` | absolute paths; nothing outside them is ever served |
| `media` | `stream_chunk_size` | `8388608` | 8 MiB |
| `media` | `completion_threshold` | `0.97` | fraction watched that counts as finished |
| `media` | `ffprobe_path` | `ffprobe` | |
| `metadata` | `default_provider` | `local` | `local`, `tmdb`, `imdb`, `omdb`, or a `+`-joined chain |
| `storage` | `data_dir` | `~/.local/share/streamserver` | keep on the SSD |
| `storage` | `audit_retention_days` | `180` | |

## Tests

```bash
cd backend
python -m pytest              # ~500 tests, about a minute
python -m pytest -q tests/test_api_playback.py
python -m pytest -k filename
```

`pyproject.toml` sets `-p no:xvfb`: the globally installed `pytest-xvfb`
plugin blocks for 30 seconds per run waiting for a virtual display this
project never opens. If you see unexplained 30-second pauses, that is why.

Useful flags: `-x` stop at the first failure, `--lf` rerun the last failures,
`-vv` for the full assertion diff, `-k <expr>` to select.

See [TESTING.md](TESTING.md) for what is covered and what is not.

## Development conventions

**Python.** Type hints on public functions, `from __future__ import annotations`
at the top of every module, dataclasses over dictionaries for structured
values, and no ORM — the SQL is in `repositories.py` where it can be read,
indexed and audited in one place.

**Comments explain *why*.** A comment that restates the code is noise. A comment
that records a decision, a failure mode, or a bug that would otherwise come
back is the most valuable thing in the file. Several in this codebase exist
because a test found the bug the comment describes.

**Layering.** `api/` has no SQL. `db/repositories.py` has no business rules.
`services/` never imports `fastapi`. If a change needs to cross a layer, that is
usually a sign the change belongs somewhere else.

**Security decisions are server-side.** A check the client could skip is not a
check. If a route's only protection is "the client hides the button", it is
unprotected.

**Tests come with the bug.** Every fix in this repository's history was found by
a test, and the commit that fixes it names the failure. When you find a bug,
write the test that finds it first.

## Building the client

```bash
cd client
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build build
./build/stream-client
```

Verified on this machine: Qt 6.11.2 with Quick, Qml, QuickControls2, Network,
Multimedia and Svg; CMake 4.3.0; Ninja 1.13.2.

`qt_standard_project_setup` plus one `qt_add_qml_module` for the `Stream`
module, which carries both the QML and the C++ types registered with
`QML_ELEMENT`. `QT_NO_CAST_FROM_ASCII` is on for every target, so a string
literal in a C++→QML boundary is a compile error rather than a silent
transliteration — and, on the test targets, so is a printf-style `qWarning`,
which Qt 6 drops along with the rest of that overload.

### Lint

`qmllint` runs as a `POST_BUILD` step on `stream-client` and fails the build on
any warning. `client/lint.sh` finds the linter and the import path; the
suppressions it applies live in `client/.qmllint.ini`, each with a written
justification. Add a suppression with its reason, never without one.

### Tests

```bash
cd client
cmake -S . -B build-test -G Ninja && cmake --build build-test
(cd build-test && ctest --output-on-failure)
```

One executable per suite — two `QTEST_MAIN` files in one binary is two
definitions of `main()` — and every one is `QTEST_GUILESS_MAIN`, because
`QTEST_MAIN` aborts on a headless machine and reports as "Subprocess aborted"
rather than as a failure. ctest sets `QT_QPA_PLATFORM=offscreen` anyway.

`client-e2e` needs `python3` and `ffmpeg` on `PATH` and starts the real server;
`client/tests/e2e/README.md` explains the fixture. Pass
`-DSTREAM_BUILD_E2E=OFF` to skip building it. It skips with a *reason* rather
than failing twenty times over "ffmpeg is missing".

### Adding a screen

1. The behaviour goes in a C++ controller exposed as a `QML_ELEMENT`; the QML
   binds to it. A screen that reaches the network itself cannot be tested
   without a window.
2. `MediaListModel`-style models carry the data; the QML binds and never
   mutates.
3. Every asynchronous load needs three states — loading, empty, error — and the
   error state says *what* went wrong, not "something went wrong".
4. `qt_add_qml_module`'s `QML_FILES` list is the module's manifest: a file not
   in it is not compiled, not linted and not found at runtime.

## Repository hygiene

- `.gitignore` covers build output, virtualenvs, `__pycache__`, runtime data
  (`/data/`, `*.db`, `*.key`, `*.log`) and the editor directories this checkout
  happens to contain.
- Never commit `server.toml` with real credentials, a `signing.key`, a database,
  or a backup. `init-config` writes `0600`; keep it that way.
- Example configuration in this repository contains placeholders only.
- Logical commits: one change, one message, describing what was wrong and why.
