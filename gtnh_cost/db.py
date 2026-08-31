"""Read-only access to the NESQL dump.

Every query here is one of the six names in `data/NESQL-NOTES.md`, or a direct
descendant of one.  The two traps that note records are honoured:

  * oredict membership is read through ORE_DICTIONARY, never through ITEM_GROUP
    alone -- ITEM_GROUP backs recipe slots and quest lists too;
  * SQLite `LIKE` is case-insensitive, so case-sensitive matching uses GLOB.
"""
from __future__ import annotations

import os
import sqlite3
import threading

DEFAULT_PATH = os.path.join("data", "nesql.sqlite")

# GregTech voltage tiers, in order.  `rt~<mod>~<map>~<tier>` ends with one of
# these when the recipe map is tiered; `rt~minecraft~crafting~shaped` does not.
TIERS = ["ULV", "LV", "MV", "HV", "EV", "IV", "LuV", "ZPM", "UV", "UHV", "UEV",
         "UIV", "UMV", "UXV", "MAX"]
TIER_INDEX = {t: i for i, t in enumerate(TIERS)}


def parse_recipe_type_id(rt_id):
    """`rt~gregtech~gt.recipe.centrifuge~LV` -> ('gregtech', 'gt.recipe.centrifuge', 'LV').

    The tier is the recipe's *minimum* voltage and each recipe is listed once,
    so (map, minTier) is the whole of "which machine can run this".
    """
    parts = rt_id.split("~")
    if len(parts) < 3 or parts[0] != "rt":
        return ("?", rt_id, None)
    mod = parts[1]
    rest = parts[2:]
    if len(rest) > 1 and rest[-1] in TIER_INDEX:
        return (mod, "~".join(rest[:-1]), rest[-1])
    return (mod, "~".join(rest), None)


class Db:
    """A thread-local read-only connection over one sqlite file."""

    def __init__(self, path=DEFAULT_PATH):
        self.path = path
        self._local = threading.local()
        if not os.path.exists(path):
            raise FileNotFoundError(
                path + " not found. Build it with:\n"
                "  python tools/nesql_to_sqlite.py data/nesql-db.script " + path)

    @property
    def conn(self):
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect("file:%s?mode=ro" % self.path, uri=True,
                                check_same_thread=False)
            c.execute("PRAGMA temp_store = MEMORY")
            self._local.conn = c
        return c

    def q(self, sql, params=()):
        return self.conn.execute(sql, params).fetchall()

    def one(self, sql, params=()):
        return self.conn.execute(sql, params).fetchone()

    # -- metadata ---------------------------------------------------------
    def version(self):
        row = self.one("SELECT VERSION FROM METADATA")
        return row[0] if row else "unknown"

    def stamp(self):
        st = os.stat(self.path)
        return "%s|%d|%d" % (self.version(), st.st_size, int(st.st_mtime))

    # -- bulk reads, used only by the cache build -------------------------
    def all_items(self):
        return self.conn.execute(
            "SELECT ID, MOD_ID, INTERNAL_NAME, ITEM_DAMAGE, LOCALIZED_NAME, "
            "       NBT, IMAGE_FILE_PATH, MAX_STACK_SIZE FROM ITEM")

    def all_fluids(self):
        return self.conn.execute(
            "SELECT ID, MOD_ID, INTERNAL_NAME, LOCALIZED_NAME, IMAGE_FILE_PATH, "
            "       GASEOUS FROM FLUID")

    def all_tag_names(self):
        return self.conn.execute("SELECT NAME FROM ORE_DICTIONARY")

    def all_tag_members(self):
        """The correct oredict join -- through ORE_DICTIONARY (NESQL-NOTES 2)."""
        return self.conn.execute(
            "SELECT o.NAME, s.ITEM_STACKS_ITEM_ID "
            "FROM ORE_DICTIONARY o "
            "JOIN ITEM_GROUP_ITEM_STACKS s ON s.ITEM_GROUP_ID = o.ITEM_GROUP_ID "
            "WHERE s.ITEM_STACKS_ITEM_ID IS NOT NULL")

    def item_output_counts(self):
        return self.conn.execute(
            "SELECT ITEM_OUTPUTS_VALUE_ITEM_ID, COUNT(*) FROM RECIPE_ITEM_OUTPUTS "
            "GROUP BY ITEM_OUTPUTS_VALUE_ITEM_ID")

    def all_fluid_containers(self):
        return self.conn.execute(
            "SELECT CONTAINER_ID, EMPTY_CONTAINER_ID, FLUID_STACK_FLUID_ID, "
            "       FLUID_STACK_AMOUNT FROM FLUID_CONTAINER "
            "WHERE CONTAINER_ID IS NOT NULL")

    # -- the walk ---------------------------------------------------------
    def producers_of_item(self, db_ids):
        """Which recipes produce this item -- NESQL-NOTES 3, the backward walk."""
        marks = ",".join(["?"] * len(db_ids))
        return self.q(
            "SELECT o.RECIPE_ID, r.RECIPE_TYPE_ID, rt.TYPE, "
            "       o.ITEM_OUTPUTS_VALUE_STACK_SIZE, o.ITEM_OUTPUTS_VALUE_PROBABILITY, "
            "       o.ITEM_OUTPUTS_KEY, o.ITEM_OUTPUTS_VALUE_ITEM_ID "
            "FROM RECIPE_ITEM_OUTPUTS o "
            "JOIN RECIPE r ON r.ID = o.RECIPE_ID "
            "JOIN RECIPE_TYPE rt ON rt.ID = r.RECIPE_TYPE_ID "
            "WHERE o.ITEM_OUTPUTS_VALUE_ITEM_ID IN (" + marks + ")", db_ids)

    def producers_of_fluid(self, fluid_id):
        return self.q(
            "SELECT o.RECIPE_ID, r.RECIPE_TYPE_ID, rt.TYPE, "
            "       o.FLUID_OUTPUTS_VALUE_AMOUNT, o.FLUID_OUTPUTS_VALUE_PROBABILITY, "
            "       o.FLUID_OUTPUTS_KEY, o.FLUID_OUTPUTS_VALUE_FLUID_ID "
            "FROM RECIPE_FLUID_OUTPUTS o "
            "JOIN RECIPE r ON r.ID = o.RECIPE_ID "
            "JOIN RECIPE_TYPE rt ON rt.ID = r.RECIPE_TYPE_ID "
            "WHERE o.FLUID_OUTPUTS_VALUE_FLUID_ID = ?", (fluid_id,))

    def recipe_head(self, rid):
        return self.one(
            "SELECT r.ID, r.RECIPE_TYPE_ID, rt.TYPE, rt.CATEGORY, rt.SHAPELESS "
            "FROM RECIPE r JOIN RECIPE_TYPE rt ON rt.ID = r.RECIPE_TYPE_ID "
            "WHERE r.ID = ?", (rid,))

    def recipe_item_slots(self, rid):
        """Slot structure, plus the oredict name of the slot when it has one.

        A recipe input group carries its own stacks, at the recipe's stack
        sizes, and points at a BASE_ITEM_GROUP_ID holding the unit-size version.
        When that base group is an oredict group its tag name is the honest
        label for a set-valued slot.
        """
        return self.q(
            "SELECT rig.ITEM_INPUTS_KEY, rig.ITEM_INPUTS_ID, "
            "       (SELECT o.NAME FROM ORE_DICTIONARY o "
            "        WHERE o.ITEM_GROUP_ID = g.BASE_ITEM_GROUP_ID) "
            "FROM RECIPE_ITEM_GROUP rig "
            "JOIN ITEM_GROUP g ON g.ID = rig.ITEM_INPUTS_ID "
            "WHERE rig.RECIPE_ID = ? ORDER BY rig.ITEM_INPUTS_KEY", (rid,))

    def group_members(self, group_id):
        return self.q(
            "SELECT s.ITEM_STACKS_ITEM_ID, s.ITEM_STACKS_STACK_SIZE "
            "FROM ITEM_GROUP_ITEM_STACKS s WHERE s.ITEM_GROUP_ID = ?", (group_id,))

    def recipe_fluid_slots(self, rid):
        return self.q(
            "SELECT rfg.FLUID_INPUTS_KEY, rfg.FLUID_INPUTS_ID "
            "FROM RECIPE_FLUID_GROUP rfg WHERE rfg.RECIPE_ID = ? "
            "ORDER BY rfg.FLUID_INPUTS_KEY", (rid,))

    def fluid_group_members(self, group_id):
        return self.q(
            "SELECT FLUID_STACKS_FLUID_ID, FLUID_STACKS_AMOUNT "
            "FROM FLUID_GROUP_FLUID_STACKS WHERE FLUID_GROUP_ID = ?", (group_id,))

    def recipe_item_outputs(self, rid):
        return self.q(
            "SELECT ITEM_OUTPUTS_KEY, ITEM_OUTPUTS_VALUE_ITEM_ID, "
            "       ITEM_OUTPUTS_VALUE_STACK_SIZE, ITEM_OUTPUTS_VALUE_PROBABILITY "
            "FROM RECIPE_ITEM_OUTPUTS WHERE RECIPE_ID = ? ORDER BY ITEM_OUTPUTS_KEY",
            (rid,))

    def recipe_fluid_outputs(self, rid):
        return self.q(
            "SELECT FLUID_OUTPUTS_KEY, FLUID_OUTPUTS_VALUE_FLUID_ID, "
            "       FLUID_OUTPUTS_VALUE_AMOUNT, FLUID_OUTPUTS_VALUE_PROBABILITY "
            "FROM RECIPE_FLUID_OUTPUTS WHERE RECIPE_ID = ? ORDER BY FLUID_OUTPUTS_KEY",
            (rid,))

    def gt_recipe_info(self, rid):
        return self.one(
            "SELECT VOLTAGE, VOLTAGE_TIER, DURATION, AMPERAGE, REQUIRES_CLEANROOM, "
            "       REQUIRES_LOW_GRAVITY, ADDITIONAL_INFO "
            "FROM GREG_TECH_RECIPE WHERE RECIPE_ID = ?", (rid,))

    def recipe_input_item_ids(self, rids):
        """Flattened recipe x item -- used only for the self-containment test."""
        marks = ",".join(["?"] * len(rids))
        return self.q(
            "SELECT RECIPE_ID, ITEM_INPUTS_ITEMS_ID FROM RECIPE_ITEM_INPUTS_ITEMS "
            "WHERE RECIPE_ID IN (" + marks + ")", rids)

    def recipe_input_fluid_ids(self, rids):
        marks = ",".join(["?"] * len(rids))
        return self.q(
            "SELECT RECIPE_ID, FLUID_INPUTS_FLUIDS_ID FROM RECIPE_FLUID_INPUTS_FLUIDS "
            "WHERE RECIPE_ID IN (" + marks + ")", rids)
