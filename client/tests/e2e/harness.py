#!/usr/bin/env python3
"""Build -- or tear down -- a real Stream server for the end-to-end tests.

The point of this script is that it is the *server*, not a fake of one: a real
configuration file, a real SQLite database with its own signing key, a real
account, and a library root holding a real, decodable media file produced by
ffmpeg.  Everything the client does afterwards goes over HTTP against it, which
is the only way the interesting failures show up -- a stub server that answers
with a canned JSON body cannot tell you that the client's resume offset never
reaches the server, or that a Range request is silently ignored.

Two commands:

    e2e/harness.py up     -- write the fixture and print one JSON line
    e2e/harness.py down   -- stop the server the fixture started

`up` starts the server itself (uvicorn in a subprocess) and does *not* return
until `/api/health` answers, so a test that gets a fixture back knows the port
is live.  It prints:

    {"url": ..., "data_dir": ..., "root": ..., "port": ...,
     "username": ..., "password": ..., "profile_id": ...}

which is everything a C++ test needs and nothing it has to guess.

Deliberate choices:

* Cheap KDF parameters.  scrypt at production cost is ~100 ms, and this
  fixture signs in several times per test run; the tests that care about the
  cost assert on the parameters, not the wall clock.
* A generated video, not random bytes.  The player needs a container the
  backend can probe and the decoder can open, or "playback works" is untested.
* `allow_insecure_transport` is *not* set.  The server binds loopback and
  permits plain HTTP there on its own; turning the flag on would be granting an
  exception the deployment does not need, and the client would then be tested
  against a configuration nobody should ship.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

#: The account the tests sign in as.  Not a secret: it is created fresh in a
#: throwaway database every run, and it is not the deployment's default
#: credential (the server refuses to create a second one otherwise).
USERNAME = "e2e"
PASSWORD = "e2e-2024-test-password"

#: One title, one second of video and one of audio.  Long enough for ffprobe to
#: report a real duration -- which is what makes "resume from 0.5 s" a
#: meaningful assertion rather than a rounding artefact -- and short enough that
#: importing it costs nothing.
CLIP_SECONDS = 1

REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = REPO_ROOT / "backend"


def free_port() -> int:
    """A port nothing is listening on.

    Bound then released, which is the only portable way to ask.  There is a
    race between the release and the server's bind, and it is the right race:
    the alternative is holding the socket open and passing it to a child, which
    uvicorn does not accept, and which would make the fixture depend on
    inherited descriptors.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def make_media(root: Path) -> Path:
    """Write a real, decodable clip into the library root.

    ``lavfi`` needs no input files, so this works on a build machine with no
    media and no network.  The result is a real Matroska container with an
    H.264 video track and an AAC audio track -- the two things the backend
    probes for and the two things a decoder has to open.
    """
    root.mkdir(parents=True, exist_ok=True)
    path = root / "Test.Feature.2021.1080p.WEB-DL.mkv"
    if path.exists() and path.stat().st_size > 0:
        return path

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise SystemExit(
            "ffmpeg is not on PATH. The end-to-end tests need it to make a real "
            "media file; a library of random bytes would only exercise the "
            "error paths."
        )

    result = subprocess.run(
        [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i",
            f"testsrc=duration={CLIP_SECONDS}:size=320x240:rate=10",
            "-f", "lavfi", "-i",
            f"sine=frequency=440:duration={CLIP_SECONDS}",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(path),
        ],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0 or not path.exists():
        raise SystemExit(f"ffmpeg failed to make a test clip:\n{result.stderr}")
    return path


#: `python -m streamserver` does not work -- the package has no `__main__`, its
#: entry point is the console script `streamserver = streamserver.cli:main`.
#: Running the module the way an operator does, rather than importing the CLI,
#: means the fixture fails the same way a broken installation would.
CLI_MODULE = "streamserver.cli"


def streamserver(*args: str, config: Path | None = None,
                 check: bool = True) -> subprocess.CompletedProcess:
    """Run the server's own CLI."""
    argv = [sys.executable, "-m", CLI_MODULE, *args]
    if config is not None:
        argv += ["-c", str(config)]
    result = run_cli(argv, check=check)
    if check and result.returncode != 0:
        raise SystemExit(
            f"streamserver {' '.join(args)} failed ({result.returncode}):\n"
            f"{result.stdout}\n{result.stderr}"
        )
    return result


def cli_env() -> dict[str, str]:
    """The environment every backend process needs.

    The key is the loopback address, and it is here rather than in the call
    sites because it is the environment's job: `no_proxy` has to be set before
    any HTTP client in this process reads it.  A machine with
    ``http_proxy`` set routes 127.0.0.1 through the proxy, and the symptom is a
    server that starts, answers its health check, and is then unreachable from
    everything else -- including the tests.
    """
    return {
        **os.environ,
        "PYTHONPATH": str(BACKEND_ROOT),
        "no_proxy": "127.0.0.1,localhost,::1",
        "NO_PROXY": "127.0.0.1,localhost,::1",
    }


def run_cli(argv: list[str], *, check: bool = False) -> subprocess.CompletedProcess:
    """Run a CLI invocation with the backend importable."""
    return subprocess.run(argv, capture_output=True, text=True,
                          env=cli_env(), cwd=str(BACKEND_ROOT), check=check)


def write_config(data_dir: Path, root: Path, port: int) -> Path:
    """The configuration file, written by hand.

    Not ``init-config`` plus a patch-up: that command writes the defaults, and
    this fixture needs a handful of them changed in ways a test should be able
    to read in one place.  The two commands are checked separately by the
    backend's own suite.
    """
    config = data_dir / "server.toml"
    config.write_text(
        f"""
# Written by client/tests/e2e/harness.py.  Throwaway: the whole data directory
# is deleted when the fixture goes away.

[server]
host = "127.0.0.1"
port = {port}
allowed_hosts = ["127.0.0.1", "localhost", "testserver"]

[storage]
data_dir = "{data_dir}"
log_to_file = false
log_level = "WARNING"

[auth]
# 2**10 rather than 2**15: ~1 ms instead of ~100 ms.  The KDF is still a real
# one -- these tests are about the client, not about scrypt.
scrypt_n = 1024
scrypt_maxmem = 16777216

[media]
library_roots = ["{root}"]

[rate_limit]
# Raised from the 10/min sign-in ceiling.  Every test signs in, and the suite
# signs in more than ten times inside a couple of seconds, so the production
# limit answered 429 and "Too many sign-in attempts" -- which is the server
# working exactly as designed, and tells a person debugging an end-to-end
# failure nothing about the client.
#
# Nothing here is weakened that these tests depend on: the *lockout* limit
# above it (30/min) is left alone, and `aWrongPasswordIsRejectedWithoutASession`
# still signs in deliberately wrongly to prove the failure path.
login_per_minute = 10000
""".lstrip(),
        encoding="utf-8",
    )
    config.chmod(0o600)
    return config


def wait_for_health(port: int, timeout: float = 30.0,
                    live: Callable[[], bool] | None = None) -> None:
    """Block until the server answers, or say why it did not.

    A fixture that returns before the port is live turns every later failure
    into a confusing connection error instead of "the server never started".
    `live` is asked between attempts so a process that has already exited ends
    the wait immediately rather than burning the whole timeout on a corpse.
    """
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if live is not None and not live():
            raise SystemExit("the server process exited while starting up")
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/health", timeout=2
            ) as response:
                if response.status == 200:
                    return
                last_error = RuntimeError(f"health returned {response.status}")
        except (urllib.error.URLError, OSError, RuntimeError) as exc:
            last_error = exc
        time.sleep(0.1)
    raise SystemExit(
        f"the server never answered /api/health on port {port}: {last_error}"
    )


def stop(pid: int) -> None:
    """Stop the server, then insist.

    A `SIGTERM` uvicorn handles; the wait is for the case where it does not.
    """
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def command_up(keep: Path) -> int:
    # Every directory up front: the config names them, and a missing parent is
    # a FileNotFoundError rather than a message about what went wrong.
    data_dir = keep / "data"
    root = keep / "library"
    data_dir.mkdir(parents=True, exist_ok=True)
    make_media(root)

    port = free_port()
    config = write_config(data_dir, root, port)

    streamserver("init-db", config=config)
    streamserver("add-user", USERNAME, "--password", PASSWORD, "--admin",
                 "--profile-name", "Main", config=config)
    streamserver("scan", "--root", str(root), config=config)

    # The pid file is what `down` needs; it is the only thing that survives a
    # round trip between this process and the one the test runs.
    log_path = keep / "server.log"
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", CLI_MODULE, "serve", "-c", str(config)],
            stdout=log, stderr=subprocess.STDOUT, env=cli_env(),
            cwd=str(BACKEND_ROOT), start_new_session=True,
        )
        (keep / "server.pid").write_text(str(process.pid), encoding="utf-8")

        try:
            wait_for_health(port, live=lambda: process.poll() is None)
        except SystemExit as failure:
            # Report *why*, from the log, rather than only that the port never
            # opened: a fixture that fails with "connection refused" and no
            # explanation is the least useful thing in a test suite.
            exit_code = process.poll()
            log.flush()
            detail = log_path.read_text(encoding="utf-8").strip()
            if exit_code is not None:
                raise SystemExit(
                    f"{failure}\n"
                    f"The server exited with status {exit_code} before it "
                    f"answered. Its output:\n{detail or '(nothing)'}"
                ) from failure
            stop(process.pid)
            raise

    listing = streamserver("list", "--limit", "50", config=config)
    if "Test Feature" not in listing.stdout:
        raise SystemExit(
            f"the fixture imported no titles:\n{listing.stdout}\n{listing.stderr}"
        )

    fixture = {
        "url": f"http://127.0.0.1:{port}",
        "port": port,
        "data_dir": str(data_dir),
        "root": str(root),
        "username": USERNAME,
        "password": PASSWORD,
    }
    # Written as well as printed.  A C++ test would otherwise have to scrape
    # stdout, and the pid file already proves that something on disk survives
    # the round trip.
    (keep / "fixture.json").write_text(json.dumps(fixture), encoding="utf-8")
    print(json.dumps(fixture))
    return 0


def command_down(keep: Path) -> int:
    pid_file = keep / "server.pid"
    if pid_file.exists():
        try:
            stop(int(pid_file.read_text(encoding="utf-8").strip()))
        except ValueError:
            pass
        pid_file.unlink(missing_ok=True)
    shutil.rmtree(keep, ignore_errors=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("up", "down"))
    parser.add_argument(
        "--dir", required=True,
        help="the scratch directory to build the fixture in; it is created, "
             "and removed by `down`",
    )
    args = parser.parse_args(argv)

    keep = Path(args.dir)
    if args.command == "up":
        keep.mkdir(parents=True, exist_ok=True)
        return command_up(keep)
    return command_down(keep)


if __name__ == "__main__":
    raise SystemExit(main())
