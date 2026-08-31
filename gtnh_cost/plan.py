"""The plan -- §15, the MVP's central object -- and the four views derived from it.

A plan stores **decisions, never quantities**.  `ceil` does not distribute: a
plan built for one circuit that runs a 2-output recipe once cannot be multiplied
by three without inventing waste, so a plan recalled at another scale re-solves.
That is also what makes `UsePlan` compose and several named plans per item
natural.

    Plan     = { id, name, roots: [(ix, qty)], nodes: NodeId -> PlanNode }
    PlanNode = { id, ix, parent, choice }
    Choice   = Open | Stop | UseRecipe {recipe, picks, children}
             | UsePlan {plan}

`roots` is a **list**, and that is the entirety of the multiblock case: nine root
demands read off GT's NEI structure preview, expanded exactly like any other node.

Four views come off the one structure:

    BOM        ix              Open + Stop nodes, summed.  What you buy or mine.
    Schedule   (ix, recipe)    UseRecipe nodes, summed -> run counts.
    Machines   (map, minTier)  distinct over the schedule.
    Surplus    ix              production - consumption, where positive.

**Batch at the schedule key.**  If three branches need 10, 5 and 3 of an item by
the same recipe, solve 18 once; three separate `ceil`s invent waste the plan does
not incur.  Batching couples run counts across branches, so the quantities are a
flow balance rather than a top-down recursion -- solved here by iterating a
top-down pass to a fixpoint, which is a couple of passes on real plans.

**Byproducts are offered, not netted.**  Surplus is reported; the user applies it.
Silently rewriting their quantities is the one thing this app is designed not to
do, and it is the only honest treatment of a chanced byproduct.
"""
from __future__ import annotations

import math
import time
import uuid
from fractions import Fraction

from . import consume

OPEN = "open"
STOP = "stop"
USE_RECIPE = "recipe"
USE_PLAN = "plan"

# How many times `propagate` will sweep before giving up.  Filling a node mints
# its children, which are fresh places for another item's decision to land, so
# the sweep repeats -- but the chain can only lengthen while the items down it
# stay distinct, so on any plan the app can build it settles in two or three.
PROPAGATE_PASSES = 16

# How many times `solve` will re-run the top-down pass before calling the run
# counts unsettled.  Batching couples branches, so the quantities are a flow
# balance rather than a recursion; without a loop it settles in two.
MAX_PASSES = 40

# How a node's quantity behaves when the same item appears at several nodes.
# `SUMMED` adds up -- ten branches wanting an ingot want ten ingots.  `OWNED`
# does not: a maintained programmed circuit and a tool with no material chosen
# are both "have one of these", and two recipes needing one need one.
SUMMED = "summed"
OWNED = "owned"

# A leaf is final, and that is the whole of it.  There was a `STOP_REASONS` enum
# here -- buy it / mine or gather it / raw, stop here -- and it was removed
# because **nothing ever branched on it**: no quantity, no view, no verdict
# changed with the value, and across two full plans the user set it 0 times out
# of 159 chances.  `cut` was tautological (every stop *is* "stop here"), and
# `buy` versus `mine` only becomes load-bearing if pricing lands -- at which
# point the partition falls out of the price table (does this have a market
# price?) rather than out of a word somebody had to remember to pick.
#
# If annotation turns out to be missed, a free-text note on the node is strictly
# better than a three-word enum: what you actually want to write is "the AE2 guy
# sells these", which no fixed vocabulary can hold.


def frac(x):
    if isinstance(x, Fraction):
        return x
    if isinstance(x, float):
        return Fraction(x).limit_denominator(10 ** 6)
    return Fraction(x)


class PlanNode:
    __slots__ = ("id", "ix", "parent", "choice")

    def __init__(self, nid, ix, parent, choice):
        self.id = nid
        self.ix = ix
        self.parent = parent
        self.choice = choice

    def to_json(self):
        return {"ix": self.ix, "parent": self.parent, "choice": self.choice}

    @classmethod
    def from_json(cls, nid, d):
        return cls(nid, d["ix"], d.get("parent"), d.get("choice") or {"kind": OPEN})

    @property
    def kind(self):
        return self.choice.get("kind", OPEN)


class Plan:
    """Decisions.  Some are about a *node*; two are about an **item**.

    `finals` and `tools` are plan-level on purpose, and for the same reason.  An
    item appears at many nodes -- iron ingot turns up in six branches of a
    turbine -- and "I will buy this rather than make it" is a fact about the
    item in this build, not about the branch you happened to be standing in when
    you said it.  Kept per node, the same statement had to be repeated at every
    occurrence, and any occurrence you had not visited yet silently went on
    expanding.  The same argument applies to a tool's material: you own one
    hammer, so charging a fraction of a steel one in the branch where you picked
    it and a whole unspecified one everywhere else does not describe anything.
    """

    def __init__(self, pid=None, name="untitled"):
        self.id = pid or ("p~" + uuid.uuid4().hex[:12])
        self.name = name
        self.roots = []          # [{"node": nid, "qty": Fraction}]
        self.nodes = {}
        self.machines = None     # None = "I have everything"; else {map: tierIdx}
        self.finals = set()      # items final everywhere in this plan
        self.tools = {}          # generic tool ix -> the material variant ix
        # How far you have got in the build order: step id -> {"n", "at"}.
        # Not a decision and not a quantity -- a fact about your game, kept
        # here because it is about this plan and nothing else.  Keyed on the
        # decision (`order.make_id`), so changing a recipe retires the step you
        # ticked instead of silently carrying the tick to a different one, and
        # `n` is what you did rather than what is now needed.  See `order.py`.
        self.progress = {}
        self.note = ""
        self.created = time.time()
        self.updated = time.time()

    # -- json -------------------------------------------------------------
    def to_json(self):
        return {
            "id": self.id, "name": self.name, "note": self.note,
            "roots": [{"node": r["node"], "qty": str(r["qty"])} for r in self.roots],
            "nodes": {nid: n.to_json() for nid, n in self.nodes.items()},
            "machines": self.machines,
            "finals": sorted(self.finals),
            "tools": self.tools,
            "progress": self.progress,
            "created": self.created, "updated": self.updated,
        }

    @classmethod
    def from_json(cls, d):
        p = cls(d["id"], d.get("name") or "untitled")
        p.note = d.get("note", "")
        p.roots = [{"node": r["node"], "qty": Fraction(r["qty"])}
                   for r in d.get("roots", [])]
        p.nodes = {nid: PlanNode.from_json(nid, nd)
                   for nid, nd in d.get("nodes", {}).items()}
        p.machines = d.get("machines")
        # Written as an object (ix -> reason) before reasons were removed, and
        # as a list since; the keys were always the whole of the meaning.
        got = d.get("finals") or []
        p.finals = set(got.keys() if isinstance(got, dict) else got)
        p.tools = dict(d.get("tools") or {})
        p.progress = {k: dict(v) for k, v in (d.get("progress") or {}).items()
                      if isinstance(v, dict)}
        p.created = d.get("created", time.time())
        p.updated = d.get("updated", time.time())
        if not p.tools:
            p._lift_node_tools()
        return p

    def _lift_node_tools(self):
        """Plans written before tools were plan-level keep them per node.

        Lift them once, so an existing plan stops charging one whole hammer at
        every node where the material had not been re-picked.  The node-level
        entries stay put and still win, so nothing is lost if two branches
        really did name different materials.
        """
        for node in self.nodes.values():
            for gix, vix in (node.choice.get("tools") or {}).items():
                self.tools.setdefault(gix, vix)

    # -- structure --------------------------------------------------------
    def new_node(self, ix, parent=None):
        nid = "n" + uuid.uuid4().hex[:10]
        self.nodes[nid] = PlanNode(nid, ix, parent, {"kind": OPEN})
        return self.nodes[nid]

    def add_root(self, ix, qty=1):
        n = self.new_node(ix, None)
        self.roots.append({"node": n.id, "qty": frac(qty)})
        self.autofill(n)
        return n

    def remove_root(self, nid):
        self.roots = [r for r in self.roots if r["node"] != nid]
        self._drop_subtree(nid)

    def _drop_subtree(self, nid):
        node = self.nodes.get(nid)
        if node is None:
            return
        for cid in list(node.choice.get("children", {}).values()):
            self._drop_subtree(cid)
        self.nodes.pop(nid, None)

    def clear_choice(self, nid, keep_open=False):
        """Back to Open, discarding the subtree below.

        `keep_open` marks it as a reopen you meant: the node stays on the
        frontier and `autofill` will not quietly put the same subplan back on
        it the next time the item is decided somewhere else.  Internal callers
        that are about to write a new choice leave it off.
        """
        node = self.nodes.get(nid)
        if node is None:
            return
        for cid in list(node.choice.get("children", {}).values()):
            self._drop_subtree(cid)
        node.choice = {"kind": OPEN, "kept": True} if keep_open else {"kind": OPEN}

    def set_stop(self, nid, everywhere=True):
        """Mark a leaf final.  By default that is a statement about the *item*.

        Every other undecided place the item appears becomes final too, and so
        does any place it turns up later when another recipe is expanded -- that
        second half is why this is a plan-level fact rather than a sweep over
        the nodes that exist right now.  A node that has been deliberately
        expanded keeps its recipe; `expanded_elsewhere` reports those so the UI
        can say so rather than quietly contradicting itself.
        """
        node = self.nodes.get(nid)
        if node is None:
            return 0
        self.clear_choice(nid)
        node.choice = {"kind": STOP}
        if everywhere:
            self.finals.add(node.ix)
        return self.places(node.ix)

    def clear_final(self, ix):
        self.finals.discard(ix)

    # -- progress through the build order ---------------------------------
    def mark_done(self, sid, n=None):
        """You made it.  `n` is how much of it you made, which is recorded so
        that raising a quantity afterwards can say what is left rather than
        either losing the tick or pretending the step is still finished."""
        self.progress[sid] = {"n": str(n) if n is not None else "",
                              "at": time.time()}

    def mark_undone(self, sid):
        self.progress.pop(sid, None)

    def clear_progress(self):
        self.progress = {}

    def places(self, ix):
        """How many nodes in this plan are this item."""
        return sum(1 for n in self.nodes.values() if n.ix == ix)

    def expanded_elsewhere(self, ix, except_nid=None):
        """Nodes for `ix` that carry an explicit recipe or adopted plan.

        A plan-level `final` never overrides one of these -- an expansion you
        made on purpose outranks a default -- so they are the honest exception
        to "it is final everywhere", and worth naming.
        """
        return [n for n in self.nodes.values()
                if n.ix == ix and n.id != except_nid
                and n.kind in (USE_RECIPE, USE_PLAN)]

    # -- autofill: the same item, planned the same way ---------------------
    # A third decision that belongs to the item rather than the node.  If you
    # have already worked out how you make copper wire, a copper wire that turns
    # up in another branch should arrive with that subplan already on it.
    #
    # Unlike `finals` and `tools` this cannot be a solve-time overlay, because a
    # recipe needs child *nodes* to exist.  So it copies, once, when the node is
    # created -- which also bounds it: the source subtree is finite and is
    # duplicated verbatim rather than replayed, so nothing cascades.

    def autofill(self, node):
        """Fill a fresh node from the same item's subplan elsewhere, if any."""
        if node is None or node.kind != OPEN:
            return False
        if node.ix in self.finals:
            return False        # "buy it" outranks "build it the way I did"
        if node.choice.get("kept"):
            return False        # you put this one back on the frontier on purpose
        if node.ix in self._ancestor_items(node.id):
            # Inside a loop, "the same subplan again" is the thing that grows,
            # and the loop is flagged already.  Leave it to the user.
            return False
        src = self._autofill_source(node.ix, node.id)
        if src is None:
            return False
        self._copy_subtree(src, node, frozenset())
        node.choice["auto"] = True
        return True

    def propagate(self):
        """Fill every undecided place an item is already planned out.

        Filling on creation is only half of "say it once".  It carries a
        decision *forward*, to the occurrences that turn up later -- but an
        item is usually planned out somewhere deep long after a shallower
        occurrence of it was minted, and that older node sat undecided for
        good.  Which is why the feature looked like it worked or did not
        depending on the order the branches were walked in.  So: sweep when a
        decision is made, as well as filling when a node is made.

        To a fixpoint, because filling a node mints its children and those are
        fresh places for another item's decision to land.  A pass that fills
        nothing ends it, and `autofill` already declines a node whose item
        stands above it, so a copied subtree cannot chain round a loop.

        Only Open nodes are touched, so this never argues with a decision: a
        node you expanded on purpose keeps its recipe, one you marked final
        stays final, and one you deliberately reopened stays on the frontier.
        """
        filled = []
        for _ in range(PROPAGATE_PASSES):
            live = self.reachable()
            got = [n.id for n in list(self.nodes.values())
                   if n.kind == OPEN and n.id in live and self.autofill(n)]
            if not got:
                break
            filled.extend(got)
        return filled

    def _ancestor_items(self, nid):
        out, seen = set(), set()
        cur = self.nodes.get(nid)
        while cur is not None and cur.parent and cur.parent not in seen:
            seen.add(cur.parent)
            cur = self.nodes.get(cur.parent)
            if cur is not None:
                out.add(cur.ix)
        return out

    def reachable(self):
        """Node ids reachable from a root.  Everything else is dead: no view
        starts anywhere but the roots."""
        out = set()
        stack = [r["node"] for r in self.roots]
        while stack:
            nid = stack.pop()
            if nid in out or nid not in self.nodes:
                continue
            out.add(nid)
            stack.extend((self.nodes[nid].choice.get("children") or {}).values())
        return out

    def prune(self):
        """Drop nodes nothing points at, and say how many.

        A plan built by the current code should never have any; this collects
        what older files accumulated, and is cheap insurance against the class
        of bug rather than the one instance of it.
        """
        live = self.reachable()
        dead = [nid for nid in self.nodes if nid not in live]
        for nid in dead:
            self.nodes.pop(nid, None)
        return len(dead)

    def _autofill_source(self, ix, exclude_nid):
        """The most worked-out decided node for this item.

        "Most decided descendants" rather than "first in route order": if the
        same item is planned twice, the one you carried further is the one you
        meant, and copying the thinner one would look like the feature had
        misfired.

        Only reachable nodes count -- copying from a branch the user abandoned
        would be the feature reviving a decision they had already thrown away.
        """
        live = self.reachable()
        best, best_score = None, 0
        for n in self.nodes.values():
            if n.ix != ix or n.id == exclude_nid or n.id not in live:
                continue
            if n.kind not in (USE_RECIPE, USE_PLAN):
                continue
            score = self._decided_count(n.id, frozenset())
            if score > best_score:
                best, best_score = n, score
        return best

    def _decided_count(self, nid, seen):
        node = self.nodes.get(nid)
        if node is None or nid in seen:
            return 0
        seen = seen | {nid}
        n = 1 if node.kind in (USE_RECIPE, USE_PLAN, STOP) else 0
        for cid in (node.choice.get("children") or {}).values():
            n += self._decided_count(cid, seen)
        return n

    def _copy_subtree(self, src, dst, seen):
        """Copy `src`'s decisions onto `dst`, minting fresh child nodes.

        `seen` guards against a malformed file whose node is its own descendant;
        a plan built through the app cannot produce one.
        """
        if src.id in seen:
            return
        seen = seen | {src.id}
        choice = src.choice
        kind = choice.get("kind")
        if kind == STOP:
            dst.choice = {"kind": STOP}
        elif kind == USE_PLAN:
            dst.choice = {"kind": USE_PLAN, "plan": choice.get("plan")}
        elif kind == USE_RECIPE:
            children = {}
            for cix, cid in (choice.get("children") or {}).items():
                kid = self.new_node(cix, dst.id)
                children[cix] = kid.id
                scn = self.nodes.get(cid)
                if scn is not None:
                    self._copy_subtree(scn, kid, seen)
            dst.choice = {"kind": USE_RECIPE, "recipe": choice.get("recipe"),
                          "picks": dict(choice.get("picks") or {}),
                          "children": children,
                          "tools": dict(choice.get("tools") or {}),
                          "sig": choice.get("sig")}

    def set_tool(self, gix, vix, everywhere=True, nid=None):
        """Which material the tool in slot `gix` is made of.

        Plan-level by default: wear is a fraction of one owned tool, and the
        same tool is the same tool in every recipe that calls for it.
        """
        if everywhere:
            if vix:
                self.tools[gix] = vix
            else:
                self.tools.pop(gix, None)
            # Drop this node's override, or it would keep winning over the
            # answer the user just gave for the whole plan.
            node = self.nodes.get(nid) if nid else None
            if node is not None and node.kind == USE_RECIPE and \
                    node.choice.get("tools"):
                node.choice["tools"].pop(gix, None)
            return
        node = self.nodes.get(nid)
        if node is None or node.kind != USE_RECIPE:
            return
        tools = dict(node.choice.get("tools") or {})
        if vix:
            tools[gix] = vix
        else:
            tools.pop(gix, None)
        node.choice["tools"] = tools

    def set_use_plan(self, nid, other_pid):
        node = self.nodes.get(nid)
        if node is None:
            return
        self.clear_choice(nid)
        node.choice = {"kind": USE_PLAN, "plan": other_pid}
        self.propagate()

    def set_recipe(self, nid, recipe, picks, ingredients, tool_variants=None,
                   keep_children=True):
        """Record a UseRecipe decision and reconcile the child nodes.

        Children are keyed by ingredient ix, so re-resolving one slot keeps every
        decision already made about the ingredients that did not change.
        """
        node = self.nodes.get(nid)
        if node is None:
            return []
        # `old` is what is there now; `reuse` is what we are allowed to keep.
        # These were the same variable, so switching a node to a *different*
        # recipe -- where `keep_children` is false -- left the previous subtree
        # in `self.nodes` with nothing pointing at it.  Unreachable nodes are
        # invisible to every view, so it only showed up as a growing file --
        # until autofill started looking for "the same item elsewhere in this
        # plan" and could have copied from an abandoned branch.
        old = dict(node.choice.get("children") or {})
        reuse = old if keep_children else {}
        children = {}
        filled = []
        for ing in ingredients:
            existing = reuse.get(ing.ix)
            if existing and existing in self.nodes:
                children[ing.ix] = existing
            else:
                kid = self.new_node(ing.ix, nid)
                children[ing.ix] = kid.id
                if self.autofill(kid):
                    filled.append(ing.ix)
        for ix, cid in old.items():
            if children.get(ix) != cid:
                self._drop_subtree(cid)
        # Keep any per-node tool material already on this node unless the caller
        # names one.  Re-resolving a slot used to reset this dict, so adjusting
        # an unrelated ingredient silently un-picked the hammer's material and
        # the plan went back to charging one whole tool.
        tools = dict(node.choice.get("tools") or {}) \
            if node.choice.get("kind") == USE_RECIPE else {}
        tools.update(tool_variants or {})
        tools = {ix: v for ix, v in tools.items()
                 if any(ing.ix == ix for ing in ingredients)}
        node.choice = {"kind": USE_RECIPE, "recipe": recipe.rid,
                       "picks": {str(k): v for k, v in (picks or {}).items()},
                       "children": children,
                       "tools": tools,
                       "sig": recipe.signature()}
        # The decision just made is about the *item*, so carry it to every
        # other undecided place the item appears -- not only to the children
        # minted above.
        self.propagate()
        return filled

    def ancestors(self, nid):
        out = []
        seen = set()
        cur = self.nodes.get(nid)
        while cur is not None and cur.parent and cur.parent not in seen:
            seen.add(cur.parent)
            out.append(cur.parent)
            cur = self.nodes.get(cur.parent)
        return out

    def touch(self):
        self.updated = time.time()


# --------------------------------------------------------------------------
# Ingredients: slots resolved, then aggregated.  Never the other way round.
# --------------------------------------------------------------------------

class Ingredient:
    __slots__ = ("ix", "qty", "zero_stack", "slots")

    def __init__(self, ix, qty, zero_stack, slots):
        self.ix = ix
        self.qty = qty
        self.zero_stack = zero_stack
        self.slots = slots


def resolve_slot(slot, picks):
    """The ix this slot resolves to: the user's pick, else the slot's default."""
    got = picks.get(str(slot.key))
    if got:
        for ix, _q in slot.members:
            if ix == got:
                return got
    return slot.default


def ingredients_of(recipe, picks):
    """Aggregate resolved slots to ingredients.

    Slots are not ingredients: the Basic Steam Turbine is 9 slots and 6
    ingredients, and the aggregation can only happen after every set-valued slot
    is resolved, because an unresolved set has no item to aggregate on.
    """
    picks = {str(k): v for k, v in (picks or {}).items()}
    acc = {}
    for slot in recipe.slots:
        ix = resolve_slot(slot, picks)
        if ix is None:
            continue
        qty = 0
        for mix, mq in slot.members:
            if mix == ix:
                qty = mq
                break
        row = acc.get(ix)
        if row is None:
            acc[ix] = row = [0, False, []]
        if qty == 0:
            row[1] = True
        row[0] += qty
        row[2].append(slot.key)
    out = []
    for ix, (qty, zero, slots) in acc.items():
        if qty == 0:
            qty = 1          # a zero-size stack is one item, required not consumed
        out.append(Ingredient(ix, frac(qty), zero, slots))
    return out


# --------------------------------------------------------------------------
# Solve
# --------------------------------------------------------------------------

class SNode:
    """One position in the expanded tree.  A plan node appears once, unless a
    UsePlan grafts the same plan in twice."""

    __slots__ = ("path", "ix", "node_id", "plan_id", "kind", "recipe", "picks",
                 "ingredients", "children", "demand", "key", "note",
                 "tools", "depth", "charge", "looped", "uses", "parent")

    def __init__(self, path, ix, node_id, plan_id, kind, depth):
        self.path = path
        self.ix = ix
        self.node_id = node_id
        self.plan_id = plan_id
        self.kind = kind
        self.depth = depth
        self.recipe = None
        self.picks = {}
        self.tools = {}
        self.ingredients = []
        self.children = []
        self.demand = Fraction(0)
        self.key = None
        self.note = ""
        # How the parent recipe charges this node: `OWNED` means "one of these,
        # forever" and does not add up across the plan.  See `_views`.
        self.charge = None
        # This item is somewhere above this node too.  A warning, not a stop --
        # §14 step 3, "cycle detection warns; it does not solve".
        self.looped = False
        # For a worn tool: how many times it is picked up here.  `demand` is
        # that divided by the tool's crafts, which is the honest cost but not a
        # number anybody can act on -- "0.195 of a File" is 25 uses of one.
        self.uses = None
        # The SNode above this one, so a raw material can say what it goes into.
        self.parent = None


class Solution:
    def __init__(self):
        self.roots = []
        self.snodes = []
        self.by_node = {}         # plan node id -> [SNode]
        self.schedule = []
        self.bom = []
        self.machines = []
        self.surplus = []
        self.report = []
        self.warnings = []
        self.converged = True
        self.passes = 0
        self.repaired = False

    def demand_of(self, node_id):
        return sum((s.demand for s in self.by_node.get(node_id, ())), Fraction(0))

    def uses_of(self, node_id):
        """For a worn tool: how many times it is picked up at this node.

        None when the node is not a worn tool with a known wear rate, which is
        a different thing from zero.
        """
        got = [s.uses for s in self.by_node.get(node_id, ()) if s.uses is not None]
        return sum(got, Fraction(0)) if got else None

    def snode_of(self, node_id):
        got = self.by_node.get(node_id)
        return got[0] if got else None


class Solver:
    def __init__(self, registry, index, overlay, store=None):
        self.reg = registry
        self.index = index
        self.overlay = overlay
        self.store = store

    # -- tree building ----------------------------------------------------
    def _build(self, plan, sol, plan_stack):
        for i, root in enumerate(plan.roots):
            node = plan.nodes.get(root["node"])
            if node is None:
                continue
            sn = self._build_node(plan, node, str(i), sol, plan_stack, (), 0)
            sn.demand = frac(root["qty"])
            sol.roots.append(sn)

    def _build_node(self, plan, node, path, sol, plan_stack, ancestry, depth,
                    on_path=()):
        """`ancestry` is the ordered list of item ix above this node, so a loop
        can be *named* and not merely detected.  `on_path` is the structural
        guard: a plan is a forest by construction, so a repeated node id can only
        come from a hand-edited file, and recursing on it would hang the app."""
        if (plan.id, node.id) in on_path:
            sn = SNode(path, node.ix, node.id, plan.id, STOP, depth)
            sol.snodes.append(sn)
            sol.by_node.setdefault(node.id, []).append(sn)
            sn.note = "this plan file is broken here"
            sol.warnings.append({
                "kind": "malformed", "path": path, "ix": node.ix,
                "text": "step %s contains itself in %s -- the plan file is "
                        "broken, so this branch was not expanded"
                        % (node.id, plan.name)})
            return sn
        on_path = on_path + ((plan.id, node.id),)
        sn = SNode(path, node.ix, node.id, plan.id, node.kind, depth)
        sol.snodes.append(sn)
        sol.by_node.setdefault(node.id, []).append(sn)
        obj = self.reg.get(node.ix)
        if obj is None:
            sn.kind = STOP
            sn.note = "unknown item -- not in the pack data"
            sol.warnings.append({"kind": "unknown-item", "path": path,
                                 "text": "%s is not in the pack data" % node.ix})
            return sn
        if node.kind == OPEN and node.ix in plan.finals:
            # Marked final somewhere else in this plan.  A node the user
            # explicitly gave a recipe is not OPEN and falls straight through.
            sn.kind = STOP
            sn.note = "final everywhere in this plan"
            return sn

        if node.kind == STOP:
            return sn
        if node.kind not in (USE_PLAN, USE_RECIPE):
            # Open: a leaf, whatever else is true of it.  The old code tested
            # for a loop before this and so marked stopped and undecided nodes
            # as cycles, which they cannot be -- nothing recurs below a leaf.
            return sn

        if node.ix in ancestry:
            # **§14 step 3: "Cycle detection warns; it does not solve."**  This
            # used to stop expanding here, and that was wrong on both counts.
            #
            # It was wrong about the *diagnosis*: the test is on the item alone,
            # so it fires wherever an item reappears on the path no matter which
            # recipes were chosen.  The worked case is the user's own -- an
            # Electronic Circuit built by the shaped recipe, which needs
            # Resistors, which need a Programmed Circuit, which is made from an
            # Electronic Circuit.  The loop closes through the *programmed
            # circuit's* recipe; the circuit recipe chosen here does not contain
            # itself, and picking a different Resistor recipe further down breaks
            # the loop outright.  Refusing to expand is what stopped them getting
            # there.
            #
            # And it was wrong about the *remedy*.  Nothing here needs the cut to
            # terminate: `plan.nodes` is a forest built by `set_recipe`, which
            # only ever mints a fresh child or reuses one of the same node's own,
            # so a node can never be its own descendant.  Expansion is bounded by
            # what the user actually built.  What a loop threatens is the run
            # counts, and that is the fixpoint's business -- it iterates and says
            # so when it does not settle.
            #
            # So: warn, name the loop, and expand.
            trail = " <- ".join(self._name(ix) for ix in
                                reversed(ancestry[max(0, len(ancestry) - 4):]))
            sol.warnings.append({
                "kind": "cycle", "path": path, "ix": node.ix,
                "text": "%s is needed to make itself, by way of %s. The "
                        "amounts around the loop are worked out by repeating "
                        "the calculation until it settles; the totals below are "
                        "real, and you will be told if they do not settle. "
                        "Choosing a different recipe anywhere around the loop "
                        "breaks it." % (obj.name, trail)})
            sn.looped = True
        if node.kind == USE_PLAN:
            other = None
            pid = node.choice.get("plan")
            if self.store and pid and pid not in plan_stack:
                other = self.store.get(pid)
            if other is None:
                sn.kind = OPEN
                sn.note = ("that plan is missing, or using it would loop"
                           if pid else "no plan chosen")
                sol.warnings.append({"kind": "plan", "path": path,
                                     "text": "%s is set to use plan %s, which "
                                             "is missing, or would loop"
                                             % (obj.name, pid)})
                return sn
            sn.note = "using plan " + other.name
            match = None
            for r in other.roots:
                rn = other.nodes.get(r["node"])
                if rn is not None and rn.ix == node.ix:
                    match = rn
                    break
            if match is None:
                sn.kind = OPEN
                sn.note = "plan %s does not make this item" % other.name
                return sn
            # The adopted root *is* this node, seen through another plan's
            # decisions, so it does not enter its own ancestry -- adding it here
            # made every UsePlan report a cycle against itself.
            child = self._build_node(other, match, path + ".p", sol,
                                     plan_stack | {other.id},
                                     ancestry, depth + 1, on_path)
            sn.kind = USE_PLAN
            child.parent = sn
            sn.children = [child]
            return sn

        rid = node.choice.get("recipe")
        recipe = self.index.get(rid)
        if recipe is None:
            sn.kind = OPEN
            sn.note = "recipe is no longer in the pack data"
            sol.warnings.append({
                "kind": "dangling", "path": path, "node": node.id,
                "text": "the recipe chosen for %s is not in the pack data any "
                        "more -- an edited recipe counts as a new one. Pick "
                        "again." % obj.name})
            return sn
        sn.recipe = recipe
        sn.picks = node.choice.get("picks") or {}
        # Plan-level material first, this node's own override on top.
        sn.tools = dict(plan.tools or {})
        sn.tools.update(node.choice.get("tools") or {})
        sn.ingredients = ingredients_of(recipe, sn.picks)
        # Only this recipe's own tools belong in the schedule key: carrying the
        # whole plan's would stop two identical nodes batching just because some
        # unrelated slot elsewhere had a material picked.
        mine = {ing.ix for ing in sn.ingredients}
        sn.tools = {ix: v for ix, v in sn.tools.items() if ix in mine}
        sn.key = (node.ix, rid, tuple(sorted(sn.picks.items())),
                  tuple(sorted(sn.tools.items())))
        got = recipe.output_for(node.ix)
        if got is None or got[2] <= 0:
            sn.kind = OPEN
            sn.note = "that recipe does not produce this item any more"
            return sn
        children = node.choice.get("children") or {}
        sub_ancestry = ancestry + (node.ix,)
        for ing in sn.ingredients:
            cid = children.get(ing.ix)
            cnode = plan.nodes.get(cid) if cid else None
            if cnode is None:
                # A recipe gained an ingredient, or a child node was lost.
                # Repair it rather than rendering a hole, and tell the caller so
                # it knows the plan is worth writing back.
                cnode = plan.new_node(ing.ix, node.id)
                children[ing.ix] = cnode.id
                node.choice["children"] = children
                sol.repaired = True
            child_sn = self._build_node(
                plan, cnode, "%s.%d" % (path, len(sn.children)), sol,
                plan_stack, sub_ancestry, depth + 1, on_path)
            child_sn.parent = sn
            sn.children.append(child_sn)
        return sn

    # -- the fixpoint -----------------------------------------------------
    def solve(self, plan):
        sol = Solution()
        self._build(plan, sol, {plan.id})
        root_demand = {sn.path: sn.demand for sn in sol.roots}

        inflate = {}
        runs = {}
        for npass in range(MAX_PASSES):
            sol.passes = npass + 1
            for sn in sol.snodes:
                sn.demand = Fraction(0)
            for sn in sol.roots:
                self._push(sn, root_demand[sn.path], inflate, sol)
            # Two totals per schedule key, and they are not the same number.
            #
            #   `asked`  every node's demand, added up.  This is how a run is
            #            *shared out* again, so the pieces still add to the run.
            #   `demand` what you actually have to make.  Own-not-consume nodes
            #            do not add up here: two recipes that each want a
            #            programmed circuit in a slot want one circuit, so this
            #            takes the largest single ask rather than the sum.
            #
            # Keeping them apart is the whole trick.  Using `asked` for both
            # built two circuits and charged two electronic circuits for them;
            # using `demand` for both would make one circuit and then let each
            # of the two nodes bill a full run's ingredients for it.
            # The two buckets are accumulated separately and only then added: an
            # item can be consumed by one recipe and merely held by another, and
            # folding `max` and `+` into one running total would make the answer
            # depend on the order the nodes happened to come in.
            demand, asked, owned = {}, {}, {}
            for sn in sol.snodes:
                if sn.key is None or sn.kind != USE_RECIPE:
                    continue
                asked[sn.key] = asked.get(sn.key, Fraction(0)) + sn.demand
                if sn.charge == OWNED:
                    owned[sn.key] = max(owned.get(sn.key, Fraction(0)), sn.demand)
                else:
                    demand[sn.key] = demand.get(sn.key, Fraction(0)) + sn.demand
            for key, held in owned.items():
                demand[key] = demand.get(key, Fraction(0)) + held
            new_runs, new_inflate = {}, {}
            for sn in sol.snodes:
                if sn.key is None or sn.kind != USE_RECIPE or sn.key in new_runs:
                    continue
                exp = sn.recipe.output_for(sn.ix)[2]
                D, A = demand[sn.key], asked[sn.key]
                r = int(math.ceil(D / exp)) if D > 0 else 0
                new_runs[sn.key] = r
                new_inflate[sn.key] = (Fraction(r) * exp / A) if A > 0 else Fraction(1)
            settled = new_runs == runs
            runs, inflate = new_runs, new_inflate
            if settled:
                break
        else:
            sol.converged = False
            sol.warnings.append({
                "kind": "fixpoint",
                "text": "run counts did not settle after %d passes, because a "
                        "loop or a byproduct ties them together. The numbers "
                        "below are from the last pass and are approximate."
                        % MAX_PASSES})

        self._views(plan, sol, runs, demand)
        return sol

    def _push(self, sn, demand, inflate, sol):
        sn.demand += demand
        if sn.kind == USE_PLAN and sn.children:
            # A plan stores decisions, not quantities, so the adopted plan's own
            # root quantity is not a scale factor -- it re-solves at our demand.
            self._push(sn.children[0], demand, inflate, sol)
            return
        if sn.kind != USE_RECIPE or sn.recipe is None:
            return
        exp = sn.recipe.output_for(sn.ix)[2]
        if exp <= 0:
            return
        share = demand / exp * inflate.get(sn.key, Fraction(1))
        for ing, child in zip(sn.ingredients, sn.children):
            d = self._designation(sn, ing)
            if d.kind == consume.MAINTAINED:
                q, child.charge = ing.qty, OWNED
            elif d.kind == consume.WORN:
                crafts, _assumed = self._wear(sn, ing, d)
                # How many times the tool is picked up does not depend on what
                # it is made of -- only how much of it that spends does.  So the
                # use count is known either way, and is worth saying even when
                # the fraction is not: "one owned Mortar, 8 uses" is a question
                # you can answer; "one owned Mortar" is not.
                child.uses = ing.qty * share
                if crafts:
                    q, child.charge = ing.qty * share / crafts, SUMMED
                else:
                    # No material chosen, so no durability to divide by: the
                    # honest fallback is "you own one of these", which is an
                    # OWNED charge and must not be added up node by node.
                    q, child.charge = ing.qty, OWNED
            else:
                q, child.charge = ing.qty * share, SUMMED
            self._push(child, q, inflate, sol)

    def _designation(self, sn, ing):
        obj = self.reg.get(ing.ix)
        return self.overlay.for_input(sn.recipe.rid, obj,
                                      0 if ing.zero_stack else int(ing.qty))

    def _wear(self, sn, ing, d):
        """(crafts per tool, is the damage-per-craft rate assumed).

        Wear is per material, which is why identity keys on PrimaryMaterial; if
        the user has not picked one we do not invent a durability.  The rate is
        a property of the tool class and is known either way -- a Hammer is 400
        per craft whatever it is made of -- so the two are reported separately.
        """
        pick = sn.tools.get(ing.ix)
        if pick:
            crafts, _per, known = consume.crafts_for(self.reg.get(pick))
            if crafts:
                return (Fraction(crafts), not known)
        return (Fraction(d.crafts) if d.crafts else None, d.crafts_assumed)

    # -- the four views ---------------------------------------------------
    def _views(self, plan, sol, runs, demand):
        sched = {}
        production = {}
        for sn in sol.snodes:
            if sn.kind != USE_RECIPE or sn.key is None:
                continue
            row = sched.get(sn.key)
            if row is None:
                qty, chance, exp = sn.recipe.output_for(sn.ix)
                r = runs.get(sn.key, 0)
                sched[sn.key] = row = {
                    "key": sn.key, "ix": sn.ix, "recipe": sn.recipe,
                    "runs": r, "demand": demand.get(sn.key, Fraction(0)),
                    "per_run": frac(qty), "chance": chance, "expected": exp,
                    "produced": exp * r, "nodes": [], "picks": sn.picks,
                    "inputs": [],
                }
                for ing in sn.ingredients:
                    d = self._designation(sn, ing)
                    crafts, assumed = self._wear(sn, ing, d)
                    if d.kind == consume.MAINTAINED:
                        q = ing.qty
                    elif d.kind == consume.WORN and crafts:
                        q = ing.qty * r / crafts
                    elif d.kind == consume.WORN:
                        q = ing.qty
                    else:
                        q = ing.qty * r
                    row["inputs"].append({"ix": ing.ix, "per_run": ing.qty,
                                          "qty": q, "designation": d,
                                          "crafts": crafts,
                                          "uses": (ing.qty * r
                                                   if d.kind == consume.WORN
                                                   else None),
                                          "rate_assumed": assumed,
                                          "per_craft": d.per_craft,
                                          "tool": sn.tools.get(ing.ix)})
                production[sn.ix] = production.get(sn.ix, Fraction(0)) + exp * r
                for out_ix, oq, oc, _k in sn.recipe.byproducts(sn.ix):
                    production[out_ix] = production.get(out_ix, Fraction(0)) + \
                        frac(oq) * frac(oc) * r
            row["nodes"].append(sn)
        sol.schedule = sorted(sched.values(),
                              key=lambda r: (-r["runs"], self._name(r["ix"])))

        # BOM: Open + Stop.  Summed -- except for the own-not-consume nodes,
        # which do not add up.  A hammer with no material chosen is charged as
        # one owned tool; five recipes using it still need one hammer, not five,
        # and adding them produced rows like "2.031 Wire Cutters" made of one
        # whole tool plus two fractions of a differently-worded one.  So the
        # owned contributions collapse to the largest single one, and it is the
        # node that asked for the most that carries it.
        bom = {}
        owned_nodes = {}
        for sn in sol.snodes:
            if sn.kind not in (OPEN, STOP):
                continue
            row = bom.get(sn.ix)
            if row is None:
                bom[sn.ix] = row = {"ix": sn.ix, "qty": Fraction(0),
                                    "open": Fraction(0), "stop": Fraction(0),
                                    "nodes": [], "makeable": None,
                                    "owned": Fraction(0),
                                    "owned_places": 0, "uses": None,
                                    "crafts": None}
            row["nodes"].append(sn)
            if sn.uses is not None:
                # Per node this is a share of a batched run and so fractional;
                # summed over the plan it comes back whole, the same way the
                # ingot shares do.
                row["uses"] = (row["uses"] or Fraction(0)) + sn.uses
            if sn.charge == OWNED:
                owned_nodes.setdefault(sn.ix, []).append(sn)
                continue
            row["qty"] += sn.demand
            if sn.kind == OPEN:
                row["open"] += sn.demand
            else:
                row["stop"] += sn.demand
        for ix, nodes in owned_nodes.items():
            row = bom[ix]
            # Ties go to a decided node, so a settled leaf is not undone by an
            # identical undecided one somewhere else in the walk.
            rep = max(nodes, key=lambda s: (s.demand, s.kind == STOP))
            row["owned"] = rep.demand
            row["owned_places"] = len(nodes)
            row["qty"] += rep.demand
            if rep.kind == OPEN:
                row["open"] += rep.demand
            else:
                row["stop"] += rep.demand
        # Where each raw material goes.  A bill of materials answers "how much
        # copper do I need"; the next question is always "what is eating it",
        # and the plan knows -- every BOM node has a parent, and the parent is a
        # recipe for some other item.  Keyed on (parent item, recipe) because
        # that is the schedule's key too: "36 into Copper Plate by Forge Hammer"
        # is one line whether it came from four branches or one.
        for row in bom.values():
            into = {}
            for sn in row["nodes"]:
                p = sn.parent
                key = (p.ix if p else None,
                       p.recipe.rid if p is not None and p.recipe else None)
                got = into.get(key)
                if got is None:
                    into[key] = got = {
                        "ix": key[0], "recipe": p.recipe if p is not None else None,
                        "qty": Fraction(0), "uses": None, "places": 0,
                        "root": p is None}
                got["qty"] += sn.demand
                got["places"] += 1
                if sn.uses is not None:
                    got["uses"] = (got["uses"] or Fraction(0)) + sn.uses
            row["into"] = sorted(into.values(),
                                 key=lambda d: (-d["qty"], self._name(d["ix"])
                                                if d["ix"] else ""))

        # The rate those uses are measured against, so the BOM can say "25 of a
        # 128-craft File" instead of a bare 0.195.  Taken from the schedule, and
        # only when every recipe using the tool agrees on it: the material is a
        # plan-level decision so they normally do, but a per-recipe override can
        # differ, and then the ratio has no single denominator to quote.
        rates = {}
        for row in sol.schedule:
            for i in row["inputs"]:
                if i.get("crafts"):
                    rates.setdefault(i["ix"], set()).add(i["crafts"])
        for ix, seen_rates in rates.items():
            if ix in bom and len(seen_rates) == 1:
                bom[ix]["crafts"] = next(iter(seen_rates))

        for ix, row in bom.items():
            if row["open"] > 0:
                row["makeable"] = bool(self.index.producer_ids(ix))
        sol.bom = sorted(bom.values(), key=lambda r: (-r["qty"], self._name(r["ix"])))

        # Machines: distinct over the schedule.  Never a cost row.
        machines = {}
        for row in sol.schedule:
            r = row["recipe"]
            if r.machine is None:
                key = ("(placed by hand)", None)
            else:
                key = r.machine
            m = machines.get(key)
            if m is None:
                machines[key] = m = {"map": key[0], "tier": key[1], "name": r.name,
                                     "recipes": 0, "runs": 0}
            m["recipes"] += 1
            m["runs"] += row["runs"]
        sol.machines = sorted(machines.values(), key=lambda m: (-m["runs"], m["map"]))

        # Surplus: production - consumption, where positive.  Offered, not netted.
        consumption = {}
        for row in sol.schedule:
            for i in row["inputs"]:
                consumption[i["ix"]] = consumption.get(i["ix"], Fraction(0)) + i["qty"]
        for r in plan.roots:
            node = plan.nodes.get(r["node"])
            if node is not None:
                consumption[node.ix] = consumption.get(node.ix, Fraction(0)) + \
                    frac(r["qty"])
        # A surplus item is a byproduct unless the plan schedules a run for it,
        # and its chance is the worst chance any of those byproduct slots gave.
        scheduled = {row["ix"] for row in sol.schedule}
        byproduct_chance = {}
        for row in sol.schedule:
            for oix, _q, oc, _k in row["recipe"].byproducts(row["ix"]):
                byproduct_chance[oix] = min(byproduct_chance.get(oix, 1.0), oc)
        surplus = []
        for ix, made in production.items():
            left = made - consumption.get(ix, Fraction(0))
            if left > 0:
                surplus.append({"ix": ix, "qty": left,
                                "byproduct": ix not in scheduled,
                                "chance": byproduct_chance.get(ix, 1.0)})
        sol.surplus = sorted(surplus, key=lambda s: (-s["qty"], self._name(s["ix"])))

        # The report joins BOM and schedule on ix -- neither alone is the answer.
        rows = {}
        for row in sol.bom:
            rows[row["ix"]] = {"ix": row["ix"], "bom": row, "made": Fraction(0),
                               "sched": []}
        for row in sol.schedule:
            r = rows.get(row["ix"])
            if r is None:
                rows[row["ix"]] = r = {"ix": row["ix"], "bom": None,
                                       "made": Fraction(0), "sched": []}
            r["made"] += row["produced"]
            r["sched"].append(row)
        for r in rows.values():
            r["obtain"] = (r["bom"]["qty"] if r["bom"] else Fraction(0))
            r["total"] = r["obtain"] + r["made"]
        sol.report = sorted(rows.values(), key=lambda r: (-r["total"],
                                                          self._name(r["ix"])))

        # Warnings the user should see rather than discover.
        for row in sol.schedule:
            if row["chance"] < 1.0:
                sol.warnings.append({
                    "kind": "chanced", "ix": row["ix"],
                    "text": "%s comes from a %d%% chance output, so %d runs is "
                            "an average rather than a guarantee"
                            % (self._name(row["ix"]), round(row["chance"] * 100),
                               row["runs"])})
            for i in row["inputs"]:
                d = i["designation"]
                if d.kind != consume.WORN:
                    continue
                if not i["crafts"]:
                    sol.warnings.append({
                        "kind": "wear", "ix": i["ix"],
                        "text": "%s is worn out rather than used up, and no "
                                "material is chosen, so there is no durability "
                                "to divide by. It is counted as one tool you own "
                                "for the whole plan -- pick a material to see "
                                "what fraction of one you use."
                                % self._name(i["ix"])})
                if i["rate_assumed"]:
                    # Only when the rate really was assumed.  This used to fire
                    # for every bare tool slot, so a hammer correctly charged at
                    # 400 per craft was reported as "100 is assumed".
                    sol.warnings.append({
                        "kind": "wear-rate", "ix": i["ix"],
                        "text": "%s: the damage one craft does is not in the "
                                "pack data, so %d is assumed for this kind of "
                                "tool"
                                % (self._name(i["ix"]),
                                   i["per_craft"] or
                                   consume.DEFAULT_DAMAGE_PER_CRAFT)})
        for row in sol.bom:
            if row["makeable"] is False and row["open"] > 0:
                sol.warnings.append({
                    "kind": "unmakeable", "ix": row["ix"],
                    "text": "no recipe in the pack data makes %s -- you mine, "
                            "gather or buy it" % self._name(row["ix"])})
        seen = set()
        deduped = []
        for w in sol.warnings:
            k = (w.get("kind"), w.get("text"))
            if k in seen:
                continue
            seen.add(k)
            deduped.append(w)
        sol.warnings = deduped

    def _name(self, ix):
        obj = self.reg.get(ix)
        return obj.name if obj is not None else ix
