"""Icons, served out of `data/image.zip` by random access.

The stack decision is a browser front-end *because of icons*: GTNH has hundreds
of near-identical plates, dusts and circuits where the icon is the fastest way to
tell two choices apart, and a terminal cannot render the choice list this app is
mostly made of.  119 MB stays zipped; ZipFile seeks straight to the member.

**The zip is opened once for the whole process, not once per thread.**  That is
the whole of what made icons slow, and it is worth the paragraph because the
numbers are so lopsided:

    opening image.zip   0.6 s warm, ~5 s cold   -- 111,563 central directory entries
    reading one member  0.05 ms

The first version kept the `ZipFile` in a `threading.local`.  Werkzeug's dev
server spawns a **thread per connection** and answers with `Connection: close`,
so every single `/icon` request got a fresh thread, and every fresh thread paid
the 0.6 s open before reading its 0.05 ms of PNG.  A node page for Iron Ingot has
665 icons on it; at six parallel connections that is a minute and a half of pure
central-directory parsing, and it blocks navigation because the browser's
connection pool is full of it.

So: one shared handle behind a lock, and a small byte cache in front of it.  A
lock is the right trade at four orders of magnitude -- serialising 665 reads of
0.05 ms costs 33 ms in total, which is less than one thread used to spend opening
the file.  `warm()` pays the open once at startup, where the loader is already
being waited on, so no request ever pays it.
"""
from __future__ import annotations

import base64
import hashlib
import os
import threading
import zipfile

ZIP_PATH = os.path.join("data", "image.zip")

# Icons are 16x16 PNGs -- a few hundred bytes each -- so the whole working set of
# a session is a couple of megabytes.  Capped anyway, because a plan can touch a
# lot of items.
CACHE_MAX = 4096

# 1x1 transparent PNG, for the items whose icon the exporter did not capture.
BLANK = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR4nGNgAAIAAAUAAXpe"
    "qz8AAAAASUVORK5CYII=")
BLANK_ETAG = "blank"


class IconStore:
    def __init__(self, path=ZIP_PATH):
        self.path = path
        self.available = os.path.exists(path)
        self._lock = threading.Lock()
        self._zf = None
        self._names = None
        self._cache = {}
        self._hits = 0
        self._misses = 0

    def warm(self, log=None):
        """Open the zip now, on the loader's time rather than a request's."""
        if not self.available:
            return
        self._open()
        if log:
            log("  icons: %d members indexed" % len(self._names))

    def _open(self):
        """The one open.  Takes `_lock` itself, so call it without holding it."""
        if self._zf is None and self.available:
            with self._lock:
                if self._zf is None:
                    zf = zipfile.ZipFile(self.path)
                    # NameToInfo is already the index ZipFile built; asking for
                    # namelist() again would rebuild a second copy of it.
                    self._names = frozenset(zf.NameToInfo)
                    self._zf = zf
        return self._zf

    def names(self):
        self._open()
        return self._names or frozenset()

    def read(self, member):
        """PNG bytes for an IMAGE_FILE_PATH, or None."""
        if not member or not self.available:
            return None
        hit = self._cache.get(member)
        if hit is not None:
            self._hits += 1
            return hit
        self._open()
        if not self._names or member not in self._names:
            return None
        with self._lock:
            # Re-check: another thread may have read it while we waited.
            hit = self._cache.get(member)
            if hit is not None:
                self._hits += 1
                return hit
            try:
                data = self._zf.read(member)
            except (KeyError, zipfile.BadZipFile, OSError):
                return None
            self._misses += 1
            if len(self._cache) >= CACHE_MAX:
                self._cache.clear()
            self._cache[member] = data
        return data

    def etag(self, member):
        """A strong validator, so a second visit to a page asks for nothing.

        The archive never changes between restarts, so the member name is
        already the whole of the identity.  Returned **unquoted**, which is what
        Werkzeug's `If-None-Match` parsing compares against; the quotes belong
        to the header, not to the tag.
        """
        if not member:
            return BLANK_ETAG
        return hashlib.blake2b(member.encode("utf-8", "replace"),
                               digest_size=10).hexdigest()

    def stats(self):
        return {"cached": len(self._cache), "hits": self._hits,
                "misses": self._misses, "members": len(self._names or ())}
