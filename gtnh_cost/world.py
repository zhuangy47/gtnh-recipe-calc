"""The model, assembled.  This is the only object the web layer needs.

Nothing below `gtnh_cost` imports a web framework; `app/` is a thin view over
this.  That is also what lets auto mode -- the LP, which is explicitly not what
is being built here -- attach later without touching the UI.
"""
from __future__ import annotations

import math
import re
from fractions import Fraction

from .classify import ClassificationOverlay, Rank
from .consume import ConsumptionOverlay
from .db import TIER_INDEX, Db, parse_recipe_type_id
from .icons import IconStore
from .identity import Registry
from .order import build_order
from .plan import STOP, USE_PLAN, USE_RECIPE, Plan, Solver, ingredients_of, resolve_slot
from .recipes import RecipeIndex
from .store import PlanStore


class World:
    def __init__(self, db_path=None, log=print):
        self.db = Db(db_path) if db_path else Db()
        self.rank = Rank()
        self.classification = ClassificationOverlay()
        self.registry = Registry(self.db, self.rank, self.classification, log=log)
        self.index = RecipeIndex(self.registry)
        self.consumption = ConsumptionOverlay(self.registry)
        self.store = PlanStore()
        self.icons = IconStore()
        # Open the icon archive here rather than on the first request that wants
        # a picture: it is a 0.6 s central-directory parse, and the loader is
        # already being waited on.  See `icons.IconStore` for why that matters
        # so much more than it looks like it should.
        self.icons.warm(log=log)
        self.solver = Solver(self.registry, self.index, self.consumption,
                             self.store)
        self.version = self.db.version()

    # -- convenience the view layer wants --------------------------------
    def get(self, ix):
        return self.registry.get(ix)

    def name(self, ix):
        obj = self.registry.get(ix)
        return obj.name if obj is not None else ix

    def unit(self, ix):
        """Fluids are counted in millibuckets; 144 mB is one ingot.  Showing a
        bare number for a fluid reads as "1,728 glue" when it means 12 ingots'
        worth, so the unit is never dropped."""
        obj = self.registry.get(ix)
        return "mB" if obj is not None and obj.kind == "fluid" else ""

    def stack_of(self, ix):
        """How many of this item go in one inventory slot.

        1 means "does not stack", and it is also the answer for a fluid and for
        an item this registry has never heard of -- in all three cases there is
        no stack to count, so the caller shows the plain number.
        """
        obj = self.registry.get(ix)
        return getattr(obj, "stack", 1) or 1

    def solve(self, plan):
        return self.solver.solve(plan)

    def order(self, plan, sol=None):
        """What to do, in what order, and how far through it you are.

        `sol` is optional so a route that has already solved does not solve
        twice: `build_order` only reads the solution, and the progress record
        is the plan's.
        """
        return build_order(sol if sol is not None else self.solve(plan),
                           plan.progress, self.name)

    def offer(self, plan, ix, only_map=None, contains=None):
        """The choice list, the withheld list and the machine tabs, in one pass.

        The node page used to ask for the offered recipes and the withheld ones
        separately, which enumerated every producer twice; and it filtered by
        machine class only after building all 422 recipe objects for an iron
        ingot.  `RecipeIndex.offer` does both jobs once.

        `contains` narrows by *input*: "macerator" and "plank" compose, and the
        text reaches inside a set-valued slot, which is where most of a GTNH
        recipe's inputs actually live.
        """
        return self.index.offer(ix, machines=plan.machines if plan else None,
                                only_map=only_map, contains=contains)

    def choices(self, plan, ix):
        return self.index.choices(ix, machines=plan.machines if plan else None)

    def hidden_choices(self, plan, ix):
        """The self-containing recipes, kept out of the offer list.

        1,136 recipes in the dump output an item that is also a member of one of
        their own input groups -- backpack dyeing, rune cycling, tool repair,
        tier-unification presses.  They are real recipes; they are just never a
        way to *make* the thing, so they are shown separately and cannot be
        chosen without seeing why.
        """
        every = self.index.choices(ix, machines=plan.machines if plan else None,
                                   include_self_containing=True)
        return [c for c in every if c.self_containing]

    def ingredients(self, recipe, picks):
        return ingredients_of(recipe, picks)

    def resolve_slot(self, slot, picks):
        return resolve_slot(slot, picks)

    def designation(self, rid, ix, zero_stack=False, qty=1):
        return self.consumption.for_input(rid, self.registry.get(ix),
                                          0 if zero_stack else qty)

    def machine_maps(self):
        """Every (map, tier) the dump knows, for the machine filter UI."""
        rows = self.db.q(
            "SELECT ID, TYPE, (SELECT COUNT(*) FROM RECIPE r WHERE "
            "r.RECIPE_TYPE_ID = RECIPE_TYPE.ID) FROM RECIPE_TYPE")
        maps = {}
        for rt_id, type_name, n in rows:
            _mod, map_, tier = parse_recipe_type_id(rt_id)
            m = maps.get(map_)
            if m is None:
                maps[map_] = m = {"map": map_, "name": type_name, "tiers": set(),
                                  "recipes": 0}
            if tier:
                m["tiers"].add(tier)
            m["recipes"] += n or 0
            if not tier:
                m["name"] = type_name
        for m in maps.values():
            m["tiers"] = sorted(m["tiers"], key=lambda t: TIER_INDEX.get(t, 99))
            if m["tiers"]:
                m["name"] = m["name"].rsplit(" (", 1)[0]
        return sorted(maps.values(), key=lambda m: -m["recipes"])

    def ore_seeded_cut(self, ix):
        return ix in self.registry.ore_cut

    def tool_variants(self, ix):
        """The material-specific rows behind a generic tool slot, deduped."""
        out = []
        seen = set()
        for vix in self.registry.tool_variants.get(ix, ()):
            v = self.registry.get(vix)
            if v is None or not v.tool_material:
                continue
            key = (v.tool_material, v.max_damage)
            if key in seen:
                continue
            seen.add(key)
            out.append(v)
        out.sort(key=lambda v: (v.max_damage, v.tool_material))
        return out

    # -- material re-pick: `sweep` in miniature --------------------------
    # "A tank's walls, gauges and valves all switch together, and doing it
    # node by node is the tedious part."  Building the manual version first is
    # how the automated sweep gets a spec.  What it can and cannot do is honest
    # rather than clever: an item is swapped when the same *form* exists in the
    # target material; a node whose item changed loses its recipe choice,
    # because a different item is genuinely made by a different recipe.

    def swap_item(self, ix, dst_material):
        """The same form in another material, or None.

        Routed through the (form, material) index rather than through the
        oredict, because every Railcraft tank block and every GT casing carries
        no tag at all -- and those are exactly the items the iron-versus-steel
        question is asked about.  The oredict is the fallback, not the path.
        """
        obj = self.registry.get(ix)
        if obj is None or not obj.form or not obj.material:
            return None
        hit = self.registry.label_index.get((obj.form, dst_material))
        if hit:
            return hit[0]
        got = self.registry.by_form(obj.form + dst_material)
        if got and got[1]:
            return got[1]
        return None

    def swap_targets(self, plan):
        """Materials every swappable form in this plan is available in."""
        forms = set()
        for node in plan.nodes.values():
            o = self.registry.get(node.ix)
            if o is not None and o.form and o.material:
                forms.add(o.form)
        if not forms:
            return []
        counts = {}
        for form, material in self.registry.label_index:
            if form in forms:
                counts[material] = counts.get(material, 0) + 1
        return sorted(m for m, c in counts.items() if c >= len(forms))

    def clone_custom_in_material(self, rid, dst_material, src_material=None):
        """A hand-entered blueprint, re-materialised.

        Returns (new rid, error, output map).  The output map matters: a
        blueprint's minted output is its own invention, so the clone gets a new
        one, and anything demanding the old thing has to be pointed at the new
        thing or it will ask an iron recipe for a steel tank.
        """
        rows = self.index.custom_rows()
        src = next((r for r in rows if r["id"] == rid), None)
        if src is None:
            return (None, "no such recipe", {})
        if not dst_material:
            return (None, "pick a material", {})
        missing = []

        def swap(entries):
            out = []
            for e in entries:
                new = self.swap_item(e["ix"], dst_material)
                if new is None:
                    obj = self.registry.get(e["ix"])
                    if obj is not None and obj.form and obj.material:
                        missing.append(obj.name)
                    out.append(dict(e))
                else:
                    out.append({"ix": new, "qty": e.get("qty", 1)})
            return out

        inputs = swap(src.get("inputs", []))
        outputs = swap(src.get("outputs", []))
        if missing:
            return (None, "%s has no %s version" % (missing[0], dst_material), {})
        name = "%s (%s)" % (_strip_material(src.get("name", "recipe")),
                            dst_material)
        # A minted output is this blueprint's own invention, so the clone needs
        # its own; sharing one would make the steel tank and the iron tank the
        # same thing, which is exactly the comparison being asked for.
        makes = None
        minted_from = None
        keep = []
        for o in outputs:
            if str(o.get("ix", "")).startswith("u~"):
                minted_from = o["ix"]
                makes = _rename_material(o.get("name") or name, src_material,
                                         dst_material)
            else:
                keep.append(o)
        new_rid = self.index.add_custom(name, src.get("machine"), inputs,
                                        keep, src.get("note", ""), makes=makes)
        if new_rid is None:
            return (None, "that recipe makes nothing", {})
        out_map = {}
        if minted_from:
            new_row = next(r for r in self.index.custom_rows()
                           if r["id"] == new_rid)
            for o in new_row.get("outputs", []):
                if str(o.get("ix", "")).startswith("u~"):
                    out_map[minted_from] = o["ix"]
        return (new_rid, None, out_map)

    def swap_material(self, plan, src_material, dst_material, new_name):
        """Duplicate a plan with one material swapped throughout.

        Fix a configuration, vary one parameter, compare -- the query this model
        exists to answer.  Decisions survive wherever the item did not change;
        where it did, the node comes back Open, because "the same recipe in
        steel" is not a thing the dump has.  Returns (plan, error).
        """
        if not src_material or not dst_material:
            return (None, "pick both materials")
        clone_of, out_map = {}, {}
        for row in self.index.custom_rows():
            if any(self._is_material(e["ix"], src_material)
                   for e in list(row.get("inputs", [])) + list(row.get("outputs", []))):
                new_rid, err, omap = self.clone_custom_in_material(
                    row["id"], dst_material, src_material)
                if err:
                    return (None, err)
                clone_of[row["id"]] = new_rid
                out_map.update(omap)
        new = Plan(name=new_name or (plan.name + " " + dst_material))
        new.machines = plan.machines
        new.note = plan.note
        # The point of a swap is that only the material changes, so the
        # item-level decisions come with it.  A `final` follows its item to the
        # swapped one; tool materials do not move at all, because a steel tank
        # is still built with whatever hammer you own.
        new.tools = dict(plan.tools)
        for ix in plan.finals:
            if self._is_material(ix, src_material):
                ix = self.swap_item(ix, dst_material) or ix
            new.finals.add(ix)
        changed = 0

        def copy(src_node, parent_id):
            nonlocal changed
            ix = src_node.ix
            if self._is_material(ix, src_material):
                swapped = self.swap_item(ix, dst_material)
                if swapped:
                    ix = swapped
                    changed += 1
            elif ix in out_map:
                # a minted blueprint output: follow it to the clone's own
                ix = out_map[ix]
                changed += 1
            node = new.new_node(ix, parent_id)
            if ix != src_node.ix and ix not in out_map.values():
                return node          # different item -> re-pick it
            choice = src_node.choice
            kind = choice.get("kind")
            if kind == STOP:
                node.choice = {"kind": STOP}
            elif kind == USE_PLAN:
                node.choice = dict(choice)
            elif kind == USE_RECIPE:
                rid = choice.get("recipe")
                rid = clone_of.get(rid, rid)
                recipe = self.index.get(rid)
                if recipe is None:
                    return node
                picks = {k: (self.swap_item(v, dst_material) or v)
                         if self._is_material(v, src_material) else v
                         for k, v in (choice.get("picks") or {}).items()}
                ings = ingredients_of(recipe, picks)
                children = {}
                old_children = choice.get("children") or {}
                for ing in ings:
                    match = None
                    for old_ix, cid in old_children.items():
                        cand = old_ix
                        if self._is_material(old_ix, src_material):
                            cand = self.swap_item(old_ix, dst_material) or old_ix
                        if cand == ing.ix:
                            match = plan.nodes.get(cid)
                            break
                    if match is not None:
                        children[ing.ix] = copy(match, node.id).id
                    else:
                        children[ing.ix] = new.new_node(ing.ix, node.id).id
                node.choice = {"kind": USE_RECIPE, "recipe": rid, "picks": picks,
                               "children": children,
                               "tools": dict(choice.get("tools") or {}),
                               "sig": recipe.signature()}
            return node

        for r in plan.roots:
            src_node = plan.nodes.get(r["node"])
            if src_node is None:
                continue
            n = copy(src_node, None)
            new.roots.append({"node": n.id, "qty": r["qty"]})
        if not changed:
            return (None, "nothing in this plan is made of %s" % src_material)
        self.store.save(new)
        return (new, None)

    def _is_material(self, ix, material):
        o = self.registry.get(ix)
        return o is not None and o.material == material

    def material_siblings(self, ix, limit=400):
        """Every item sharing this one's form, keyed by material.

        This is the manual half of `sweep`: re-picking a route in steel instead
        of iron is the tank question, and building it by hand first is how the
        automated version gets a spec.
        """
        obj = self.registry.get(ix)
        if obj is None or not obj.form:
            return []
        out = []
        for tag, members in self.registry.tag_members.items():
            sp = self.rank.split(tag)
            if not sp or sp[0] != obj.form:
                continue
            default = members[0] if members else None
            if default is None or default == ix:
                continue
            other = self.registry.get(default)
            if other is None or other.material is None:
                continue
            out.append(other)
            if len(out) >= limit:
                break
        out.sort(key=lambda o: (o.material or ""))
        return out


def _rename_material(name, src_material, dst_material):
    """"Railcraft Iron Tank 5x5x5" -> "Railcraft Steel Tank 5x5x5", when the
    material really is a word in the name.  Otherwise the material is appended,
    because guessing harder would be guessing."""
    if not src_material:
        return "%s (%s)" % (_strip_material(name), dst_material)
    word = r"\b" + re.escape(src_material) + r"\b"
    if re.search(word, name):
        return re.sub(word, dst_material, name)
    return "%s (%s)" % (_strip_material(name), dst_material)


def _strip_material(name):
    """"Railcraft Iron Tank (5x5x5)" -> "Railcraft Tank (5x5x5)" is not safely
    derivable, so the material is simply appended and any previous parenthetical
    material suffix removed."""
    return re.sub(r"\s*\([A-Z][A-Za-z0-9 -]*\)\s*$", "", name).strip() or name


def fmt_qty(x, places=3):
    """Quantities are exact Fractions; humans read decimals.

    Fractional rows are real and wanted -- 1.16 steel hammers is the honest
    answer for 148 hammer-crafts -- so they are shown, not rounded away, with the
    exact fraction available when it is not a terminating decimal.
    """
    if x is None:
        return ""
    f = x if isinstance(x, Fraction) else Fraction(x)
    if f.denominator == 1:
        return "{:,}".format(f.numerator)
    val = float(f)
    s = ("%." + str(places) + "f") % val
    s = s.rstrip("0").rstrip(".")
    return s


def fmt_stacks(x, stack, places=3):
    """The same quantity read off a chest instead of a spreadsheet.

    4,096 iron plates is 64 stacks; 150 is "2 stacks + 22".  Below one stack
    there is nothing to say, so the plain number is shown -- and an item that
    does not stack has no stacks to count, so it is always the plain number.
    The remainder keeps whatever fraction the quantity had: a share of a batch
    stays a share of a batch.
    """
    if x is None:
        return ""
    f = x if isinstance(x, Fraction) else Fraction(x)
    if not stack or stack <= 1 or f < stack:
        return fmt_qty(f, places)
    n = int(f // stack)
    rest = f - n * stack
    out = "{:,} {}".format(n, "stack" if n == 1 else "stacks")
    if rest:
        out += " + " + fmt_qty(rest, places)
    return out


def fmt_exact(x):
    f = x if isinstance(x, Fraction) else Fraction(x)
    if f.denominator == 1:
        return None
    return "%d/%d" % (f.numerator, f.denominator)


def ceil_qty(x):
    f = x if isinstance(x, Fraction) else Fraction(x)
    return int(math.ceil(f))
