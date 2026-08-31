"""Classification: turn an item's oredict tags into a (form, material) label.

This is the consumer side of `data/rank.json`, which `tools/derive_rank.py`
derived from the dump.  Nothing here re-derives anything; the two orders and the
grid are read out of the artifact so that a rebuild of the artifact changes the
app without changing this file.

Two rules from the design carry through and are worth restating where they are
implemented:

  * **The oredict is a relation, not a function, in both directions.**  A tag
    names a set of items and an item carries a list of tags.  So `byForm`
    returns (members, default) and an item keeps `tags`; `form`/`material` are
    *derived labels that nothing may branch on* -- they exist to be shown to a
    human and to drive the material re-pick, never to construct an item.
  * **`byForm` must distinguish "no such tag" from "tag registered, no
    members"** -- 99,346 of 121,352 registered names are empty, because GT
    registers the prefix x material cross product.  `lookup` returns None for
    the first and an empty tuple for the second.
"""
from __future__ import annotations

import json
import os
import re

RANK_PATH = os.path.join("data", "rank.json")
OVERLAY_PATH = os.path.join("data", "classification_overlay.json")

BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

_MISSING = object()


def camel_splits(name):
    return [(name[:m.start()], name[m.start():]) for m in BOUNDARY.finditer(name)]


class Rank:
    """`data/rank.json`, loaded.  Both orders, the grid, and the overrides."""

    def __init__(self, path=RANK_PATH):
        self.path = path
        with open(path, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        tr = d["tag_rank"]
        self.prefixes = tr["prefix_density"]
        self.materials = tr["material_density"]
        self.collisions = {k: tuple(v) for k, v in tr["collisions"].items()}
        self.alias_materials = list(tr["alias_materials"])
        sr = d["source_rank"]
        self.mod_order = {m: i for i, m in enumerate(sr["mod_order"])}
        self.overrides = d.get("overrides") or {}
        self.tag_overrides = self.overrides.get("tag_rank") or {}
        self.source_overrides = self.overrides.get("source_rank") or {}
        self._split_cache = {}

    def stamp(self):
        st = os.stat(self.path)
        return "%d|%d" % (st.st_size, int(st.st_mtime))

    def split(self, tag):
        """Longest matching grid prefix, with GT's collision exceptions."""
        hit = self._split_cache.get(tag, _MISSING)
        if hit is not _MISSING:  # None is a real answer: "not grid-shaped"
            return hit
        if tag in self.collisions:
            out = self.collisions[tag]
        else:
            best = None
            for p, s in camel_splits(tag):
                if p in self.prefixes and (best is None or len(p) > len(best[0])):
                    best = (p, s)
            out = best
        self._split_cache[tag] = out
        return out

    def tag_rank_key(self, tag, siblings, members):
        """item -> tags.  Lower sorts first; the first tag wins the label.

        Ported from `tools/derive_rank.py`, whose docstring is the argument for
        each component.  The one thing worth repeating here: the `Any` demotion
        is its own component *above* subsumption, because the two disagree --
        ingotBronze is a strict superset of ingotAnyBronze, the inverse of
        plateAnyIron/plateIron, and merging them hands the win to the alias.
        """
        mine = members.get(tag) or ()
        mine = mine if isinstance(mine, frozenset) else frozenset(mine)
        name_alias = any(tag.endswith(m) and tag != m for m in self.alias_materials)
        subsumes = False
        for other in siblings:
            if other == tag:
                continue
            om = members.get(other)
            if om and mine > frozenset(om):
                subsumes = True
                break
        sp = self.split(tag)
        if sp is None:
            return (int(name_alias), int(subsumes), 1, 0, 0, 0, tag)
        prefix, material = sp
        more_specific = False
        for t in siblings:
            if t == tag:
                continue
            s2 = self.split(t)
            if s2 and material != s2[1] and material.endswith(s2[1]):
                more_specific = True
                break
        return (int(name_alias), int(subsumes), 0, -self.prefixes.get(prefix, 0),
                -int(more_specific), -self.materials.get(material, 0), tag)

    def source_rank_key(self, gid, mod, produced):
        """tag -> items.  Lower sorts first; the first item is the default."""
        return (self.mod_order.get(mod, len(self.mod_order)), -produced, gid)

    def label_for(self, gid, tags, members):
        """(tag, form, material) for an item, or (None, None, None).

        `overrides.tag_rank[gid]` pins the label tag by hand; that is the one
        place a correction belongs, never in the derived tables.
        """
        pinned = self.tag_overrides.get(gid)
        if pinned:
            sp = self.split(pinned)
            return (pinned, sp[0] if sp else None, sp[1] if sp else None)
        if not tags:
            return (None, None, None)
        sibs = sorted(tags)
        best = min(sibs, key=lambda t: self.tag_rank_key(t, sibs, members))
        sp = self.split(best)
        return (best, sp[0] if sp else None, sp[1] if sp else None)


class ClassificationOverlay:
    """Hand-authored (form, material) for items that carry no oredict tag.

    600 items across the GT casing/coil families and all three Railcraft tank
    block families carry zero tags, so this is mandatory rather than a fallback:
    without it "make the tank steel instead of iron" has nothing to re-pick on.
    Keyed by `mod:internalName@damage`.
    """

    def __init__(self, path=OVERLAY_PATH):
        self.path = path
        self.entries = {}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self.entries = data.get("entries", data)

    def stamp(self):
        if not os.path.exists(self.path):
            return "none"
        st = os.stat(self.path)
        return "%d|%d" % (st.st_size, int(st.st_mtime))

    def get(self, gid):
        e = self.entries.get(gid)
        if not e:
            return None
        return (e.get("form"), e.get("material"))

    def save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"note": "Hand-authored (form, material) for zero-tag items. "
                               "Keyed by mod:internalName@damage.",
                       "entries": self.entries}, fh, indent=1, sort_keys=True)

    def set(self, gid, form, material):
        self.entries[gid] = {"form": form, "material": material,
                             "provenance": "user"}
        self.save()
