"""The build order -- the schedule as a dependency graph, layered.

The four views in `plan.py` say what a route *costs*.  This one says what to
**do**: which recipe to run on which machine, in an order that never asks you
to make something out of a thing you have not made yet, with everything that
can be done at the same time standing side by side, and with your progress
through it written down.

**A step is a schedule row, not a tree node.**  That is the whole reason this
is worth having.  Run counts already batch at `(item, recipe, picks, tools)`,
so "72 copper plates in a Forge Hammer" is *one* trip to *one* machine however
many branches of the route wanted them.  Walking the tree instead would send
you back to the same hammer eleven times and never tell you the total.

The other kind of step is a thing you **obtain** -- a bill-of-materials row.
Those need nothing, so they are the first column, and the drawing reads left
to right as time.

**Order comes from the edges, not from the depth.**  Step A needs step B when
some node of A's recipe is fed by a node of B's.  Depth in the route is not
that: two boxes eight levels apart can be made at the same moment if neither
feeds the other, and a shallow box can be the very last thing you make.  So
the layer is the *longest* path back to something you already have -- the
earliest round in which a step could possibly start -- and everything sharing
a layer is independent by construction.  If two steps in one column needed
each other, the longest path would have separated them.

**The things you asked for share the last column.**  The longest path scatters
them -- a four-root plan put its goals in columns 4, 4, 5 and 6 -- and a drawing
whose goals are strewn through the middle has no end to read towards.  A goal
that feeds nothing is pinned to the last column; one that is also an ingredient
of another goal stays where the topology puts it, because nothing may be drawn
beside the thing it is made of.  Each step therefore carries both `earliest`,
the round it could really start in, and `layer`, the column it is drawn in --
they differ only for the pinned goals, and the view says so where they do.

**A loop is walked, not cut.**  Section 14 step 3's rule reaches this far: an
item needed to make itself is a real route the run counts already solve by
iteration, and refusing to order it would be the cut this model does not make.
Steps that need each other are one *group*, drawn in one column, marked, and
unlocked together -- inside the group there is no first, which is the honest
answer.

**Ticking a step off records how much of it you did.**  A plan stores
decisions, not quantities, so the run count under a step can change after you
have ticked it -- ask for 40 circuits instead of 20 and the plates step goes
from 24 runs to 48.  The tick is kept and the difference reported, rather than
either being thrown away or quietly hidden: you did make the first 24.
"""
from __future__ import annotations

import hashlib
import math
from collections import deque
from fractions import Fraction

from .plan import OPEN, STOP, USE_PLAN, USE_RECIPE

MAKE = "make"
OBTAIN = "obtain"


# --------------------------------------------------------------------------
# Step identity
# --------------------------------------------------------------------------
# A step id has to survive a restart, because it is what a ticked-off step is
# remembered by.  It is therefore derived from the decision -- item, recipe,
# slot picks, tool materials -- and from nothing else.  Change the recipe and
# it is a different step, which is right: what you ticked is not what is there
# now.  Change *how many* and it is the same step, which is also right, and is
# why the run count is recorded alongside the tick rather than inside its id.

def _sid(prefix, parts):
    text = "\x1f".join(parts)
    return prefix + hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def make_id(key):
    """The id of the step that runs one schedule key."""
    ix, rid, picks, tools = key
    return _sid("s", [ix, rid,
                      ",".join("%s=%s" % p for p in picks),
                      ",".join("%s=%s" % t for t in tools)])


def obtain_id(ix):
    """The id of the step that gets hold of one bill-of-materials item."""
    return _sid("o", [ix])


def _ceil(x):
    return int(math.ceil(x if isinstance(x, Fraction) else Fraction(x)))


def _as_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# The order
# --------------------------------------------------------------------------

class Order:
    """Steps, the columns they fall into, and where you have got to."""

    def __init__(self):
        self.steps = {}       # id -> step dict
        self.layers = []      # [[step, ...]] -- one list per column
        self.walk = []        # every step, in the order to do them
        self.loops = []       # [[id, ...]] -- groups of two or more, only
        self.goal_layer = None    # the column the things you asked for share
        self.done = 0
        self.ready = 0
        self.blocked = 0
        self.stale = 0        # ticked, but there is more of it to do now

    @property
    def total(self):
        return len(self.steps)

    def get(self, sid):
        return self.steps.get(sid)


def _step(sid, kind, ix, **fields):
    """A step, with the fields the two kinds share already filled in.

    A make step and an obtain step differ in about a third of their keys and
    agree on the rest, so only the differences are worth writing down.
    """
    st = {
        "id": sid, "kind": kind, "ix": ix, "recipe": None,
        "runs": None, "n": 0, "qty": Fraction(0), "demand": Fraction(0),
        "per_run": None, "chance": 1.0, "picks": {}, "nodes": [],
        "inputs": [], "uses": None, "crafts": None, "owned": None,
        "makeable": None, "into": [], "spare": Fraction(0),
        "needs": set(), "feeds": set(), "root": False, "root_qty": None,
        "looped": False, "layer": 0, "group": None, "loop_with": [],
    }
    st.update(fields)
    return st


def build_order(sol, progress=None, name=None):
    """`sol` is a solved plan; `progress` is `{step id: {"n": .., "at": ..}}`.

    Pure: it reads the solution and the progress record and writes to neither.
    """
    prog = progress or {}
    naming = name or (lambda ix: ix)
    o = Order()

    # -- 1. the steps ------------------------------------------------------
    for row in sol.schedule:
        sid = make_id(row["key"])
        o.steps[sid] = _step(
            sid, MAKE, row["ix"], recipe=row["recipe"],
            runs=row["runs"], n=row["runs"],
            qty=row["produced"], demand=row["demand"],
            per_run=row["per_run"], chance=row["chance"],
            picks=row["picks"], nodes=row["nodes"],
            inputs=[dict(i, **{"from": []}) for i in row["inputs"]],
            spare=row["produced"] - row["demand"])
    for row in sol.bom:
        sid = obtain_id(row["ix"])
        o.steps[sid] = _step(
            sid, OBTAIN, row["ix"], n=_ceil(row["qty"]),
            qty=row["qty"], demand=row["qty"], nodes=row["nodes"],
            uses=row["uses"], crafts=row["crafts"], owned=row["owned"],
            makeable=row["makeable"], into=row["into"])

    def step_of(sn):
        """The step that produces what this node stands for, or None.

        A `UsePlan` node is not a step -- it is another plan's decisions seen
        from here -- so it is looked straight through to the root it adopted.
        """
        while sn is not None and sn.kind == USE_PLAN:
            sn = sn.children[0] if sn.children else None
        if sn is None:
            return None
        if sn.kind == USE_RECIPE and sn.key is not None:
            return make_id(sn.key)
        if sn.kind in (OPEN, STOP):
            return obtain_id(sn.ix)
        return None

    # -- 2. the edges ------------------------------------------------------
    # One arrow per (feeder, fed) pair, collapsed: eleven branches asking the
    # same hammer for plates are one arrow, because they are one trip.
    feeders = {}     # step id -> {input ix -> {step id}}
    for sn in sol.snodes:
        if sn.kind != USE_RECIPE or sn.key is None:
            continue
        tgt = make_id(sn.key)
        if tgt not in o.steps:
            continue
        for ing, child in zip(sn.ingredients, sn.children):
            src = step_of(child)
            if src is None or src not in o.steps:
                continue
            feeders.setdefault(tgt, {}).setdefault(ing.ix, set()).add(src)
            if src == tgt:
                # A recipe fed by its own output.  The offer withholds those,
                # so it takes a hand-edited plan to get here; say so rather
                # than drawing an arrow from a box to itself.
                o.steps[tgt]["looped"] = True
                continue
            o.steps[tgt]["needs"].add(src)
            o.steps[src]["feeds"].add(tgt)
    for sid, per_ix in feeders.items():
        for i in o.steps[sid]["inputs"]:
            i["from"] = sorted(per_ix.get(i["ix"], ()))

    for root in sol.roots:
        st = o.steps.get(step_of(root))
        if st is not None:
            st["root"] = True
            st["root_qty"] = (st["root_qty"] or Fraction(0)) + root.demand

    # -- 3. the columns ----------------------------------------------------
    ids = list(o.steps)
    feeds = {sid: o.steps[sid]["feeds"] for sid in ids}
    needs = {sid: o.steps[sid]["needs"] for sid in ids}
    comp, members = _sccs(ids, feeds, needs)
    o.loops = [sorted(m) for m in members if len(m) > 1]
    for sid, st in o.steps.items():
        st["group"] = comp[sid]
        if len(members[comp[sid]]) > 1:
            st["looped"] = True
            st["loop_with"] = sorted(m for m in members[comp[sid]] if m != sid)

    layer = _layers(len(members), comp, feeds)
    top = max(layer.values()) if layer else 0
    for st in o.steps.values():
        # Two numbers, because they answer different questions.  `earliest` is
        # the longest path and so the first round the step could possibly start
        # in; `layer` is the column it is drawn in, and they are the same for
        # everything except the goals.
        st["earliest"] = layer[st["group"]]
        st["layer"] = st["earliest"]
    # **The things you asked for share the last column.**  Left to the longest
    # path they scatter -- a four-root plan put its goals in columns 4, 4, 5 and
    # 6 -- and the drawing then has no end: you cannot see at a glance what the
    # whole thing is for.  Pinning them is safe exactly when a goal feeds
    # nothing, which is the normal case; a root that is also an ingredient of
    # another root has to stay where the topology puts it, or the drawing would
    # be claiming you can make a thing at the same time as the thing it is made
    # of.  Nothing else can sit at `top` -- every non-goal step feeds something,
    # which would have to be later -- so the last column is the goals and only
    # the goals, and no column in between can be emptied by the move: an
    # occupied column k always has a step at k-1 that feeds it.
    for st in o.steps.values():
        if st["root"] and not st["feeds"]:
            st["layer"] = top
    o.goal_layer = top if any(st["root"] and not st["feeds"]
                              for st in o.steps.values()) else None

    # -- 4. where you have got to ------------------------------------------
    for sid, st in o.steps.items():
        rec = prog.get(sid)
        st["done"] = rec is not None
        st["done_n"] = _as_int(rec.get("n")) if rec else None
        st["done_at"] = (rec or {}).get("at")
        st["redo"] = 0
        if rec and st["done_n"] is not None and st["n"] > st["done_n"]:
            st["redo"] = st["n"] - st["done_n"]
    for sid, st in o.steps.items():
        # A prerequisite inside your own loop cannot block you: nothing in the
        # loop can go first, so the whole group unlocks together.
        st["blocked_by"] = sorted(
            b for b in st["needs"]
            if comp[b] != comp[sid] and not o.steps[b]["done"])
        if st["done"]:
            st["state"] = "done"
            o.done += 1
            if st["redo"]:
                o.stale += 1
        elif st["blocked_by"]:
            st["state"] = "blocked"
            o.blocked += 1
        else:
            st["state"] = "ready"
            o.ready += 1

    # -- 5. the order to read them in --------------------------------------
    def where(st):
        r = st["recipe"]
        return r.name if r is not None else ""

    n_columns = top + 1 if layer else 0
    o.layers = [[] for _ in range(n_columns)]
    for st in o.steps.values():
        o.layers[st["layer"]].append(st)
    for col in o.layers:
        # Grouped by machine inside a column, because a column is one trip:
        # while you are standing at the assembler, run all of it.
        col.sort(key=lambda st: (where(st), -(st["n"] or 0), naming(st["ix"])))
        o.walk.extend(col)
    return o


def _sccs(ids, out_edges, in_edges):
    """Strongly connected components, iteratively.

    Iteratively because a loop is a supported route rather than a bug: the
    recursion depth would be the longest chain in the plan, and the page that
    draws the route must not be the thing that overflows.
    """
    seen = set()
    finish = []
    for s in ids:
        if s in seen:
            continue
        seen.add(s)
        stack = [(s, iter(out_edges.get(s, ())))]
        while stack:
            node, it = stack[-1]
            step = None
            for nxt in it:
                if nxt not in seen:
                    step = nxt
                    break
            if step is None:
                finish.append(node)
                stack.pop()
            else:
                seen.add(step)
                stack.append((step, iter(out_edges.get(step, ()))))
    comp = {}
    members = []
    for s in reversed(finish):
        if s in comp:
            continue
        gid = len(members)
        comp[s] = gid
        found = []
        stack = [s]
        while stack:
            node = stack.pop()
            found.append(node)
            for prev in in_edges.get(node, ()):
                if prev not in comp:
                    comp[prev] = gid
                    stack.append(prev)
        members.append(found)
    return comp, members


def _layers(n_groups, comp, out_edges):
    """Longest path from a group that needs nothing -- the earliest round in
    which a step could start.  The condensation is a DAG, so Kahn finishes."""
    gout = {}
    indeg = dict.fromkeys(range(n_groups), 0)
    for a, bs in out_edges.items():
        for b in bs:
            ga, gb = comp[a], comp[b]
            if ga == gb or gb in gout.get(ga, ()):
                continue
            gout.setdefault(ga, set()).add(gb)
            indeg[gb] += 1
    layer = dict.fromkeys(range(n_groups), 0)
    q = deque(g for g in range(n_groups) if indeg[g] == 0)
    while q:
        g = q.popleft()
        for h in gout.get(g, ()):
            if layer[h] < layer[g] + 1:
                layer[h] = layer[g] + 1
            indeg[h] -= 1
            if indeg[h] == 0:
                q.append(h)
    return layer
