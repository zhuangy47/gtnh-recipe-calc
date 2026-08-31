"""Unpack the shipped archives, once, on the first start after a clone.

GitHub refuses any file over 100 MiB, and the three files this app reads are
350 MB, 483 MB and 119 MB.  So the repository carries the two *sources* as xz,
and everything else under `data/` is rebuilt from them here:

    data/nesql-db.script.xz    26 MB -> 350 MB   2.5 s   the dump
    data/nesql.sqlite                   483 MB    23 s   converted from it
    data/image.zip.xz          62 MB -> 119 MB   4.3 s   the icons

Shipping the 26 MB dump and paying 23 s to convert it beats shipping the
converted database, which is 62 MB compressed: git keeps every version of every
blob for ever, and the 23 s is paid once per clone.  The icons ship as the zip
itself rather than as deduped payloads, which would save a further 12 MB --
22.8% of the 111,563 members are byte-identical -- but only by rewriting
`icons.py` around a blob and an offset index.  The same argument is why
`data/cache/registry.pickle` is not shipped either -- it is 33 MB and the six
seconds to rebuild it are already handled by `identity.Registry`.

xz rather than zstd because `lzma` is in the standard library, so unpacking adds
no dependency, and it happens to compress this dump 12.8x against zstd's 12.3x.
It is slow to compress and fast to decompress -- 138 MB/s -- which is the right
way round for something compressed once and unpacked on every clone.

Nothing here redoes work.  Each artifact is stamped with the size and mtime of
the archive it came from, in the same `size|mtime` form `db.stamp()` uses, and a
file that is already present but unstamped is adopted rather than rebuilt, so
upgrading an existing checkout costs nothing.
"""
from __future__ import annotations

import lzma
import os
import shutil
import subprocess
import sys
import time

DATA = "data"
SCRIPT_XZ = os.path.join(DATA, "nesql-db.script.xz")
SCRIPT = os.path.join(DATA, "nesql-db.script")
SQLITE = os.path.join(DATA, "nesql.sqlite")
ICONS_XZ = os.path.join(DATA, "image.zip.xz")
ICONS = os.path.join(DATA, "image.zip")
CONVERTER = os.path.join("tools", "nesql_to_sqlite.py")
STAMP_DIR = os.path.join(DATA, "cache")


def _stamp(path):
    st = os.stat(path)
    return "%d|%d" % (st.st_size, int(st.st_mtime))


def _stamp_file(target):
    return os.path.join(STAMP_DIR, os.path.basename(target) + ".stamp")


def _read_stamp(target):
    try:
        with open(_stamp_file(target), "r", encoding="ascii") as fh:
            return fh.read().strip()
    except OSError:
        return None


def _write_stamp(target, source):
    os.makedirs(STAMP_DIR, exist_ok=True)
    with open(_stamp_file(target), "w", encoding="ascii") as fh:
        fh.write(_stamp(source))


def _current(target, source):
    """Is `target` already built from exactly this `source`?

    A target that exists without a stamp is *adopted*: it is almost certainly a
    checkout that predates this module, and rebuilding a good 483 MB file to
    learn nothing would be a poor trade.  It gets a stamp so the question is
    only ever asked once.
    """
    if not os.path.exists(target):
        return False
    want = _stamp(source)
    got = _read_stamp(target)
    if got is None:
        _write_stamp(target, source)
        return True
    return got == want


def _unpack(src, dst, log):
    """Decompress `src` to `dst` through a `.part` file.

    The rename is what makes this safe to interrupt.  Without it a Ctrl-C
    halfway through leaves a truncated file that every later start would happily
    accept, because the only test we have is that it exists.
    """
    part = dst + ".part"
    t0 = time.time()
    with lzma.open(src, "rb") as fh, open(part, "wb") as out:
        shutil.copyfileobj(fh, out, 1024 * 1024)
    os.replace(part, dst)
    log("  %s -> %s  (%.0f MB, %.1fs)"
        % (os.path.basename(src), os.path.basename(dst),
           os.path.getsize(dst) / 1e6, time.time() - t0))


def ensure_icons(log=print):
    if _current(ICONS, ICONS_XZ) or not os.path.exists(ICONS_XZ):
        return
    log("unpacking icons (first run only) ...")
    _unpack(ICONS_XZ, ICONS, log)
    _write_stamp(ICONS, ICONS_XZ)


def ensure_db(log=print):
    """Build `data/nesql.sqlite`, decompressing the dump first if need be."""
    if _current(SQLITE, SCRIPT_XZ) or not os.path.exists(SCRIPT_XZ):
        return
    if not os.path.exists(CONVERTER):
        raise SystemExit(
            "%s is missing, so %s cannot be built from %s."
            % (CONVERTER, SQLITE, SCRIPT_XZ))

    log("building the database (first run only, about 30 s) ...")
    # Only delete a script we unpacked ourselves.  A checkout that already has
    # the 350 MB dump lying around keeps it.
    ours = not os.path.exists(SCRIPT)
    if ours:
        _unpack(SCRIPT_XZ, SCRIPT, log)
    try:
        part = SQLITE + ".part"
        if os.path.exists(part):
            os.remove(part)
        t0 = time.time()
        # A subprocess, not an import: the converter is a script driven by
        # argv, and it sets a 256 MB page cache that we want handed back to the
        # OS when it exits.
        rc = subprocess.call([sys.executable, CONVERTER, SCRIPT, part])
        if rc != 0 or not os.path.exists(part):
            raise SystemExit("%s failed (exit %d)" % (CONVERTER, rc))
        os.replace(part, SQLITE)
        log("  %s  (%.0f MB, %.1fs)"
            % (os.path.basename(SQLITE), os.path.getsize(SQLITE) / 1e6,
               time.time() - t0))
    finally:
        if ours and os.path.exists(SCRIPT):
            os.remove(SCRIPT)
    _write_stamp(SQLITE, SCRIPT_XZ)


def ensure_data(log=print):
    """Everything the app needs on disk before `World` is built."""
    ensure_db(log)
    ensure_icons(log)
