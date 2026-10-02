#!/usr/bin/env python3
"""Rewrite an extracted RPM tree in place so it runs from a private prefix.

The Qt SDK is unpacked rather than installed, which means absolute paths baked
into the CMake packages, ``*.pc`` files, pkg-config probes and ELF ``RUNPATH``
entries all still point at ``/usr`` and ``/usr/lib64``.  This helper patches
those references to the staging root.

Only text files whose name matches :data:`TEXT_SUFFIXES` are rewritten, and
always as a byte-for-byte replacement -- no re-encoding -- so binary content is
never corrupted even if a prefix happens to occur inside it.
"""

from __future__ import annotations

import argparse
import os
import stat
import sys
from pathlib import Path

TEXT_SUFFIXES = {
    ".cmake", ".pc", ".conf", ".in", ".prf", ".pro", ".pri", ".pc.in",
    ".plist", ".json", ".xml", ".txt", ".sh", ".spec", "", ".cmake.in",
}

# Ordered most-specific-first: /usr/lib64 must win over /usr/lib.
SYS_SUFFIXES = (
    "/usr/lib/x86_64-linux-gnu",
    "/usr/libexec",
    "/usr/lib64",
    "/usr/include",
    "/usr/share",
    "/usr/lib",
    "/usr/bin",
    "/usr",
)

MAX_BYTES = 8 * 1024 * 1024


def needs_rewrite(path: Path) -> bool:
    if path.is_symlink():
        return False
    if path.suffix in TEXT_SUFFIXES and path.suffix not in (".so", ".a"):
        return True
    return path.name in {"moc", "rcc", "uic", "qmake", "qmake6", "lrelease"}


def process(target: Path, prefix: str, dry_run: bool) -> tuple[int, int]:
    if target.is_dir():
        changed = 0
        scanned = 0
        for child in sorted(target.rglob("*")):
            c, s = process(child, prefix, dry_run)
            changed += c
            scanned += s
        return changed, scanned

    if not needs_rewrite(target):
        return 0, 1
    try:
        if target.stat().st_size > MAX_BYTES:
            return 0, 1
        data = target.read_bytes()
    except (OSError, PermissionError):
        return 0, 1

    original = data
    prefix_b = prefix.encode()
    for suffix in SYS_SUFFIXES:
        data = data.replace(suffix.encode(), prefix_b + suffix[len(b"/usr"):].rstrip(b"/"))

    if data == original:
        return 0, 1
    if not dry_run:
        mode = target.stat().st_mode
        target.write_bytes(data)
        os.chmod(target, mode & 0o7777)
    return 1, 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("paths", nargs="+", type=Path)
    ap.add_argument("--prefix", default=None,
                    help="staging root (default: derived from the first path)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.prefix:
        prefix = args.prefix.rstrip("/")
    else:
        # <root>/usr/lib64/... -> <root>
        first = args.paths[0].resolve()
        parts = first.parts
        prefix = str(Path(*parts[: parts.index("usr")])) if "usr" in parts else str(first.parent)

    total_changed = 0
    total_scanned = 0
    for p in args.paths:
        if not p.exists():
            continue
        c, s = process(p, prefix, args.dry_run)
        total_changed += c
        total_scanned += s

    print(f"relocate: scanned {total_scanned} files, rewrote {total_changed} (prefix={prefix})")
    return 0


if __name__ == "__main__":
    sys.exit(main())