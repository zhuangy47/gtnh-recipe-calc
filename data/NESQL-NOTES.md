# `nesql.sqlite` — schema notes and query cookbook

Everything here was verified against the real database on 2026-08-27. It exists so
the next session does not re-derive it. Rebuild the db with
`python tools/nesql_to_sqlite.py data/nesql-db.script data/nesql.sqlite`.

**506 MB · 3,011,184 rows · 45 tables · ITEM 109,601 · RECIPE 219,334 ·
ORE_DICTIONARY 121,352 · FLUID 1,542**

---

## 1. ID formats

Every primary key is a readable prefixed string. Parse them rather than joining
when you only need the parts.

| prefix | shape | example |
|---|---|---|
| item | `i~<mod>~<internalName>~<damage>[~<sha1(nbt)>]` | `i~gregtech~gt.blockmachines~1120` |
| item + NBT | | `i~Railcraft~machine.eta~0~21o21S59OSmCVp3eI-l2xg==` |
| fluid | `f~<mod>~<internalName>` | `f~AWWayofTime~lifeessence` |
| recipe | `r~<hash>` | `r~YmxYgeX2OVix19iz34H-vg==` |
| recipe type | `rt~<mod>~<mapName>~<tier>` | `rt~gregtech~gt.recipe.centrifuge~LV` |
| item group | `ig~<hash>` | `ig~dCrbi_bjNLiCH2Sx4maC1Q==` |

**`rt~` is load-bearing.** The map name is the machine *class* and the tier is the
recipe's **minimum** voltage. A recipe is listed **once**, not duplicated per tier —
Centrifuge has 923 recipes at ULV, 270 at LV, 65 at MV. So "which machines can run
this" is `map + minTier ≤ the tier the user owns`, with no extra data.

**Recipe ids look content-derived.** A changed recipe therefore yields a new id and
any saved plan referencing it dangles. Detect and report that per node — see §15
"Plans have to survive a pack update".

---

## 2. The trap: `ITEM_GROUP` serves two masters

`ITEM_GROUP` is a generic set-of-itemstacks. It is the backing store for **oredict
membership** *and* for **recipe input slots**, and also for quest/reward item lists.

> An item being in 365 item-groups does **not** mean it carries 365 oredict tags.
> Always route through `ORE_DICTIONARY` when you mean oredict.

This bit once during this project: a query joining `ITEM → ITEM_GROUP_ITEM_STACKS`
reported GT casings as carrying hundreds of tags. They carry **zero**. The corrected
query joins `ORE_DICTIONARY` on `ITEM_GROUP_ID`.

```sql
-- oredict tags on an item (correct)
SELECT o.NAME FROM ORE_DICTIONARY o
JOIN ITEM_GROUP_ITEM_STACKS s ON s.ITEM_GROUP_ID = o.ITEM_GROUP_ID
WHERE s.ITEM_STACKS_ITEM_ID = :item_id;
```

Two input tables, for different jobs:

- `RECIPE_ITEM_GROUP(RECIPE_ID, ITEM_INPUTS_ID, ITEM_INPUTS_KEY)` — the **slot
  structure**. `ITEM_INPUTS_KEY` is the grid slot index. Use this to render a recipe
  and to let the user resolve set-valued slots.
- `RECIPE_ITEM_INPUTS_ITEMS(RECIPE_ID, ITEM_INPUTS_ITEMS_ID)` — flattened
  recipe×item. Use this only for "is this item used anywhere", never for quantities.

Fluids mirror items exactly: `RECIPE_FLUID_GROUP` → `FLUID_GROUP` →
`FLUID_GROUP_FLUID_STACKS`, and `RECIPE_FLUID_OUTPUTS`.

---

## 3. The queries the MVP is built from

### Resolve an item

```sql
SELECT ID, MOD_ID || ':' || INTERNAL_NAME || '@' || ITEM_DAMAGE AS gid, LOCALIZED_NAME
FROM ITEM WHERE MOD_ID = ? AND INTERNAL_NAME = ? AND ITEM_DAMAGE = ?;
```

**`LOCALIZED_NAME` is not unique** — two distinct items are both "Integrated Logic
Circuit". Never key on it; always show `gid` in the UI.

### Which recipes produce this item (the backward walk)

```sql
SELECT r.ID, rt.TYPE, rt.ID AS type_id, o.ITEM_OUTPUTS_VALUE_STACK_SIZE AS yield,
       o.ITEM_OUTPUTS_VALUE_PROBABILITY AS chance
FROM RECIPE_ITEM_OUTPUTS o
JOIN RECIPE r      ON r.ID  = o.RECIPE_ID
JOIN RECIPE_TYPE rt ON rt.ID = r.RECIPE_TYPE_ID
WHERE o.ITEM_OUTPUTS_VALUE_ITEM_ID = :item_id;
```

Covered by the added index `NESQL_IDX_OUT_ITEM`; without it this is a full scan.

### A recipe's input slots, with each slot's substitutes

```sql
SELECT rig.ITEM_INPUTS_KEY AS slot, s.ITEM_STACKS_ITEM_ID, s.ITEM_STACKS_STACK_SIZE
FROM RECIPE_ITEM_GROUP rig
LEFT JOIN ITEM_GROUP_ITEM_STACKS s ON s.ITEM_GROUP_ID = rig.ITEM_INPUTS_ID
WHERE rig.RECIPE_ID = :recipe_id ORDER BY slot;
```

**Slots are not ingredients.** The Basic Steam Turbine is 9 slots → 6 ingredients:
two slots of Bronze Fluid Pipe, two of Tin Rotor, two of Electric Motor (LV), plus a
circuit slot with 4 substitutes, an LV Machine Hull and a Tin Cable. Aggregate to
ingredients only **after** the user resolves each set-valued slot.

### Machine class and minimum tier

```sql
-- rt~<mod>~<map>~<tier>
SELECT rt.ID, rt.TYPE FROM RECIPE r JOIN RECIPE_TYPE rt ON rt.ID = r.RECIPE_TYPE_ID
WHERE r.ID = :recipe_id;
```

### Oredict tag → members

```sql
SELECT i.* FROM ORE_DICTIONARY o
JOIN ITEM_GROUP_ITEM_STACKS s ON s.ITEM_GROUP_ID = o.ITEM_GROUP_ID
JOIN ITEM i ON i.ID = s.ITEM_STACKS_ITEM_ID
WHERE o.NAME = :tag;
```

### Tool wear, derived

GT tools have `MAX_DAMAGE = 0` and no tool class; wear lives in NBT.

```sql
SELECT CAST(replace(substr(NBT, instr(NBT,'MaxDamage:') + 10, 14), 'L', '') AS INTEGER)
FROM ITEM WHERE INTERNAL_NAME = 'gt.metatool.01' AND instr(NBT, 'MaxDamage:') > 0;
```

`crafts = MaxDamage / damagePerCraft`, where damagePerCraft is **not in the dump**:
400 hammer/file/screwdriver, 200 saw, 100 crowbar. Steel Hammer = 51,200/400 = **128
crafts**; Iron 64; Bronze 48. Per material, which is why identity keys on
`PrimaryMaterial`.

---

## 4. Data shape you will trip over

- **NBT variants are the majority case.** 1,373 `(mod, name, damage)` keys have
  multiple ITEM rows — 59,219 rows, over half the table. In every case NBT differs,
  never a collision. Biggest families: `TConstruct:BoltPart` 27,399,
  `gregtech:gt.metatool.01` 13,043, `Forestry:beePrincessGE` 750.
- **Fluids are not items.** Separate `FLUID` table, separate recipe join tables.
  `FLUID_CONTAINER` maps 2,085 filled containers to 17 empties (Empty Cell 1,152,
  Refractory Capsule 306, Wax Capsule 276, Glass Bottle 113, Bucket 91) — a partial
  `getContainerItem`, and the seed for §15's `Swapped`.
- **82% of oredict tags are empty.** 99,346 of 121,352 registered names have no
  members; GT registers the prefix × material cross product. Distinguish "no such
  tag" from "tag exists, disabled" — the second is GT's `mDisabledItems`.
- **Only three recipe categories exported**: `gregtech` (160,304), `minecraft`
  (58,897), `avaritia` (133). Active plugins were BASE / MINECRAFT / NEI / FORGE /
  MOBS_INFO / AVARITIA / GREGTECH / QUEST. **No Forestry** — bee items exist, bee
  breeding recipes do not. **No multiblock structure layouts** — the controller block
  and every casing are ordinary items with ordinary recipes, but the block *counts*
  live only in GT's NEI structure preview.
- **Probability** is a `DOUBLE` on `RECIPE_ITEM_OUTPUTS` / `RECIPE_FLUID_OUTPUTS`,
  `1.0` for deterministic. §5 wants it as an exact `Rational`.

---

## 5. Hazards — all pinned in `tools/identity_probe.py`

Run it before trusting any of this; it exits 1 while findings are open.

| hazard | scale |
|---|---|
| recipes whose output is in their own input group | 1,136 recipes / 1,082 items |
| programmed circuit imported as consumed | input to 22,168 recipes, output of 25 |
| ore-classified items that are recipe outputs | 157 items / 885 recipes |
| oredict tags resolving to >1 item | 990 |
| items carrying >1 oredict tag | 2,115 of 24,309 |

The first two are the ones that will corrupt a walk silently rather than loudly.
