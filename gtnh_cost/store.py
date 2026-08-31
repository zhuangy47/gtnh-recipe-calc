"""Plan persistence, and the `PlanIndex` that makes `UsePlan` reachable.

`PlanIndex : ix -> [PlanId]` sits beside the store.  It is what answers "is
there already a plan for this block?" when a node is opened -- the prompt without
which `UsePlan` exists and nothing ever offers it.

Plans are JSON files under `plans/`, one per plan, named by id.  They are private
working documents on the user's own disk; there is nothing to serve.
"""
from __future__ import annotations

import json
import os
import re
import uuid

from .plan import Plan

PLAN_DIR = "plans"


class PlanStore:
    def __init__(self, path=PLAN_DIR):
        self.path = path
        os.makedirs(path, exist_ok=True)
        self._cache = {}
        self._mtimes = {}

    def _file(self, pid):
        safe = re.sub(r"[^A-Za-z0-9~_-]", "_", pid)
        return os.path.join(self.path, safe + ".json")

    def ids(self):
        out = []
        for name in os.listdir(self.path):
            if name.endswith(".json"):
                out.append(name[:-5])
        return out

    def all(self):
        plans = [self.get(pid) for pid in self.ids()]
        plans = [p for p in plans if p is not None]
        plans.sort(key=lambda p: -p.updated)
        return plans

    def get(self, pid):
        if not pid:
            return None
        f = self._file(pid)
        if not os.path.exists(f):
            return None
        mtime = os.path.getmtime(f)
        if self._mtimes.get(pid) == mtime and pid in self._cache:
            return self._cache[pid]
        with open(f, "r", encoding="utf-8") as fh:
            plan = Plan.from_json(json.load(fh))
        self._cache[pid] = plan
        self._mtimes[pid] = mtime
        return plan

    def save(self, plan):
        # Collect anything nothing points at, so a plan file cannot accumulate
        # dead branches.  See `Plan.prune`.
        plan.prune()
        plan.touch()
        f = self._file(plan.id)
        tmp = f + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(plan.to_json(), fh, indent=1)
        os.replace(tmp, f)
        self._cache[plan.id] = plan
        self._mtimes[plan.id] = os.path.getmtime(f)

    def delete(self, pid):
        f = self._file(pid)
        if os.path.exists(f):
            os.remove(f)
        self._cache.pop(pid, None)
        self._mtimes.pop(pid, None)

    def duplicate(self, pid, new_name):
        src = self.get(pid)
        if src is None:
            return None
        copy = Plan.from_json(src.to_json())
        copy.id = "p~" + uuid.uuid4().hex[:12]
        copy.name = new_name
        self.save(copy)
        return copy

    # -- the index --------------------------------------------------------
    def index(self):
        """ix -> [Plan], over every plan's roots."""
        out = {}
        for p in self.all():
            for r in p.roots:
                node = p.nodes.get(r["node"])
                if node is not None:
                    out.setdefault(node.ix, []).append(p)
        return out

    def plans_for(self, ix, exclude=None):
        return [p for p in self.index().get(ix, []) if p.id != exclude]
