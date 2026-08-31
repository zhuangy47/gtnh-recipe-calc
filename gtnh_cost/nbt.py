"""Tolerant NBT-text parser and the canonicalisation §14 step 1 asks for.

The dump stores NBT as Minecraft's printed form:

    {}                        {energy:0L}
    {CustomFlaskEffects:[0:{concentration:0,durationFactor:2}]}
    {GT.ToolStats:{SecondaryMaterial:"Wood",PrimaryMaterial:"Steel",MaxDamage:51200L}}

Two items whose NBT differs only in a numeric type suffix (`0L` vs `0`) or in a
tool's current `Damage` are the same item for costing purposes, and they arrive
with different `sha1(nbt)` discriminators in their ids, so they must be interned
together.  §14 step 1: "drop tool `Damage`, keep `PrimaryMaterial`".

Parsing is deliberately forgiving: anything this parser cannot read falls back to
the raw string, which keeps such items *distinct* rather than merging them by
accident.  Failing closed is the safe direction -- a missed merge shows up as two
similar rows in a choice list, a wrong merge silently mixes two items' recipes.
"""
from __future__ import annotations

# Keys dropped anywhere in the tree.  `Damage` is a tool's *current* wear, which
# is state, not identity.  `MaxDamage` is identity (it is the tool's material)
# and is a different key, so an exact-name test is enough.
DROP_KEYS = frozenset({"Damage"})

_NUM_SUFFIX = "LlBbSsFfDd"


class _P:
    __slots__ = ("s", "i")

    def __init__(self, s: str) -> None:
        self.s = s
        self.i = 0

    def error(self, why: str):
        raise ValueError(f"{why} at {self.i} in {self.s[:60]!r}")

    def peek(self) -> str:
        return self.s[self.i] if self.i < len(self.s) else ""

    def skip_ws(self) -> None:
        while self.i < len(self.s) and self.s[self.i] in " \t\r\n":
            self.i += 1

    def value(self):
        self.skip_ws()
        c = self.peek()
        if c == "{":
            return self.compound()
        if c == "[":
            return self.listlike()
        if c == '"' or c == "'":
            return self.string()
        return self.scalar()

    def compound(self):
        self.i += 1  # {
        out = {}
        self.skip_ws()
        if self.peek() == "}":
            self.i += 1
            return out
        while True:
            self.skip_ws()
            key = self.key()
            self.skip_ws()
            if self.peek() != ":":
                self.error("expected ':'")
            self.i += 1
            out[key] = self.value()
            self.skip_ws()
            c = self.peek()
            if c == ",":
                self.i += 1
                continue
            if c == "}":
                self.i += 1
                return out
            self.error("expected ',' or '}'")

    def listlike(self):
        """Both `[0:{..},1:{..}]` (printed list) and `[1,2,3]` (array)."""
        self.i += 1  # [
        out = []
        self.skip_ws()
        if self.peek() == "]":
            self.i += 1
            return out
        # `[I;1,2,3]` / `[B;..]` array-type marker
        if len(self.s) > self.i + 1 and self.s[self.i + 1] == ";":
            self.i += 2
        while True:
            self.skip_ws()
            start = self.i
            if self.peek() not in "{[\"'":
                # possible `index:` prefix
                j = self.i
                while j < len(self.s) and self.s[j].isdigit():
                    j += 1
                if j > self.i and j < len(self.s) and self.s[j] == ":":
                    self.i = j + 1
                else:
                    self.i = start
            out.append(self.value())
            self.skip_ws()
            c = self.peek()
            if c == ",":
                self.i += 1
                continue
            if c == "]":
                self.i += 1
                return out
            self.error("expected ',' or ']'")

    def key(self) -> str:
        c = self.peek()
        if c in "\"'":
            return self.string()
        start = self.i
        while self.i < len(self.s) and self.s[self.i] not in ":,{}[]":
            self.i += 1
        if self.i == start:
            self.error("empty key")
        return self.s[start:self.i].strip()

    def string(self) -> str:
        quote = self.s[self.i]
        self.i += 1
        buf = []
        while self.i < len(self.s):
            c = self.s[self.i]
            if c == "\\" and self.i + 1 < len(self.s):
                buf.append(self.s[self.i + 1])
                self.i += 2
                continue
            if c == quote:
                self.i += 1
                return "".join(buf)
            buf.append(c)
            self.i += 1
        self.error("unterminated string")

    def scalar(self):
        start = self.i
        while self.i < len(self.s) and self.s[self.i] not in ",}]":
            self.i += 1
        raw = self.s[start:self.i].strip()
        if not raw:
            self.error("empty scalar")
        body = raw[:-1] if raw[-1] in _NUM_SUFFIX else raw
        try:
            return int(body)
        except ValueError:
            pass
        try:
            return float(body)
        except ValueError:
            pass
        return raw


def parse(text: str):
    """Parse printed NBT.  Raises ValueError on anything unrecognised."""
    p = _P(text)
    v = p.value()
    p.skip_ws()
    if p.i != len(p.s):
        p.error("trailing input")
    return v


def _canon(v):
    if isinstance(v, dict):
        return tuple(sorted((k, _canon(x)) for k, x in v.items() if k not in DROP_KEYS))
    if isinstance(v, list):
        return tuple(_canon(x) for x in v)
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


def canonical(text: str) -> str:
    """A stable discriminator for an item's NBT.

    Equal return values mean "intern these two rows as one item".  Empty NBT and
    an empty compound both canonicalise to ``""``, so `{}` and `''` merge.
    Unparseable NBT canonicalises to its own raw text, keeping it distinct.
    """
    text = (text or "").strip()
    if not text:
        return ""
    try:
        v = parse(text)
    except (ValueError, RecursionError, IndexError):
        return "raw:" + text
    c = _canon(v)
    return "" if c == () else repr(c)


def get_path(text: str, *path: str):
    """Read one value out of printed NBT, or None.  Used for GT.ToolStats."""
    try:
        v = parse(text or "")
    except (ValueError, RecursionError, IndexError):
        return None
    for key in path:
        if not isinstance(v, dict) or key not in v:
            return None
        v = v[key]
    return v
