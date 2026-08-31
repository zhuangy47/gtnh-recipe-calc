"""`RecipeIndex` -- §14 step 2, grounding as enumeration.

For the guided walk, grounding's job is narrower than the LP's: **enumerate
every recipe that outputs the demanded item**, and desugar a set-valued slot into
a list the *user* picks from rather than a choice node a solver prices.

Three things the dump makes true and this module has to respect:

  * **Slots are not ingredients.**  The Basic Steam Turbine is 9 slots and 6
    ingredients.  Aggregation to ingredients can only happen *after* every
    set-valued slot is resolved, because an unresolved set has no item to
    aggregate on.
  * **The machine class is `(map, minTier)`** and comes free from the recipe
    type id -- a recipe is listed once, under its minimum voltage.  Machines gate
    availability and are never a cost row: asking what an electronic circuit
    costs must not cost an assembler.
  * **A choice may never contain its own item.**  1,136 recipes output an item
    that is also a member of one of their own input groups -- backpack dyeing,
    rune cycling, tool repair, tier-unification presses.  The worked case is
    "Any LV Circuit", a real item carrying oredict `circuitBasic`, whose Forming
    Press recipe consumes `circuitBasic`, which contains itself.  One test when
    choices are listed covers all 1,136 and needs no curated exclusion list.

User-authored recipes live here too, and they are what make a multiblock an MVP
feature: a structure is a `Recipe` with `machine: None`, one nullable field of
difference from a furnace recipe.
"""
from __future__ import annotations

import json
import os
import re
from fractions import Fraction

from .db import TIER_INDEX, TIERS, parse_recipe_type_id

CUSTOM_PATH = os.path.join("data", "custom_recipes.json")


class Slot:
    """One input slot: its grid position, its oredict name when it has one, and
    its members.  `len(members) > 1` is a set the user resolves."""

    __slots__ = ("key", "tag", "members", "is_fluid")

    def __init__(self, key, tag, members, is_fluid=False):
        self.key = key
        self.tag = tag
        self.members = members       # [(ix, qty)]
        self.is_fluid = is_fluid

    @property
    def set_valued(self):
        return len(self.members) > 1

    @property
    def default(self):
        return self.members[0][0] if self.members else None


class Recipe:
    """One recipe, from the dump or hand-entered.  `machine is None` means
    "built by placing blocks in the world"."""

    __slots__ = ("rid", "name", "rt_id", "category", "shapeless", "mod", "map",
                 "tier", "item_slots", "fluid_slots", "outputs", "gt", "custom",
                 "note")

    def __init__(self, rid, name, rt_id, category, shapeless, mod, map_, tier,
                 item_slots, fluid_slots, outputs, gt=None, custom=False, note=""):
        self.rid = rid
        self.name = name
        self.rt_id = rt_id
        self.category = category
        self.shapeless = shapeless
        self.mod = mod
        self.map = map_
        self.tier = tier
        self.item_slots = item_slots
        self.fluid_slots = fluid_slots
        self.outputs = outputs      # [(ix, qty, chance, key)]
        self.gt = gt
        self.custom = custom
        self.note = note

    @property
    def slots(self):
        return list(self.item_slots) + list(self.fluid_slots)

    @property
    def machine(self):
        """(map, minTier) -- the class-and-gate pair, or None for hand-placed."""
        if self.map is None:
            return None
        return (self.map, self.tier)

    @property
    def tier_index(self):
        return TIER_INDEX.get(self.tier, -1) if self.tier else -1

    def output_for(self, ix):
        """(qty, min chance, expected qty) of `ix` per run, or None."""
        hits = [o for o in self.outputs if o[0] == ix]
        if not hits:
            return None
        qty = sum(h[1] for h in hits)
        chance = min(h[2] for h in hits)
        expected = sum(Fraction(h[1]) * Fraction(h[2]).limit_denominator(10 ** 6)
                       for h in hits)
        return (qty, chance, expected)

    def byproducts(self, primary_ix):
        return [o for o in self.outputs if o[0] != primary_ix]

    def signature(self):
        """Enough to re-find this recipe and to say what changed after a pack
        update.  Recipe ids look content-derived, so a changed recipe yields a
        new id and a saved plan referencing it dangles."""
        return {
            "outputs": sorted(o[0] for o in self.outputs),
            "map": self.map,
            "tier": self.tier,
            "inputs": sorted(m[0] for s in self.slots for m in s.members),
        }


class Choice:
    """A recipe offered at a node, with everything the UI needs to rank it."""

    __slots__ = ("recipe", "qty", "chance", "expected", "self_containing",
                 "gated", "reason")

    def __init__(self, recipe, qty, chance, expected, self_containing, gated,
                 reason=""):
        self.recipe = recipe
        self.qty = qty
        self.chance = chance
        self.expected = expected
        self.self_containing = self_containing
        self.gated = gated
        self.reason = reason

    @property
    def chanced(self):
        return self.chance < 1.0


class Offer:
    """What a node page asks for: the recipes to show, the ones withheld, the
    machine-class tabs, and the two counts a filtered page has to tell apart --
    how many recipes matched the text search (`total`, what the tabs add up to)
    and how many there were before it (`unfiltered`)."""

    __slots__ = ("choices", "hidden", "groups", "total", "unfiltered")

    def __init__(self, choices, hidden, groups, total, unfiltered=None):
        self.choices = choices
        self.hidden = hidden
        self.groups = groups
        self.total = total
        self.unfiltered = total if unfiltered is None else unfiltered


class RecipeIndex:
    def __init__(self, registry, custom_path=CUSTOM_PATH):
        self.reg = registry
        self.db = registry.db
        self.custom_path = custom_path
        self.custom = {}
        self.custom_by_output = {}
        self._cache = {}
        self.load_custom()

    # -- user-authored recipes -------------------------------------------
    def load_custom(self):
        self.custom = {}
        self.custom_by_output = {}
        if not os.path.exists(self.custom_path):
            return
        with open(self.custom_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        for row in data.get("recipes", []):
            # Mint the blueprint's output before materialising it, so the walk
            # can demand a thing that exists only because this file says so.
            for o in row.get("outputs", []):
                if str(o.get("ix", "")).startswith("u~"):
                    self.reg.register_synthetic(
                        o["ix"], o.get("name") or row.get("name") or o["ix"])
            r = self._materialise_custom(row)
            self.custom[r.rid] = r
            for ix, _q, _c, _k in r.outputs:
                self.custom_by_output.setdefault(ix, []).append(r.rid)

    def _materialise_custom(self, row):
        slots, fslots = [], []
        for n, i in enumerate(row.get("inputs", [])):
            ix = i["ix"]
            qty = int(i.get("qty", 1))
            if ix.startswith("f~"):
                fslots.append(Slot(n, None, [(ix, qty)], is_fluid=True))
            else:
                slots.append(Slot(n, None, [(ix, qty)]))
        outs = [(o["ix"], int(o.get("qty", 1)), 1.0, n)
                for n, o in enumerate(row.get("outputs", []))]
        return Recipe(row["id"], row.get("name") or "hand-entered",
                      None, "custom", False, None,
                      row.get("machine"), row.get("tier"),
                      slots, fslots, outs, custom=True,
                      note=row.get("note", ""))

    def save_custom(self, rows):
        os.makedirs(os.path.dirname(self.custom_path) or ".", exist_ok=True)
        with open(self.custom_path, "w", encoding="utf-8") as fh:
            json.dump({"note": "Hand-entered recipes. A structure is a recipe "
                               "with machine: null -- read the block counts off "
                               "GT's NEI multiblock preview and enter them here.",
                       "recipes": rows}, fh, indent=1)
        self.load_custom()
        self._cache.clear()

    def custom_rows(self):
        if not os.path.exists(self.custom_path):
            return []
        with open(self.custom_path, "r", encoding="utf-8") as fh:
            return json.load(fh).get("recipes", [])

    def add_custom(self, name, machine, inputs, outputs, note="", makes=None):
        """`makes` names a thing the dump does not have, and mints it."""
        rows = self.custom_rows()
        used = {r["id"] for r in rows}
        rid = _unique("u~" + _slug(name or "recipe"), used)
        outputs = list(outputs or [])
        if makes:
            minted = _unique("u~" + _slug(makes),
                             {o.get("ix") for r in rows for o in r.get("outputs", [])})
            outputs.append({"ix": minted, "qty": 1, "name": makes})
        if not outputs:
            return None
        rows.append({"id": rid, "name": name, "machine": machine or None,
                     "inputs": inputs, "outputs": outputs, "note": note})
        self.save_custom(rows)
        return rid

    def delete_custom(self, rid):
        rows = [r for r in self.custom_rows() if r["id"] != rid]
        self.save_custom(rows)

    # -- recipe loading ---------------------------------------------------
    def get(self, rid):
        if rid in self.custom:
            return self.custom[rid]
        hit = self._cache.get(rid)
        if hit is not None:
            return hit
        head = self.db.recipe_head(rid)
        if head is None:
            return None
        _id, rt_id, type_name, category, shapeless = head
        mod, map_, tier = parse_recipe_type_id(rt_id)
        item_slots = []
        for key, gid, tag in self.db.recipe_item_slots(rid):
            members = []
            for iid, size in self.db.group_members(gid):
                if iid is None:
                    continue
                members.append((self.reg.canon(iid), size))
            members = _sorted_members(_dedupe(members), self.reg)
            if members:
                item_slots.append(Slot(key, tag, members))
        fluid_slots = []
        for key, gid in self.db.recipe_fluid_slots(rid):
            members = [(f, a) for f, a in self.db.fluid_group_members(gid) if f]
            if members:
                fluid_slots.append(Slot(key, None, _dedupe(members), is_fluid=True))
        outputs = []
        for key, iid, size, prob in self.db.recipe_item_outputs(rid):
            if iid:
                outputs.append((self.reg.canon(iid), size, prob, key))
        for key, fid, amount, prob in self.db.recipe_fluid_outputs(rid):
            if fid:
                outputs.append((fid, amount, prob, "f%s" % key))
        gt = self.db.gt_recipe_info(rid)
        r = Recipe(rid, type_name, rt_id, category, bool(shapeless), mod, map_,
                   tier, item_slots, fluid_slots, outputs, gt=gt)
        self._cache[rid] = r
        return r

    # -- enumeration ------------------------------------------------------
    def producer_ids(self, ix):
        obj = self.reg.get(ix)
        if obj is None:
            return []
        if obj.kind == "fluid":
            return self.db.producers_of_fluid(ix)
        return self.db.producers_of_item(list(obj.members) or [ix])

    def offer(self, ix, machines=None, only_map=None, contains=None):
        """Everything a node page needs about "what makes this", in one pass.

        Returns `Offer(choices, hidden, groups, total)`.

        Two things here are only worth doing because of what the dump is like.
        Iron Ingot has **422** producers, and materialising one recipe costs a
        head query, a query per input slot, a member query per slot, two output
        queries and a GregTech-info query -- so building all 422 to render the
        two the user asked for was most of the cost of a filtered page.

          * **The machine tabs are counted off the producer rows.**  Those rows
            already carry `RECIPE_TYPE_ID`, and `(map, minTier)` parses straight
            out of it, so the tab list needs no recipe objects at all.
          * **Only the recipes that survive the filter are materialised.**

        The self-containment test stays over the whole set -- it is two bulk
        queries for 422 recipes, and its answer changes the counts.
        """
        obj = self.reg.get(ix)
        if obj is None:
            return Offer([], [], [], 0)
        rows = {}
        for row in self.producer_ids(ix):
            rows.setdefault(row[0], (row[1], row[2]))
        for rid in self.custom_by_output.get(ix, ()):
            rows.setdefault(rid, None)
        if not rows:
            return Offer([], [], [], 0)
        self_containing = self._self_containing(list(rows), obj)
        # The text filter runs *before* the tabs are counted, so "macerator" and
        # "plank" compose in either order and the tab numbers describe what you
        # would actually see if you clicked them.
        by_input = self._with_input([r for r in rows if r not in self_containing],
                                    contains)

        groups, wanted, total = {}, [], 0
        unfiltered = 0
        for rid, head in rows.items():
            if rid in self_containing:
                continue
            unfiltered += 1
            if by_input is not None and rid not in by_input:
                continue
            if head is None:                       # hand-entered
                custom = self.custom.get(rid)
                if custom is None:
                    continue
                key = custom.map or "(placed by hand)"
                label = custom.name
            else:
                rt_id, type_name = head
                _mod, map_, _tier = parse_recipe_type_id(rt_id)
                key = map_ or "(placed by hand)"
                label = type_name
            g = groups.get(key)
            if g is None:
                groups[key] = g = {"map": key, "name": label, "n": 0}
            g["n"] += 1
            total += 1
            if only_map in (None, "") or key == only_map:
                wanted.append(rid)

        out = self._materialise(wanted, ix, machines, False)
        # The withheld ones are few by construction -- a node has at most a
        # handful of recipes that turn its item into itself -- so they are
        # always materialised.
        hidden = self._materialise(self_containing, ix, machines, True)

        return Offer(out, hidden,
                     sorted(groups.values(), key=lambda g: -g["n"]), total,
                     unfiltered)

    def _materialise(self, rids, ix, machines, self_containing):
        """Build the `Choice` for each of `rids`, in display order."""
        out = []
        for rid in rids:
            r = self.get(rid)
            got = r.output_for(ix) if r is not None else None
            if got is None:
                continue
            qty, chance, expected = got
            gated, reason = _gate(r, machines)
            out.append(Choice(r, qty, chance, expected, self_containing, gated,
                              reason))
        out.sort(key=_choice_order)
        return out

    def choices(self, ix, machines=None, include_self_containing=False):
        """Every recipe that outputs `ix`, ready to be offered.

        `machines` is the set the user owns, as {map: max tier index}; a recipe
        needing more is still shown, marked `gated` -- "go build the machine",
        which is a different verdict from `unpriced`, "supply a price".
        """
        got = self.offer(ix, machines=machines)
        if include_self_containing:
            return sorted(got.choices + got.hidden, key=_choice_order)
        return got.choices

    def _with_input(self, rids, text):
        """The subset of `rids` with an input whose name contains `text`.

        Read out of `RECIPE_ITEM_INPUTS_ITEMS`, the flattened recipe x item
        table, and that choice is the whole feature: it holds **every member of
        every set-valued slot**, not the display representative.  A Lathe recipe
        whose wood slot offers 29 planks stores all 29 there, so typing "plank"
        finds it even though the slot shows "Larch Wood Planks (Fireproof)" and
        the word never appears anywhere the user can see.

        Two bulk queries per 400 recipes, against materialising every candidate
        recipe to look at its slots -- which is what the machine filter used to
        do and is why an iron ingot page took half a second.
        """
        want = self.reg.ix_matching(text)
        if want is None:
            return None
        hit, from_db = set(), []
        for rid in rids:
            custom = self.custom.get(rid)
            if custom is None:
                from_db.append(rid)
            elif any(m[0] in want for s in custom.slots for m in s.members):
                hit.add(rid)
        for chunk in _chunks(from_db, 400):
            for rid, iid in self.db.recipe_input_item_ids(chunk):
                if rid not in hit and self.reg.canon(iid) in want:
                    hit.add(rid)
            for rid, fid in self.db.recipe_input_fluid_ids(chunk):
                if rid not in hit and fid in want:
                    hit.add(rid)
        return hit

    def _self_containing(self, rids, obj):
        """Recipes whose inputs already contain the demanded item."""
        bad = set()
        want = set(obj.members) | {obj.ix}
        for chunk in _chunks(rids, 400):
            if obj.kind == "fluid":
                rows = self.db.recipe_input_fluid_ids(chunk)
            else:
                rows = self.db.recipe_input_item_ids(chunk)
            for rid, iid in rows:
                if iid in want or self.reg.canon(iid) == obj.ix:
                    bad.add(rid)
        for rid in rids:
            r = self.custom.get(rid)
            if r and any(m[0] == obj.ix for s in r.slots for m in s.members):
                bad.add(rid)
        return bad


def _gate(recipe, machines):
    """(gated, why).  Machine class only -- never a cost."""
    if machines is None or recipe.machine is None:
        return (False, "")
    have = machines.get(recipe.map)
    if have is None:
        return (True, "needs %s, which you do not have"
                % (recipe.name or recipe.map))
    if recipe.tier and TIER_INDEX.get(recipe.tier, 0) > have:
        return (True, "needs %s, you have up to %s"
                % (recipe.tier, _tier_name(have)))
    return (False, "")


def _tier_name(idx):
    return TIERS[idx] if 0 <= idx < len(TIERS) else "?"


def _choice_order(c):
    r = c.recipe
    return (0 if r.custom else 1,
            1 if c.gated else 0,
            1 if c.chanced else 0,
            r.tier_index,
            len(r.slots),
            r.name or "",
            r.rid)


def _sorted_members(members, reg):
    """A set-valued slot is offered in `sourceRank` order, so the option already
    selected is the one the pack actually hands the player."""
    order = {ix: i for i, ix in enumerate(reg.rank_items([m[0] for m in members]))}
    return sorted(members, key=lambda m: order.get(m[0], 999))


def _dedupe(members):
    seen = {}
    for ix, qty in members:
        if ix in seen:
            seen[ix] = max(seen[ix], qty)
        else:
            seen[ix] = qty
    return list(seen.items())


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-") or "thing"


def _unique(base, used):
    if base not in used:
        return base
    n = 2
    while "%s-%d" % (base, n) in used:
        n += 1
    return "%s-%d" % (base, n)


def _chunks(seq, n):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]
