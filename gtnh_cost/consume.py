"""What does an input stack become after the craft?  §15 "Consumption".

"Is it consumed" is really "what does the input stack become", and there are
four answers, each a different cost formula:

    Consumed     nothing            qty * runs
    Maintained   itself             qty          -- own one, forever
    Worn         itself, damaged    qty * runs / craftsPerTool
    Swapped      a different item   contents consumed, container owned

The transformation is a runtime function -- Forge's `getContainerItem()` for
crafting recipes and GT's own input-consumption logic for machine recipes -- and
neither is in any dump.  So: guess from the dump, let the user correct, and
record provenance on every designation so a re-export refreshes the derived ones
and leaves the corrections alone.

The seeds, and where each comes from:

  zero-size input stack   Maintained   derived, 376 items.  The exporter writes
                                       stack size 0 for an input it saw was not
                                       consumed: programmed circuits, GT moulds,
                                       Blood Magic orbs.  This is a *per recipe
                                       slot* signal and the sharpest one there is.
  gt.integrated_circuit   Maintained   the family rule, for the recipes where the
                                       exporter did write size 1 (25 of them).
  GT metatool families    Worn         crafts = MaxDamage / damagePerCraft, both
                                       per material.
  short hand list         Worn         the IC2 Cutter and friends, from the
                                       asymmetry review queue.
  FLUID_CONTAINER         Swapped      2,085 filled containers -> 17 empties.
  everything else         Consumed     the default, and right for the 525 items
                                       MAX_DAMAGE would have mislabelled.

**MAX_DAMAGE and ITEM_TOOL_CLASSES are deliberately not used as seeds.**  They
answer "how much wear capacity has this", not "does a recipe wear it or eat it".
Every GT metatool has MAX_DAMAGE = 0 and no tool class, and so does the IC2
Cutter, worn across 1,564 cable recipes; meanwhile 525 of the 583 durability
items that are ever a recipe input are armour and fuel rods, which are eaten.

**One deliberate deviation from §15, stated here because it is a judgement
call.**  §15 says a `Swapped` container should default to *owned*.  That is right
once §2's decomposition of a filled container into `fluid x amount + empty
container` exists.  It does not yet, so defaulting to owned would charge nothing
at all for a Lubricant Cell's lubricant -- a silent understatement of exactly the
kind the identity work exists to prevent.  So `Swapped` defaults to the
conservative reading, the filled container consumed, and the cycled reading is
one click away per node.  Revisit when the decomposition lands.
"""
from __future__ import annotations

import json
import os

CONSUMED = "consumed"
MAINTAINED = "maintained"
WORN = "worn"
SWAPPED = "swapped"

DESIGNATIONS = (CONSUMED, MAINTAINED, WORN, SWAPPED)

DESIGNATION_HELP = {
    CONSUMED: "used up by the recipe, every run",
    MAINTAINED: "you need one and keep it: counted once for the whole plan",
    WORN: "loses durability: counted as the fraction of a tool you use up",
    SWAPPED: "the container comes back, but it is still counted as used up",
}

OVERLAY_PATH = os.path.join("data", "consumption_overlay.json")

# From getToolDamagePerContainerCraft(); NOT in the dump.  This is the one
# hand-carried constant in the wear calculation.  Matched against the tool's
# localized name, most specific first.
DAMAGE_PER_CRAFT = (
    ("screwdriver", 400),
    ("hammer", 400),
    ("file", 400),
    ("saw", 200),      # also Chainsaw, Buzzsaw
    ("crowbar", 100),
)
DEFAULT_DAMAGE_PER_CRAFT = 100   # GT's base default; assumed, and flagged as such

# The review queue's output so far: high uses, no producers, confirmed by hand.
HAND_WORN = {
    "IC2:itemToolCutter@0",       # worn across 1,564 cable recipes
    "IC2:itemToolWrench@0",
}


def damage_per_craft(name):
    """(damage per craft, is that rate known rather than assumed).

    Matched on the localized name, which is the *tool*'s name and not the
    material's -- every GT metatool is one item with the material in NBT, so a
    Steel Hammer and an Olenite Hammer are both called "Hammer".  That is what
    lets the same test answer for a bare tool slot and for a chosen variant.
    """
    low = (name or "").lower()
    for needle, dmg in DAMAGE_PER_CRAFT:
        if needle in low:
            return dmg, True
    return DEFAULT_DAMAGE_PER_CRAFT, False


def crafts_for(obj):
    """(crafts per tool, damage per craft, is the rate known), or (None, ...).

    `crafts` is `durability / perCraft` floored -- **but never zero**.  An
    Olenite Hammer has 300 durability against a 400-damage craft, so the floor
    is 0, and `Fraction(0)` is falsy everywhere the wear formula divides by it:
    the tool silently reverted to the "one owned tool, forever" reading, which
    charges a single hammer for a ten-thousand-run plan.  A tool that cannot
    survive one craft is one tool *per craft*, not one tool per plan, and the
    conservative floor is 1.

    Flooring rather than rounding up costs at most one craft of tool life and is
    what §7's `durability/perCraft` says; the numbers that matter are exact
    anyway (Steel Hammer, 51,200 / 400 = 128).
    """
    if obj is None or not getattr(obj, "max_damage", 0):
        return (None, None, False)
    per, known = damage_per_craft(obj.name)
    if not per:
        return (None, None, False)
    return (max(1, obj.max_damage // per), per, known)


class Designation:
    """One answer, with the provenance that lets a re-export refresh it."""

    __slots__ = ("kind", "crafts", "crafts_assumed", "why", "provenance",
                 "swap_to", "per_craft")

    def __init__(self, kind, why, provenance="derived", crafts=None,
                 crafts_assumed=False, swap_to=None, per_craft=None):
        self.kind = kind
        self.why = why
        self.provenance = provenance
        self.crafts = crafts
        self.crafts_assumed = crafts_assumed
        self.swap_to = swap_to
        self.per_craft = per_craft

    @property
    def is_user(self):
        return self.provenance == "user"

    def __repr__(self):
        return "<%s %s>" % (self.kind, self.why)


class ConsumptionOverlay:
    """Default per item, override per (recipe, input).  Both levels are needed:
    a hammer is worn in the 7,684 recipes that use it as a tool and *consumed*
    by the one that melts it down."""

    def __init__(self, registry, path=OVERLAY_PATH):
        self.reg = registry
        self.path = path
        self.by_item = {}          # gid -> kind
        self.by_recipe_input = {}  # "rid|gid" -> kind
        self._load()

    def _load(self):
        if not os.path.exists(self.path):
            return
        with open(self.path, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        self.by_item = d.get("by_item", {})
        self.by_recipe_input = d.get("by_recipe_input", {})

    def save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({
                "note": "User corrections to consumption designations. Derived "
                        "designations are never stored here, so a re-export "
                        "refreshes them and leaves these alone.",
                "by_item": self.by_item,
                "by_recipe_input": self.by_recipe_input,
            }, fh, indent=1, sort_keys=True)

    def set_item(self, gid, kind):
        if kind is None:
            self.by_item.pop(gid, None)
        else:
            self.by_item[gid] = kind
        self.save()

    def set_recipe_input(self, rid, gid, kind):
        key = "%s|%s" % (rid, gid)
        if kind is None:
            self.by_recipe_input.pop(key, None)
        else:
            self.by_recipe_input[key] = kind
        self.save()

    # -- the guess --------------------------------------------------------
    def derived_for_item(self, obj):
        """The item-level default, before any user correction."""
        if obj is None or obj.kind == "fluid":
            return Designation(CONSUMED, "fluid")
        if obj.internal == "gt.integrated_circuit":
            return Designation(MAINTAINED,
                               "a programmed circuit is set to a number, not "
                               "used up")
        if obj.tool_material and obj.max_damage:
            crafts, per, known = crafts_for(obj)
            return Designation(WORN, "%s tool: %d durability, %d per craft"
                               % (obj.tool_material, obj.max_damage, per),
                               crafts=crafts, crafts_assumed=not known,
                               per_craft=per)
        if obj.ix in self.reg.tool_variants:
            # The rate is a property of the tool *class*, so it is known here
            # even though the durability is not -- a Hammer is 400 per craft
            # whatever it is made of.  Saying otherwise made the plan warn that
            # "100 is assumed" about a hammer it had correctly charged at 400.
            per, known = damage_per_craft(obj.name)
            return Designation(WORN, "tool slot: pick a material to see how "
                                     "much of one you use up", crafts=None,
                               crafts_assumed=not known, per_craft=per)
        if obj.gid in HAND_WORN:
            per, known = damage_per_craft(obj.name)
            return Designation(WORN, "known to be used as a tool, not an "
                                     "ingredient", crafts=None,
                               crafts_assumed=not known, per_craft=per)
        hit = self.reg.containers.get(obj.ix)
        if hit:
            empty, _fluid, amount = hit
            back = self.reg.get(empty) if empty else None
            return Designation(SWAPPED,
                               "a filled container: %d mB, and you get %s back"
                               % (amount, back.name if back else
                                  "an empty container"),
                               swap_to=empty)
        return Designation(CONSUMED, "consumed")

    def for_input(self, rid, obj, slot_stack_size=None):
        """The designation actually used for this input of this recipe.

        Order: user (recipe, input) > user item > derived per-slot > derived item.
        """
        if obj is None:
            return Designation(CONSUMED, "unknown item")
        key = "%s|%s" % (rid, obj.gid)
        forced = self.by_recipe_input.get(key)
        if forced:
            return self._user(forced, obj, "corrected for this recipe")
        forced = self.by_item.get(obj.gid)
        if forced:
            return self._user(forced, obj, "corrected for this item")
        derived = self.derived_for_item(obj)
        if slot_stack_size == 0 and derived.kind == CONSUMED:
            # The exporter writes stack size 0 for an input it saw was not
            # consumed.  It is the sharpest signal there is, but it says less
            # than a family rule does, so it only speaks where nothing else did.
            return Designation(MAINTAINED,
                               "the recipe needs this in a slot but does not "
                               "use it up")
        return derived

    def _user(self, kind, obj, why):
        crafts = per = None
        assumed = True
        if kind == WORN:
            if obj.tool_material and obj.max_damage:
                crafts, per, known = crafts_for(obj)
            else:
                # A bare tool slot: no durability yet, but the class still fixes
                # the rate, so a user-corrected Hammer does not claim 100.
                per, known = damage_per_craft(obj.name)
            assumed = not known
        return Designation(kind, why, provenance="user", crafts=crafts,
                           crafts_assumed=assumed, per_craft=per)
