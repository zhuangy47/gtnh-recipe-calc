"""Identity: interning, the registry, `Bag`.  §14 step 1.

Every item interns on `(gameId, meta)` -- `modId:internalName` plus a
canonicalised damage/NBT discriminator.  The dump has 1,373 (mod, name, damage)
keys with several ITEM rows, differing only in NBT, so the dump's row id is
*not* an identity: `{energy:0L}` and `{energy:0}` are the same item and arrive
with different `sha1(nbt)` suffixes.

An interned item is addressed by its **ix** -- the dump id of the class's
representative row.  Keeping ix inside the `i~...` namespace means a recipe row
joins straight back to it, and `members` carries the other rows of the class so
the backward walk can ask for all of them at once.

`form` and `material` are classification *annotations* and never a way to
construct an item; `ItemRef` is always a lookup.  See classify.py.

The cache: reading and canonicalising 109,601 items takes about six seconds, so
the whole registry is pickled to `data/cache/`.  The cache key includes the
dump's stamp, rank.json's, and the overlay's, so any of them changing rebuilds
it.  Nothing derived is ever stored anywhere else.
"""
from __future__ import annotations

import collections
import os
import pickle
import re
import sys
import time

from . import nbt
from .classify import ClassificationOverlay, Rank

CACHE_DIR = os.path.join("data", "cache")
CACHE_FILE = os.path.join(CACHE_DIR, "registry.pickle")
CACHE_FORMAT = 5

# GT keeps tool wear in NBT: every metatool has MAX_DAMAGE = 0 and no tool
# class, so the dump's durability columns are useless here (they were tried and
# rejected -- 525 false positives, approximately no true ones).
TOOL_FAMILIES = ("gt.metatool.01", "gt.detrav.metatool.01", "gt.plusplus.metatool.01")


class Item:
    __slots__ = ("ix", "gid", "mod", "internal", "damage", "name", "image",
                 "stack", "members", "tags", "tag", "form", "material",
                 "tool_material", "max_damage")

    def __init__(self, ix, gid, mod, internal, damage, name, image, stack,
                 members, tags, tag, form, material, tool_material, max_damage):
        self.ix = ix
        self.gid = gid
        self.mod = mod
        self.internal = internal
        self.damage = damage
        self.name = name
        self.image = image
        self.stack = stack
        self.members = members
        self.tags = tags
        self.tag = tag
        self.form = form
        self.material = material
        self.tool_material = tool_material
        self.max_damage = max_damage

    kind = "item"

    @property
    def label(self):
        if self.form and self.material:
            return "%s %s" % (self.form, self.material)
        return self.tag or ""

    def __repr__(self):
        return "<Item %s %r>" % (self.gid, self.name)


class Fluid:
    __slots__ = ("ix", "gid", "mod", "internal", "name", "image", "gaseous")

    def __init__(self, ix, gid, mod, internal, name, image, gaseous):
        self.ix = ix
        self.gid = gid
        self.mod = mod
        self.internal = internal
        self.name = name
        self.image = image
        self.gaseous = gaseous

    kind = "fluid"
    damage = 0
    members = ()
    tags = ()
    tag = None
    form = "fluid"
    material = None
    stack = 1
    tool_material = None
    max_damage = 0
    label = "fluid"

    def __repr__(self):
        return "<Fluid %s %r>" % (self.gid, self.name)


class Bag(dict):
    """A multiset of ix -> quantity.  Merges by addition; that is all it is."""

    def add(self, ix, qty):
        if qty:
            self[ix] = self.get(ix, 0) + qty
        return self

    def merge(self, other):
        for ix, qty in other.items():
            self.add(ix, qty)
        return self

    def positive(self):
        return Bag({ix: q for ix, q in self.items() if q > 0})


# --------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------

def _representative(ids):
    """The class member that stands for it.  Prefer the row with no NBT
    discriminator, so `i~gregtech~gt.metatool.01~12` beats a sha1-suffixed
    sibling and the ix stays readable; otherwise the smallest id, so a rebuild
    is reproducible."""
    bare = [i for i in ids if i.count("~") == 3]
    return min(bare) if bare else min(ids)


def _build(db, rank, overlay, log=print):
    t0 = time.time()
    classes = collections.defaultdict(list)
    row_of = {}
    for iid, mod, internal, dmg, name, raw, image, stack in db.all_items():
        key = (mod, internal, dmg, nbt.canonical(raw))
        classes[key].append(iid)
        row_of[iid] = (mod, internal, dmg, name, raw, image, stack)
    log("  interned %d rows into %d items in %.1fs"
        % (len(row_of), len(classes), time.time() - t0))

    by_db_id = {}
    items = {}
    for key, ids in classes.items():
        ix = _representative(ids)
        for i in ids:
            by_db_id[i] = ix
        mod, internal, dmg, name, raw, image, stack = row_of[ix]
        gid = "%s:%s@%d" % (mod, internal, dmg)
        tool_material = None
        max_damage = 0
        if internal in TOOL_FAMILIES and raw:
            stats = nbt.get_path(raw, "GT.ToolStats")
            if isinstance(stats, dict):
                tool_material = stats.get("PrimaryMaterial")
                md = stats.get("MaxDamage")
                max_damage = int(md) if isinstance(md, (int, float)) else 0
        items[ix] = Item(ix, gid, mod, internal, dmg, name, image, stack,
                         tuple(ids), (), None, None, None, tool_material,
                         max_damage)

    fluids = {}
    for fid, mod, internal, name, image, gaseous in db.all_fluids():
        fluids[fid] = Fluid(fid, "%s:%s" % (mod, internal), mod, internal, name,
                            image, bool(gaseous))

    # -- oredict, through ORE_DICTIONARY ---------------------------------
    tag_names = set(n for (n,) in db.all_tag_names())
    members = collections.defaultdict(set)
    tags_of = collections.defaultdict(set)
    for tag, iid in db.all_tag_members():
        ix = by_db_id.get(iid)
        if ix is None:
            continue
        members[tag].add(ix)
        tags_of[ix].add(tag)
    members = {t: frozenset(s) for t, s in members.items()}
    log("  oredict: %d registered names, %d with members, %d tagged items"
        % (len(tag_names), len(members), len(tags_of)))

    # -- labels, via tagRank ---------------------------------------------
    for ix, tags in tags_of.items():
        it = items[ix]
        it.tags = tuple(sorted(tags))
        tag, form, material = rank.label_for(it.gid, it.tags, members)
        it.tag, it.form, it.material = tag, form, material

    # -- the hand-authored overlay, for the 600 zero-tag items -----------
    n_overlaid = 0
    for ix, it in items.items():
        hit = overlay.get(it.gid)
        if hit and (it.form is None or it.material is None):
            it.form, it.material = hit
            n_overlaid += 1
    log("  classification overlay: %d items labelled by hand" % n_overlaid)

    # -- source rank: the default member of each tag ---------------------
    produced = collections.Counter()
    for iid, n in db.item_output_counts():
        ix = by_db_id.get(iid)
        if ix is not None:
            produced[ix] += n
    tag_members = {}
    for tag, ms in members.items():
        pinned = rank.source_overrides.get(tag)
        ordered = sorted(ms, key=lambda ix: rank.source_rank_key(
            items[ix].gid, items[ix].mod, produced.get(ix, 0)))
        if pinned:
            keep = [ix for ix in ordered if items[ix].gid == pinned]
            if keep:
                ordered = keep + [ix for ix in ordered if ix not in keep]
        tag_members[tag] = tuple(ordered)

    # -- tool variants: bare tool id -> the material-specific NBT rows ---
    tool_variants = collections.defaultdict(list)
    for ix, it in items.items():
        if it.tool_material and it.max_damage:
            bare = "i~%s~%s~%d" % (it.mod, it.internal, it.damage)
            if bare in items:
                tool_variants[bare].append(ix)
    for bare, vs in tool_variants.items():
        vs.sort(key=lambda ix: (items[ix].max_damage, items[ix].tool_material))
    tool_variants = {k: tuple(v) for k, v in tool_variants.items()}
    log("  tool variants: %d tool slots carry %d material variants"
        % (len(tool_variants), sum(len(v) for v in tool_variants.values())))

    # -- ore leaves that are nonetheless produced ------------------------
    # §7 assumed "ores are already leaves".  157 of them are recipe outputs
    # (873 of the 885 recipes are Space Mining), so they are seeded into `cut`
    # by default: the walk terminates there unless the user says otherwise.
    ore_items = set()
    for tag, ms in members.items():
        if tag.startswith("ore"):
            ore_items.update(ms)
    produced_ore = frozenset(ix for ix in ore_items if produced.get(ix, 0))
    log("  ore leaves: %d ore-classified items, %d of them produced -> seeded cut"
        % (len(ore_items), len(produced_ore)))

    # -- filled containers, the seed for `Swapped` -----------------------
    containers = {}
    for cid, empty, fluid, amount in db.all_fluid_containers():
        ix = by_db_id.get(cid)
        eix = by_db_id.get(empty) if empty else None
        if ix is not None:
            containers[ix] = (eix, fluid, amount)

    # -- (form, material) -> items ---------------------------------------
    # The index the material re-pick walks.  It has to cover *both* label
    # sources: an oredict tag alone would miss every Railcraft tank block and
    # every GT casing, which carry no tags at all -- and those are exactly the
    # items the iron-vs-steel question is asked about.
    label_index = collections.defaultdict(list)
    for ix, it in items.items():
        if it.form and it.material:
            label_index[(it.form, it.material)].append(ix)
    for key, ixs in label_index.items():
        ixs.sort(key=lambda ix: rank.source_rank_key(
            items[ix].gid, items[ix].mod, produced.get(ix, 0)))
    label_index = {k: tuple(v) for k, v in label_index.items()}
    log("  labels: %d (form, material) pairs" % len(label_index))

    return {
        "format": CACHE_FORMAT,
        "items": items,
        "label_index": label_index,
        "fluids": fluids,
        "by_db_id": by_db_id,
        "tag_names": frozenset(tag_names),
        "tag_members": tag_members,
        "produced": dict(produced),
        "tool_variants": tool_variants,
        "ore_cut": produced_ore,
        "containers": containers,
    }


class Registry:
    """The interned world: items, fluids, oredict, labels, defaults."""

    def __init__(self, db, rank=None, overlay=None, log=print, use_cache=True):
        self.db = db
        self.rank = rank or Rank()
        self.overlay = overlay or ClassificationOverlay()
        stamp = "|".join([str(CACHE_FORMAT), db.stamp(), self.rank.stamp(),
                          self.overlay.stamp()])
        data = None
        if use_cache:
            data = _load_cache(stamp, log)
        if data is None:
            log("building registry from %s (first run takes ~20s) ..." % db.path)
            data = _build(db, self.rank, self.overlay, log)
            if use_cache:
                _save_cache(stamp, data, log)
        self.items = data["items"]
        self.label_index = data["label_index"]
        self.fluids = data["fluids"]
        self.by_db_id = data["by_db_id"]
        self.tag_names = data["tag_names"]
        self.tag_members = data["tag_members"]
        self.produced = data["produced"]
        self.tool_variants = data["tool_variants"]
        self.ore_cut = data["ore_cut"]
        self.containers = data["containers"]
        self._search_index = None
        self._match_cache = None

    # -- lookup ----------------------------------------------------------
    def get(self, ix):
        """An Item or Fluid by ix, or None.  Accepts a raw dump id too."""
        if ix is None:
            return None
        if ix.startswith("f~"):
            return self.fluids.get(ix)
        it = self.items.get(ix)
        if it is not None:
            return it
        canon = self.by_db_id.get(ix)
        return self.items.get(canon) if canon else None

    def canon(self, db_id):
        return self.by_db_id.get(db_id, db_id)

    def by_gid(self, gid):
        """`mod:internalName@damage` -> Item.  Never key on localized name:
        two distinct items are both "Integrated Logic Circuit"."""
        m = re.match(r"^(.*?):(.*)@(-?\d+)$", gid)
        if not m:
            return None
        ix = "i~%s~%s~%s" % (m.group(1), m.group(2), m.group(3))
        return self.items.get(ix)

    def register_synthetic(self, ix, name):
        """Mint an item the dump has never heard of: the thing a hand-entered
        blueprint makes.

        "Railcraft Iron Tank 5x5x5" is not an item in any registry -- it is a
        shape you build out of blocks -- but it has to be *demandable* for the
        tank to be one node rather than nine roots.  Minted identity is honest
        about what it is: it exists because this model created it, and no dump
        will ever corroborate it.  Deliberately minimal -- the parametric
        `EStructure` that mints on a binding is a later step.
        """
        got = self.items.get(ix)
        if got is not None:
            got.name = name
            return got
        item = Item(ix, "structure:" + ix[2:] if ix.startswith("u~") else ix,
                    "(hand-entered)", ix.split("~", 1)[-1], 0, name, "", 1,
                    (ix,), (), None, "structure", None, None, 0)
        self.items[ix] = item
        self._search_index = None
        self._match_cache = None
        return item

    def rank_items(self, ixs):
        """Order candidate items by `sourceRank` -- gregtech first, then by how
        many recipes produce it.

        This is what decides which member of a set-valued slot is pre-selected,
        and it is not cosmetic: the `ingotSteel` slot lists four items and the
        dump's own order puts a Galacticraft ingot that **nothing in this pack
        produces** first.  Offering that as the default silently routes a plan
        through an unmakeable item.
        """
        def key(ix):
            obj = self.items.get(ix)
            if obj is None:
                return (99, 0, ix)
            return self.rank.source_rank_key(obj.gid, obj.mod,
                                             self.produced.get(ix, 0))
        return sorted(ixs, key=key)

    def by_form(self, tag):
        """(members, default) for a tag, or None if no such tag is registered.

        The None/empty distinction is load-bearing: 99,346 of 121,352 names are
        registered with no members, which is GT's disabled-item cross product,
        not a typo in the caller.
        """
        if tag not in self.tag_names:
            return None
        ms = self.tag_members.get(tag, ())
        return (ms, ms[0] if ms else None)

    # -- search ----------------------------------------------------------
    def _index(self):
        if self._search_index is None:
            idx = []
            for ix, it in self.items.items():
                idx.append((it.name.lower(), ix, it.name))
            for ix, f in self.fluids.items():
                idx.append((f.name.lower(), ix, f.name))
            idx.sort(key=lambda r: (len(r[0]), r[0]))
            self._search_index = idx
        return self._search_index

    def ix_matching(self, text):
        """Every ix whose localized name contains `text`, as a set.

        Deliberately not `search`.  That one is for a person: it ranks, caps at
        `limit`, and collapses the 246 hammer variants and the 17 dyed tank walls
        to one row per gameId.  Filtering recipes by their inputs needs the
        opposite -- every variant, no order, no cap -- because a recipe's slot
        names one specific NBT row and collapsing would lose it.

        Returns None for an empty query, which means "no filter" and is a
        different answer from the empty set, "nothing matched".
        """
        q = (text or "").strip().lower()
        if not q:
            return None
        if self._match_cache and self._match_cache[0] == q:
            return self._match_cache[1]
        got = {ix for low, ix, _n in self._index() if q in low}
        self._match_cache = (q, got)
        return got

    def search(self, query, limit=60, kinds=("item", "fluid")):
        """Substring search over localized names, best match first.

        Also accepts a gameId (`gregtech:gt.metaitem.01@32716`) or a raw dump id.

        Results are collapsed to one row per gameId.  A tank wall exists in 17
        dyed NBT variants and a Hammer in 246 material ones; showing all of them
        buries the answer, and both are reachable where they matter -- the tool
        material through the wear picker, the dyed block through its gameId.
        """
        query = (query or "").strip()
        if not query:
            return []
        direct = self.by_gid(query) or self.get(query)
        hits = []
        found = {}
        q = query.lower()
        for low, ix, name in self._index():
            if q not in low:
                continue
            obj = self.get(ix)
            if obj is None or obj.kind not in kinds:
                continue
            if low == q:
                score = 0
            elif low.startswith(q):
                score = 1
            else:
                score = 2
            found[ix] = obj
            hits.append((score, len(low), name, ix))
        hits.sort()
        out, seen = [], set()
        for _s, _l, _n, ix in hits:
            obj = found[ix]
            if obj.gid in seen:
                continue
            seen.add(obj.gid)
            out.append(obj)
            if len(out) >= limit:
                break
        if direct is not None and direct not in out:
            out.insert(0, direct)
        return out


def _load_cache(stamp, log):
    if not os.path.exists(CACHE_FILE):
        return None
    try:
        t0 = time.time()
        with open(CACHE_FILE, "rb") as fh:
            got = pickle.load(fh)
        if got.get("stamp") != stamp:
            log("cache stale (dump, rank.json or overlay changed) -- rebuilding")
            return None
        log("registry cache loaded in %.1fs" % (time.time() - t0))
        return got["data"]
    except Exception as exc:  # a corrupt cache must never be fatal
        log("cache unreadable (%s) -- rebuilding" % exc)
        return None


def _save_cache(stamp, data, log):
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp = CACHE_FILE + ".tmp"
    try:
        sys.setrecursionlimit(20000)
        with open(tmp, "wb") as fh:
            pickle.dump({"stamp": stamp, "data": data}, fh,
                        protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, CACHE_FILE)
        log("registry cached to %s (%.0f MB)"
            % (CACHE_FILE, os.path.getsize(CACHE_FILE) / 1e6))
    except Exception as exc:
        log("could not write cache (%s) -- continuing without it" % exc)
