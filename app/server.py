"""The browser layer: server-rendered fragments over `gtnh_cost`.

Why this shape (§14, "The stack"): a plan stores *decisions, never quantities*,
so every derived view is recomputed from the decision list on each read.  The
natural loop is therefore already click -> server re-solves -> re-render, with
the plan living server-side as the single copy.  An SPA would duplicate the
run-count and batching logic in a second language to avoid a round trip that is
free on localhost.

Nothing here holds model state; `World` does.  That is what lets auto mode
attach later without touching this file.
"""
from __future__ import annotations

import argparse
import os
import sys
from fractions import Fraction

from flask import (Flask, abort, has_request_context, jsonify, redirect,
                   render_template, request, url_for)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gtnh_cost import bootstrap, consume  # noqa: E402
from gtnh_cost.db import TIER_INDEX  # noqa: E402
from gtnh_cost.icons import BLANK, BLANK_ETAG  # noqa: E402
from gtnh_cost.plan import OPEN, USE_RECIPE, Plan, ingredients_of  # noqa: E402
from gtnh_cost.order import OBTAIN  # noqa: E402
from gtnh_cost.world import (World, ceil_qty, fmt_exact, fmt_qty,  # noqa: E402
                             fmt_stacks)

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True

WORLD = None

# How many recipes the in-place picker lists before handing off to the node
# page.  An iron ingot has 422 producers; a panel is for choosing between a few
# you can see, not for scrolling a catalogue.
OFFER_CAP = 12

# How a quantity is written: as a plain count, or as the stacks you would pull
# out of a chest.  It is a way of *looking* at a plan, not a decision the plan
# holds -- so, like the graph's folding, it lives in a cookie and never in the
# plan file.
UNITS = ("count", "stacks")
UNITS_COOKIE = "units"


def units_mode():
    """Counts unless the reader has asked for stacks -- and counts outside a
    request, so the model-side formatters stay callable from a script."""
    if not has_request_context():
        return "count"
    u = request.cookies.get(UNITS_COOKIE)
    return u if u in UNITS else "count"


def world():
    global WORLD
    if WORLD is None:
        # A fresh clone has only the xz archives; the 483 MB database and the
        # 119 MB icon zip are built from them here, once.  A checkout that
        # already has them pays nothing.  See `gtnh_cost.bootstrap`.
        bootstrap.ensure_data()
        WORLD = World()
    return WORLD


# --------------------------------------------------------------------------
# Template helpers
# --------------------------------------------------------------------------

def qty_for(x, ix=None):
    """A quantity, written the way the reader asked for.

    `ix` is what makes a number stackable, so passing it is the whole of the
    decision: the numbers with no item behind them -- run counts, tool uses,
    how many nodes are still undecided -- have nothing to divide by and read
    the same in both modes.
    """
    if ix is not None and units_mode() == "stacks":
        return fmt_stacks(x, world().stack_of(ix))
    return fmt_qty(x)


def stack_note(x, ix):
    """What a tooltip can add about this quantity in stacks mode, or None.

    None whenever it would only be repeating what is already on screen: in
    count mode, for a fluid (millibuckets do not stack into anything), and
    below one stack, where "22" is written "22" either way.  An item that does
    not stack says so at any size, because that is the one case where the two
    modes look identical and the reader is owed the reason.
    """
    if ix is None or units_mode() != "stacks":
        return None
    w = world()
    if w.unit(ix):
        return None
    n = w.stack_of(ix)
    if n <= 1:
        return "does not stack"
    shown = fmt_stacks(x, n)
    plain = fmt_qty(x)
    return None if shown == plain else "%s in all · stacks of %s" % (
        plain, "{:,}".format(n))


@app.context_processor
def helpers():
    w = world()
    return {
        "obj": w.get,
        "name": w.name,
        "unit": w.unit,
        "qty": qty_for,
        "stacked": units_mode() == "stacks",
        "stack_note": stack_note,
        "redo_label": _redo_label,
        "exact": fmt_exact,
        "ceil_qty": ceil_qty,
        "plural": plural,
        "TIER_INDEX": TIER_INDEX,
        "DESIGNATIONS": consume.DESIGNATIONS,
        "DESIGNATION_HELP": consume.DESIGNATION_HELP,
        "dump_version": w.version,
        "n_items": len(w.registry.items),
    }


def plural(n, one, many=None):
    """`plural(3, 'use')` -> "uses".  `many` is for the words that do not just
    take an -s, verbs included: `plural(n, 'makes', 'make')`."""
    return one if n == 1 else (many if many is not None else one + "s")


@app.route("/units", methods=["POST"])
def set_units():
    """Switch between counts and stacks, and go back where you were.

    A form rather than a link so it is a POST, and a cookie rather than a query
    parameter so the answer survives following any of the links on the page --
    the route, the graph, the build order and the node pages are one reading
    session, and the reader should not have to say it four times.

    The field is `to` and not `back`: `back` already means something else on
    the node page -- which of the two route views you arrived from -- and two
    hidden fields of the same name meaning different things is how a page ends
    up sending the wrong one.
    """
    mode = request.form.get("mode")
    resp = redirect(_local(request.form.get("to")) or url_for("index"))
    resp.set_cookie(UNITS_COOKIE, mode if mode in UNITS else "count",
                    max_age=60 * 60 * 24 * 365, samesite="Lax")
    return resp


def _local(path):
    """A redirect target from a form field, or None.

    Only ever back to this app: a leading `//` is a host, not a path, and
    anything without a leading `/` could be one too.
    """
    if not path or not path.startswith("/") or path.startswith("//"):
        return None
    return path


def get_plan(pid):
    p = world().store.get(pid)
    if p is None:
        abort(404)
    return p


def solved(pid):
    """The plan and its solution.  Solving can repair a plan whose recipe
    gained an ingredient, and a repair is worth writing back."""
    w = world()
    plan = get_plan(pid)
    sol = w.solve(plan)
    if sol.repaired:
        w.store.save(plan)
    return plan, sol


def parent_recipe_of(plan, node):
    """The recipe the node's parent runs, or None -- it is what says how this
    node's item is used: how many per run, and whether it is used up."""
    parent = plan.nodes.get(node.parent) if node.parent else None
    if parent is None or parent.kind != USE_RECIPE:
        return None, None
    return parent, world().index.get(parent.choice.get("recipe"))


def tree_rows(plan, sol):
    """Flatten the plan into display rows, in the recipe's ingredient order."""
    w = world()
    rows = []

    def walk(nid, depth, root=None):
        node = plan.nodes.get(nid)
        if node is None:
            return
        sn = sol.snode_of(nid)
        recipe = (w.index.get(node.choice.get("recipe"))
                  if node.kind == USE_RECIPE else None)
        row = {
            "node": node, "depth": depth, "snode": sn, "root": root,
            "obj": w.get(node.ix), "recipe": recipe,
            "demand": sol.demand_of(nid),
            "uses": sol.uses_of(nid),
            "kind": sn.kind if sn else node.kind,
            "note": sn.note if sn else "",
            "designation": None,
        }
        parent, prec = parent_recipe_of(plan, node)
        if prec is not None:
            ing = _ingredient_for(prec, parent.choice.get("picks"), node.ix)
            if ing is not None:
                row["designation"] = w.designation(prec.rid, node.ix,
                                                   ing.zero_stack, int(ing.qty))
                row["per_run"] = ing.qty
            tool = ((parent.choice.get("tools") or {}).get(node.ix)
                    or plan.tools.get(node.ix))
            row["tool"] = w.get(tool) if tool else None
            row["is_tool"] = node.ix in w.registry.tool_variants
        rows.append(row)
        if node.kind == USE_RECIPE and recipe is not None:
            children = node.choice.get("children") or {}
            for ing in ingredients_of(recipe, node.choice.get("picks")):
                cid = children.get(ing.ix)
                if cid:
                    walk(cid, depth + 1, root)

    for r in plan.roots:
        walk(r["node"], 0, r)
    return rows


def _ingredient_for(recipe, picks, ix):
    """The ingredient of `recipe` that is `ix`, once its slots are resolved."""
    for ing in ingredients_of(recipe, picks):
        if ing.ix == ix:
            return ing
    return None


def open_ids(rows):
    """Open nodes in route order -- the outstanding work, as a queue."""
    return [r["node"].id for r in rows if r["kind"] == OPEN]


def next_open(rows, after=None):
    """The next undecided node, so the walk is a loop and not a treasure hunt."""
    ids = open_ids(rows)
    if not ids:
        return None
    if after in ids:
        i = ids.index(after)
        return ids[(i + 1) % len(ids)] if len(ids) > 1 else None
    return ids[0]


def back_to(pid, nid):
    """Land back in whichever route view the decision was taken in.

    The list and the graph are the same walk drawn two ways, so a button
    pressed in one must not teleport you into the other.  Carried as a plain
    `back` field rather than a session flag: the two views are meant to be open
    side by side, and a sticky preference would fight that.
    """
    where = request.values.get("back")
    end = "plan_graph" if where == "graph" else "plan_view"
    return redirect(url_for(end, pid=pid) + "#" + nid)


# --------------------------------------------------------------------------
# Plans
# --------------------------------------------------------------------------

@app.route("/")
def index():
    w = world()
    plans = w.store.all()
    return render_template("index.html", plans=plans)


@app.route("/plans", methods=["POST"])
def create_plan():
    w = world()
    name = (request.form.get("name") or "").strip() or "untitled plan"
    p = Plan(name=name)
    ix = _ix()
    if ix:
        p.add_root(ix, _qty(request.form.get("qty"), 1))
    w.store.save(p)
    return redirect(url_for("plan_view", pid=p.id))


@app.route("/plans/<pid>")
def plan_view(pid):
    plan, sol = solved(pid)
    rows = tree_rows(plan, sol)
    return render_template("plan.html", plan=plan, sol=sol, rows=rows,
                           open_n=len(open_ids(rows)), next_open=next_open(rows),
                           view=request.args.get("view", "bom"))


@app.route("/plans/<pid>/graph")
def plan_graph(pid):
    """The same rows as the list view, positioned instead of indented.

    Deliberately the *same* walk: `tree_rows` already carries depth, kind,
    demand and the plan node id, so the drawing is a presentation of the list
    and cannot drift from it.  Only the layout is client-side -- x is depth and
    y is leaf order, which is arithmetic, not model logic -- because collapsing
    a branch is a question about the picture and not a decision about the plan.
    """
    plan, sol = solved(pid)
    rows = tree_rows(plan, sol)
    kids = {}
    for r in rows:
        par = r["node"].parent
        if par:
            kids[par] = kids.get(par, 0) + 1
    return render_template("graph.html", plan=plan, sol=sol, rows=rows,
                           open_n=len(open_ids(rows)),
                           next_open=next_open(rows), kids=kids,
                           max_depth=max((r["depth"] for r in rows), default=0))


@app.route("/plans/<pid>/steps")
def plan_steps(pid):
    """The build order: what to make, where, and in what order.

    Not the route drawn a third way.  The route says how you decided to make
    the thing; this says what to go and do, and its unit is the *schedule* row
    rather than the tree node -- so eleven branches wanting copper plates are
    one trip to one hammer.  `order.build_order` does all of it; nothing about
    the ordering lives here, because the ordering is model logic and the LP
    that is not being built would want the same graph.
    """
    plan, sol = solved(pid)
    order = world().order(plan, sol)
    rows = tree_rows(plan, sol)
    return render_template(
        "steps.html", plan=plan, sol=sol, order=order,
        open_n=len(open_ids(rows)),
        # Ticks whose step is not in the plan any more -- you changed the
        # recipe under one.  Counted and offered for clearing rather than
        # dropped on sight: a dangling recipe after a pack update makes a step
        # vanish for a moment, and silently forgetting the work would be worse
        # than carrying a line of dead JSON.
        orphan_ticks=sum(1 for k in plan.progress if k not in order.steps))


def _states(order):
    """Everything the drawing has to repaint after a tick, and nothing else.

    The client never works out what a tick unlocked: it posts, and the server
    re-derives the whole state from the same `build_order` the page was drawn
    with.  That is the only way the two can never disagree.
    """
    return {
        "steps": {sid: {"state": st["state"], "redo": st["redo"],
                        "redo_label": _redo_label(st),
                        "n": st["n"], "done": st["done"]}
                  for sid, st in order.steps.items()},
        "counts": {"done": order.done, "ready": order.ready,
                   "blocked": order.blocked, "stale": order.stale,
                   "total": order.total},
    }


def _redo_label(st):
    """"2 stacks + 22 more".  An obtain step is short by items, so it is written
    in whatever units the reader picked; a make step is short by *runs*, which
    do not stack."""
    return qty_for(st["redo"], st["ix"] if st["kind"] == OBTAIN else None)


@app.route("/plans/<pid>/steps/<sid>/done", methods=["POST"])
def step_done(pid, sid):
    """Tick a step off, or take the tick back.

    What is recorded is *how much you did* -- the run count at the moment you
    said so.  Raise a quantity afterwards and the step is still ticked, with
    the shortfall reported; that is the honest answer, and it is why this is
    not a set of ids.
    """
    w = world()
    plan = get_plan(pid)
    sol = w.solve(plan)
    order = w.order(plan, sol)
    st = order.get(sid)
    if st is None:
        abort(404)
    if request.form.get("state") == "todo":
        plan.mark_undone(sid)
    else:
        plan.mark_done(sid, st["n"])
    w.store.save(plan)
    if request.values.get("fmt") == "json":
        # The same solution, re-read: a tick changes no decision, so nothing
        # about the route or the run counts can have moved.
        return jsonify(_states(w.order(plan, sol)))
    return redirect(url_for("plan_steps", pid=pid) + "#" + sid)


@app.route("/plans/<pid>/steps/clear", methods=["POST"])
def clear_progress(pid):
    w = world()
    plan = get_plan(pid)
    if request.form.get("scope") == "orphans":
        order = w.order(plan)
        for sid in list(plan.progress):
            if sid not in order.steps:
                plan.mark_undone(sid)
    else:
        plan.clear_progress()
    w.store.save(plan)
    return redirect(url_for("plan_steps", pid=pid))


@app.route("/plans/<pid>/rename", methods=["POST"])
def rename_plan(pid):
    w = world()
    plan = get_plan(pid)
    plan.name = (request.form.get("name") or plan.name).strip() or plan.name
    plan.note = request.form.get("note", plan.note)
    w.store.save(plan)
    return redirect(url_for("plan_view", pid=pid))


@app.route("/plans/<pid>/delete", methods=["POST"])
def delete_plan(pid):
    world().store.delete(pid)
    return redirect(url_for("index"))


@app.route("/plans/<pid>/duplicate", methods=["POST"])
def duplicate_plan(pid):
    w = world()
    src = get_plan(pid)
    copy = w.store.duplicate(pid, (request.form.get("name") or
                                   (src.name + " (copy)")).strip())
    return redirect(url_for("plan_view", pid=copy.id))


@app.route("/plans/<pid>/roots", methods=["POST"])
def add_root(pid):
    w = world()
    plan = get_plan(pid)
    ix = _ix()
    if ix:
        plan.add_root(ix, _qty(request.form.get("qty"), 1))
        w.store.save(plan)
    return redirect(url_for("plan_view", pid=pid))


@app.route("/plans/<pid>/roots/<nid>", methods=["POST"])
def edit_root(pid, nid):
    w = world()
    plan = get_plan(pid)
    if request.form.get("action") == "delete":
        plan.remove_root(nid)
    else:
        for r in plan.roots:
            if r["node"] == nid:
                r["qty"] = _qty(request.form.get("qty"), r["qty"])
    w.store.save(plan)
    return redirect(url_for("plan_view", pid=pid))


@app.route("/plans/<pid>/machines", methods=["GET", "POST"])
def machines(pid):
    w = world()
    plan = get_plan(pid)
    if request.method == "POST":
        if request.form.get("mode") == "all":
            plan.machines = None
        else:
            owned = {}
            for key, val in request.form.items():
                if key.startswith("map:") and val and val != "-":
                    owned[key[4:]] = int(val)
            plan.machines = owned
        w.store.save(plan)
        return redirect(url_for("plan_view", pid=pid))
    return render_template("machines.html", plan=plan, maps=w.machine_maps())


# --------------------------------------------------------------------------
# Nodes -- the expansion loop itself
# --------------------------------------------------------------------------

@app.route("/plans/<pid>/node/<nid>")
def node_view(pid, nid):
    w = world()
    plan = get_plan(pid)
    node = plan.nodes.get(nid)
    if node is None:
        abort(404)
    sol = w.solve(plan)
    obj = w.get(node.ix)
    only = request.args.get("map")
    q = (request.args.get("q") or "").strip()
    offer = w.offer(plan, node.ix, only_map=only, contains=q)
    parent, parent_recipe = parent_recipe_of(plan, node)
    designation = None
    per_run = None
    if parent_recipe is not None:
        ing = _ingredient_for(parent_recipe, parent.choice.get("picks"), node.ix)
        if ing is not None:
            designation = w.designation(parent_recipe.rid, node.ix,
                                        ing.zero_stack, int(ing.qty))
            per_run = ing.qty
    other_plans = w.store.plans_for(node.ix, exclude=pid)
    return render_template(
        "node.html", plan=plan, node=node, it=obj, sol=sol,
        choices=offer.choices, n_choices=offer.total, groups=offer.groups,
        only=only, q=q, n_unfiltered=offer.unfiltered,
        next_open=next_open(tree_rows(plan, sol), nid),
        hidden=offer.hidden,
        parent_recipe=parent_recipe, designation=designation, per_run=per_run,
        demand=sol.demand_of(nid), uses=sol.uses_of(nid),
        crafts=_crafts_at(w, plan, node, parent), other_plans=other_plans,
        tool_variants=w.tool_variants(node.ix),
        chosen_tool=_chosen_tool(plan, node, parent),
        tool_scope=("here" if parent is not None
                    and node.ix in (parent.choice.get("tools") or {})
                    else "everywhere"),
        final_everywhere=node.ix in plan.finals,
        places=plan.places(node.ix),
        expanded_elsewhere=plan.expanded_elsewhere(node.ix, except_nid=nid),
        ore_seeded=w.ore_seeded_cut(node.ix))


@app.route("/plans/<pid>/node/<nid>/offer")
def node_offer(pid, nid):
    """The choice list on its own, as a fragment.

    The graph opens this in place so changing your mind about a recipe costs a
    click rather than a page.  It is deliberately the *same* `offer` call the
    node page makes -- same filters, same gating, same withheld reasons -- so
    the short list can never disagree with the long one.  Everything the panel
    does not carry (tool materials, designation corrections, adopting another
    plan) stays one link away, because those are rarer and want the room.
    """
    w = world()
    plan = get_plan(pid)
    node = plan.nodes.get(nid)
    if node is None:
        abort(404)
    only = request.args.get("map")
    q = (request.args.get("q") or "").strip()
    offer = w.offer(plan, node.ix, only_map=only, contains=q)
    rows = offer.choices
    # Whatever else the cap drops, it must not drop the recipe the node is
    # actually on: the panel would then be a list of things to switch to with
    # no sight of what you are switching from, and no way to re-apply it after
    # changing a slot pick.  Stable, so the rest of the order survives.
    on = node.choice.get("recipe") if node.kind == USE_RECIPE else None
    if on:
        rows = sorted(rows, key=lambda ch: ch.recipe.rid != on)
    sol = w.solve(plan)
    sn = sol.snode_of(nid)
    return render_template(
        "_offer.html", plan=plan, node=node, it=w.get(node.ix),
        choices=rows[:OFFER_CAP], n_shown=min(len(rows), OFFER_CAP),
        n_choices=offer.total, groups=offer.groups, only=only, q=q,
        n_unfiltered=offer.unfiltered, hidden=offer.hidden,
        # The *solved* kind, not `node.choice.kind`: an item marked final
        # plan-wide leaves its nodes undecided, and offering "final instead" on
        # something already final would be a button that does nothing.
        kind=(sn.kind if sn else node.kind), demand=sol.demand_of(nid))


def _crafts_at(w, plan, node, parent):
    """The chosen tool material's crafts-per-tool, so the node page can put the
    fraction in units anybody can act on."""
    pick = _chosen_tool(plan, node, parent)
    return consume.crafts_for(w.get(pick))[0] if pick else None


def _chosen_tool(plan, node, parent):
    """This node's own material if it has one, else the plan's."""
    if parent is not None and parent.kind == USE_RECIPE:
        got = (parent.choice.get("tools") or {}).get(node.ix)
        if got:
            return got
    return plan.tools.get(node.ix)


@app.route("/plans/<pid>/node/<nid>/recipe", methods=["POST"])
def choose_recipe(pid, nid):
    w = world()
    plan = get_plan(pid)
    node = plan.nodes.get(nid)
    if node is None:
        abort(404)
    rid = request.form.get("recipe")
    recipe = w.index.get(rid)
    if recipe is None:
        abort(400)
    picks = {}
    for key, val in request.form.items():
        if key.startswith("pick:") and val:
            picks[key[5:]] = val
    ings = ingredients_of(recipe, picks)
    keep = node.kind == USE_RECIPE and node.choice.get("recipe") == rid
    plan.set_recipe(nid, recipe, picks, ings, keep_children=keep)
    w.store.save(plan)
    return back_to(pid, nid)


@app.route("/plans/<pid>/node/<nid>/stop", methods=["POST"])
def stop_node(pid, nid):
    """Final is a statement about the item, not about the node you were on.

    So it applies to every other undecided place the item appears, and to the
    places it turns up later when another recipe is expanded.  `scope=here`
    keeps it to this node for the cases where that is really what you mean.
    """
    w = world()
    plan = get_plan(pid)
    everywhere = request.form.get("scope", "everywhere") != "here"
    plan.set_stop(nid, everywhere=everywhere)
    w.store.save(plan)
    nxt = request.form.get("next")
    if nxt == "stay":
        return redirect(url_for("node_view", pid=pid, nid=nid,
                                back=request.form.get("back") or None))
    return back_to(pid, nid)


@app.route("/plans/<pid>/node/<nid>/open", methods=["POST"])
def open_node(pid, nid):
    w = world()
    plan = get_plan(pid)
    node = plan.nodes.get(nid)
    if node is not None:
        # Reopening one occurrence lifts the plan-level "final everywhere" with
        # it; leaving it set would close this node again on the next solve, and
        # the button would look broken.
        plan.clear_final(node.ix)
    # `keep_open`, for the same reason one step over: this node is back on the
    # frontier because you said so, and the next decision about the item made
    # somewhere else must not quietly fill it in again.
    plan.clear_choice(nid, keep_open=True)
    w.store.save(plan)
    return back_to(pid, nid)


@app.route("/plans/<pid>/node/<nid>/useplan", methods=["POST"])
def use_plan(pid, nid):
    w = world()
    plan = get_plan(pid)
    plan.set_use_plan(nid, request.form.get("plan"))
    w.store.save(plan)
    return back_to(pid, nid)


@app.route("/plans/<pid>/node/<nid>/tool", methods=["POST"])
def set_tool(pid, nid):
    """Wear is per material -- which is why identity keys on PrimaryMaterial.

    Plan-level by default: you own one hammer, and charging a fraction of a
    steel one where the material was picked and a whole unspecified one
    everywhere else describes nothing.  `scope=here` keeps it to this recipe.
    """
    w = world()
    plan = get_plan(pid)
    node = plan.nodes.get(nid)
    if node is None:
        abort(404)
    everywhere = request.form.get("scope", "everywhere") != "here"
    plan.set_tool(node.ix, request.form.get("variant") or None,
                  everywhere=everywhere, nid=node.parent)
    w.store.save(plan)
    return redirect(url_for("node_view", pid=pid, nid=nid,
                            back=request.form.get("back") or None))


@app.route("/plans/<pid>/node/<nid>/designation", methods=["POST"])
def set_designation(pid, nid):
    """Guess from the dump, let the user correct.  Provenance is recorded so a
    re-export refreshes the derived designations and leaves these alone."""
    w = world()
    plan = get_plan(pid)
    node = plan.nodes.get(nid)
    obj = w.get(node.ix) if node else None
    if obj is None:
        abort(404)
    kind = request.form.get("kind") or None
    scope = request.form.get("scope") or "item"
    if kind == "auto":
        kind = None
    if scope == "recipe":
        parent = plan.nodes.get(node.parent) if node.parent else None
        rid = parent.choice.get("recipe") if parent is not None else None
        if rid:
            w.consumption.set_recipe_input(rid, obj.gid, kind)
    else:
        w.consumption.set_item(obj.gid, kind)
    return redirect(url_for("node_view", pid=pid, nid=nid,
                            back=request.form.get("back") or None))


# --------------------------------------------------------------------------
# Material re-pick -- `sweep` in miniature
# --------------------------------------------------------------------------

@app.route("/plans/<pid>/repick", methods=["GET", "POST"])
def repick(pid):
    w = world()
    plan = get_plan(pid)
    materials = {}
    for node in plan.nodes.values():
        o = w.get(node.ix)
        if o is not None and o.material and o.form:
            materials.setdefault(o.material, set()).add(o.form)
    if request.method == "POST":
        src = request.form.get("from")
        dst = (request.form.get("to") or "").strip()
        name = (request.form.get("name") or "%s (%s)" % (plan.name, dst)).strip()
        new, err = w.swap_material(plan, src, dst, name)
        if new is None:
            return render_template("repick.html", plan=plan,
                                   materials=sorted(materials),
                                   error=err, targets=w.swap_targets(plan))
        return redirect(url_for("plan_view", pid=new.id))
    return render_template("repick.html", plan=plan, materials=sorted(materials),
                           targets=w.swap_targets(plan), error=None)


# --------------------------------------------------------------------------
# Hand-entered recipes -- what makes a multiblock an MVP feature
# --------------------------------------------------------------------------

@app.route("/recipes", methods=["GET"])
def custom_recipes():
    w = world()
    return render_template("custom.html", rows=w.index.custom_rows(),
                           index=w.index)


@app.route("/recipes/new", methods=["POST"])
def new_custom_recipe():
    w = world()
    name = (request.form.get("name") or "").strip() or "hand-entered recipe"
    machine = (request.form.get("machine") or "").strip() or None
    note = (request.form.get("note") or "").strip()
    # The editor names its rows `in_ix:3` / `in_qty:3`, one pair per row, so the
    # row number joins the item to its quantity.
    inputs, outputs = [], []
    for key in request.form:
        if key.endswith("__text"):
            continue
        for ix_prefix, qty_prefix, bucket in (("in_ix:", "in_qty:", inputs),
                                              ("out_ix:", "out_qty:", outputs)):
            if not key.startswith(ix_prefix):
                continue
            ix = _ix(key)
            if not ix:
                continue
            row = key[len(ix_prefix):]
            q = request.form.get(qty_prefix + row) or "1"
            bucket.append({"ix": ix, "qty": max(1, int(float(q)))})
    makes = (request.form.get("makes") or "").strip()
    if not outputs and not makes:
        return redirect(url_for("custom_recipes", err="Say what the recipe makes."))
    if not inputs:
        return redirect(url_for("custom_recipes", err="Say what it is made from."))
    w.index.add_custom(name, machine, inputs, outputs, note, makes=makes or None)
    return redirect(url_for("custom_recipes"))


@app.route("/recipes/<path:rid>/delete", methods=["POST"])
def delete_custom_recipe(rid):
    world().index.delete_custom(rid)
    return redirect(url_for("custom_recipes"))


@app.route("/recipes/<path:rid>/clone", methods=["POST"])
def clone_custom_recipe(rid):
    """Clone a blueprint in another material -- the tank's iron/steel question."""
    w = world()
    dst = (request.form.get("material") or "").strip()
    _new_rid, err, _outs = w.clone_custom_in_material(rid, dst)
    return redirect(url_for("custom_recipes", err=err or None))


# --------------------------------------------------------------------------
# Search, icons, item detail
# --------------------------------------------------------------------------

@app.route("/search")
def search():
    w = world()
    q = request.args.get("q", "")
    kinds = ("item", "fluid")
    if request.args.get("kind") == "item":
        kinds = ("item",)
    hits = w.registry.search(q, limit=int(request.args.get("limit", 40)),
                             kinds=kinds)
    if request.args.get("fragment"):
        return render_template("_results.html", hits=hits,
                               field=request.args.get("field", "ix"),
                               target=request.args.get("target", ""))
    return jsonify([{"ix": o.ix, "name": o.name, "gid": o.gid,
                     "label": o.label} for o in hits])


@app.route("/icon")
def icon():
    """One member of `data/image.zip`, cached hard.

    The archive is a build artifact of a pack export, so a member's bytes never
    change while the app is running: the response is `immutable` and carries a
    strong ETag, which means a second visit to a 665-icon page asks the server
    for nothing at all.  `IconStore` explains why the zip handle is shared.
    """
    w = world()
    obj = w.get(request.args.get("ix", ""))
    data = None
    tag = BLANK_ETAG
    if obj is not None:
        data = w.icons.read(obj.image)
        if data is not None:
            tag = w.icons.etag(obj.image)
    if data is None:
        data = BLANK
    if request.if_none_match.contains(tag):
        resp = app.response_class(status=304)
    else:
        resp = app.response_class(data, mimetype="image/png")
    resp.headers["ETag"] = '"%s"' % tag
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


@app.route("/item")
def item_view():
    w = world()
    obj = w.get(request.args.get("ix", ""))
    if obj is None:
        abort(404)
    offer = w.offer(None, obj.ix)
    return render_template("item.html", it=obj,
                           choices=offer.choices,
                           hidden=offer.hidden,
                           designation=w.consumption.derived_for_item(obj),
                           tool_variants=w.tool_variants(obj.ix),
                           plans=w.store.plans_for(obj.ix))


def _ix(field="ix"):
    """The item the user picked, or the one their typed text names.

    Picking from the type-ahead fills a hidden field; typing a name and hitting
    enter does not. Falling back to the best search hit means the obvious
    gesture works instead of silently creating a plan with no root.
    """
    w = world()
    ix = (request.form.get(field) or "").strip()
    if ix and w.get(ix):
        return ix
    text = (request.form.get(field + "__text") or "").strip()
    if not text:
        return None
    hits = w.registry.search(text, limit=1)
    return hits[0].ix if hits else None


def _qty(raw, default):
    try:
        v = Fraction(str(raw).strip())
        return v if v > 0 else Fraction(default)
    except Exception:
        return Fraction(default)


def main():
    ap = argparse.ArgumentParser(description="GTNH raw-material cost model")
    ap.add_argument("--port", type=int, default=5057)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    print("loading the dump ...")
    world()

    # A note so nobody spends an afternoon on it twice: **this server cannot do
    # keep-alive.**  `threaded=True` already puts Werkzeug on HTTP/1.1, but
    # `serving.py` then sends `Connection: close` unconditionally, because
    # Python's `http.server` cannot drain a request body before the next request
    # line.  So a page with 665 icons really does open 665 connections and spawn
    # 665 threads, and `netstat` really does show a wall of TIME_WAIT.
    #
    # That turned out not to be the expensive part.  A connection costs about
    # 3 ms; what cost 0.8 s per icon was each of those threads reopening
    # `image.zip`, which `IconStore` now does once for the process.  Icons are
    # `immutable` with a strong ETag on top, so most of the requests never
    # happen twice.  Beyond that the fix is a different WSGI server, and this is
    # a local single-user app -- not worth the dependency.
    url = "http://%s:%d/" % (a.host, a.port)
    print("\n  GTNH cost model ready at %s\n" % url)
    if not a.no_browser and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        import threading
        import webbrowser
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    app.run(host=a.host, port=a.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
