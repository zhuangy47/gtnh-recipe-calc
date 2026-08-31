#!/usr/bin/env python3
"""Convert an HSQLDB .script replay file (NESQL export) into a SQLite database.

The .script file is a line-oriented replay log: a DDL prologue followed by one
INSERT statement per row.  HSQLDB and SQLite agree on value-literal syntax
(single-quoted strings with '' doubling, NULL, TRUE/FALSE, C-style numerics),
so the INSERT statements are replayed verbatim and only the DDL is translated.
Replaying rather than re-parsing means row values are never round-tripped
through Python, so nothing can be silently mangled in the middle.

Usage: python nesql_to_sqlite.py <in.script> <out.sqlite> [--keep]
       --keep  overwrite an existing output file instead of refusing
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
import time
from collections import Counter

CHUNK_BYTES = 16 * 1024 * 1024

# Indexes the dump does not declare but every recipe-graph query needs.
# Named NESQL_* so they are visibly ours and not part of the export.
EXTRA_INDEXES = [
    # identity: the (mod, internal name, damage) interning key
    "CREATE INDEX NESQL_IDX_ITEM_IDENT ON ITEM(MOD_ID,INTERNAL_NAME,ITEM_DAMAGE)",
    "CREATE INDEX NESQL_IDX_ITEM_INTERNAL ON ITEM(INTERNAL_NAME)",
    "CREATE INDEX NESQL_IDX_ITEM_UNLOCALIZED ON ITEM(UNLOCALIZED_NAME)",
    # backward walk: which recipes produce this item / fluid
    "CREATE INDEX NESQL_IDX_OUT_ITEM ON RECIPE_ITEM_OUTPUTS(ITEM_OUTPUTS_VALUE_ITEM_ID)",
    "CREATE INDEX NESQL_IDX_OUT_FLUID ON RECIPE_FLUID_OUTPUTS(FLUID_OUTPUTS_VALUE_FLUID_ID)",
    # set-valued input slots: group -> members, member -> groups
    "CREATE INDEX NESQL_IDX_IGIS_GROUP ON ITEM_GROUP_ITEM_STACKS(ITEM_GROUP_ID)",
    "CREATE INDEX NESQL_IDX_IGIS_ITEM ON ITEM_GROUP_ITEM_STACKS(ITEM_STACKS_ITEM_ID)",
    "CREATE INDEX NESQL_IDX_RIG_GROUP ON RECIPE_ITEM_GROUP(ITEM_INPUTS_ID)",
    "CREATE INDEX NESQL_IDX_RFG_GROUP ON RECIPE_FLUID_GROUP(FLUID_INPUTS_ID)",
    # oredict
    "CREATE INDEX NESQL_IDX_ORE_NAME ON ORE_DICTIONARY(NAME)",
    "CREATE INDEX NESQL_IDX_ORE_GROUP ON ORE_DICTIONARY(ITEM_GROUP_ID)",
    # GT recipe side-table joins
    "CREATE INDEX NESQL_IDX_GTR_RECIPE ON GREG_TECH_RECIPE(RECIPE_ID)",
    "CREATE INDEX NESQL_IDX_GTRM_RECIPE ON GREG_TECH_RECIPE_METADATA(GREG_TECH_RECIPE_ID)",
    "CREATE INDEX NESQL_IDX_FLUID_INTERNAL ON FLUID(INTERNAL_NAME)",
]


def split_top_level(s):
    """Split on commas that are outside parens and outside string literals."""
    parts, depth, start, in_str = [], 0, 0, False
    for i, ch in enumerate(s):
        if in_str:
            if ch == "'":
                in_str = False
        elif ch == "'":
            in_str = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(s[start:i])
            start = i + 1
    parts.append(s[start:])
    return parts


def sqlite_type(t):
    u = t.upper()
    if u.startswith(("VARCHAR", "CHAR", "CLOB", "LONGVARCHAR")):
        return "TEXT"
    if u in ("INTEGER", "INT", "SMALLINT", "TINYINT", "BIGINT"):
        return "INTEGER"
    if u.startswith(("DECIMAL", "NUMERIC")):
        return "NUMERIC"
    if u in ("DOUBLE", "FLOAT", "REAL"):
        return "REAL"
    if u == "BOOLEAN":
        return "INTEGER"          # SQLite has no BOOLEAN; TRUE/FALSE store as 1/0
    if u.startswith(("VARBINARY", "BINARY", "BLOB", "BIT")):
        return "BLOB"
    return "TEXT"


TABLE_CONSTRAINT = re.compile(
    r"^\s*(CONSTRAINT\s+\w+\s+)?(PRIMARY\s+KEY|FOREIGN\s+KEY|UNIQUE|CHECK)\b", re.I
)
COLDEF = re.compile(r"^(\w+)\s+([A-Za-z]+(?:\([^)]*\))?)\s*(.*)$", re.S)


class Table:
    def __init__(self, name):
        self.name = name
        self.columns = []       # (name, type, flags)
        self.constraints = []

    def ddl(self):
        body = [f'  {n} {t}{(" " + f) if f else ""}' for n, t, f in self.columns]
        body += [f"  {c}" for c in self.constraints]
        return f"CREATE TABLE {self.name} (\n" + ",\n".join(body) + "\n)"


def parse_ddl(lines):
    """Translate the HSQLDB DDL prologue into SQLite DDL.

    ALTER TABLE ... ADD CONSTRAINT is folded into the CREATE TABLE, because
    SQLite cannot add a constraint after the fact.  SQLite resolves foreign-key
    targets lazily, so forward references are fine.
    """
    tables = {}
    indexes = []
    for ln in lines:
        m = re.match(r"^CREATE (?:MEMORY|CACHED|TEXT) TABLE PUBLIC\.(\w+)\((.*)\)$", ln)
        if m:
            tbl = Table(m.group(1))
            for part in split_top_level(m.group(2)):
                part = part.strip()
                if TABLE_CONSTRAINT.match(part):
                    tbl.constraints.append(part.replace("PUBLIC.", ""))
                    continue
                cm = COLDEF.match(part)
                if not cm:
                    raise SystemExit(f"unparsed column definition in {tbl.name}: {part!r}")
                tbl.columns.append(
                    (cm.group(1), sqlite_type(cm.group(2)), cm.group(3).strip())
                )
            tables[tbl.name] = tbl
            continue
        m = re.match(r"^CREATE (UNIQUE )?INDEX (\w+) ON PUBLIC\.(\w+)\((.*)\)$", ln)
        if m:
            uniq = m.group(1) or ""
            indexes.append(
                f"CREATE {uniq}INDEX {m.group(2)} ON {m.group(3)}({m.group(4)})"
            )
            continue
        m = re.match(r"^ALTER TABLE PUBLIC\.(\w+) ADD (CONSTRAINT .*)$", ln)
        if m:
            tables[m.group(1)].constraints.append(m.group(2).replace("PUBLIC.", ""))
            continue
    return tables, indexes


def statements(path):
    """Yield (schema, table, sql) for each statement in the data section.

    A statement starts at a line beginning with INSERT INTO or SET; anything
    else is a continuation (a string literal holding a raw newline).  This dump
    has none, but handling it costs nothing and an unhandled one would corrupt
    data silently.
    """
    schema = "PUBLIC"
    buf = []
    table = None
    with open(path, "r", encoding="ascii", newline="") as fh:
        for raw in fh:
            ln = raw.rstrip("\r\n")
            starts = ln.startswith("INSERT INTO ") or ln.startswith("SET ")
            if starts and buf:
                yield schema, table, "\n".join(buf) + ";"
                buf = []
            if starts:
                if ln.startswith("SET "):
                    sm = re.match(r"^SET SCHEMA (\w+)$", ln)
                    if sm:
                        schema = sm.group(1)
                    table = None
                    continue
                table = ln.split(" ", 3)[2]
                buf = [ln]
            elif buf:
                buf.append(ln)
    if buf:
        yield schema, table, "\n".join(buf) + ";"


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    src, dst = sys.argv[1], sys.argv[2]
    if os.path.exists(dst):
        if "--keep" not in sys.argv:
            print(f"refusing to overwrite {dst} (pass --keep)", file=sys.stderr)
            return 2
        os.remove(dst)

    t0 = time.time()

    # --- DDL: the prologue is everything before the first INSERT ---------
    ddl_lines = []
    with open(src, "r", encoding="ascii", newline="") as fh:
        for raw in fh:
            ln = raw.rstrip("\r\n")
            if ln.startswith("INSERT INTO "):
                break
            ddl_lines.append(ln)
    tables, indexes = parse_ddl(ddl_lines)
    print(f"schema: {len(tables)} tables, {len(indexes)} declared indexes", flush=True)

    conn = sqlite3.connect(dst, isolation_level=None)
    conn.execute("PRAGMA page_size = 8192")
    conn.execute("PRAGMA journal_mode = OFF")
    conn.execute("PRAGMA synchronous = OFF")
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA cache_size = -262144")     # 256 MB
    for t in tables.values():
        conn.execute(t.ddl())

    # --- data: replay the INSERTs verbatim, in chunks --------------------
    expected = Counter()
    chunk = []
    state = {"size": 0}
    nstmt = 0

    def flush():
        if not chunk:
            return
        script = "BEGIN;\n" + "\n".join(chunk) + "\nCOMMIT;"
        try:
            conn.executescript(script)
        except sqlite3.Error:
            conn.execute("ROLLBACK")
            conn.execute("BEGIN")
            for s in chunk:                      # isolate the offender
                try:
                    conn.execute(s)
                except sqlite3.Error as e:
                    conn.execute("ROLLBACK")
                    raise SystemExit(f"failed statement: {e}\n{s[:500]}")
            conn.execute("COMMIT")
        chunk.clear()
        state["size"] = 0

    for schema, table, sql in statements(src):
        if schema != "PUBLIC" or table not in tables:
            continue                              # SYSTEM_LOBS bookkeeping
        expected[table] += 1
        nstmt += 1
        chunk.append(sql)
        state["size"] += len(sql)
        if state["size"] >= CHUNK_BYTES:
            flush()
            print(f"  {nstmt:>9,} rows  {time.time() - t0:6.1f}s", flush=True)
    flush()
    print(f"loaded {nstmt:,} rows in {time.time() - t0:.1f}s", flush=True)

    # --- verify before indexing -----------------------------------------
    bad = []
    for name in tables:
        got = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
        if got != expected.get(name, 0):
            bad.append((name, expected.get(name, 0), got))
    if bad:
        for name, exp, got in bad:
            print(f"MISMATCH {name}: expected {exp}, got {got}", file=sys.stderr)
        return 1
    print("row counts match the source for every table", flush=True)

    # --- indexes ---------------------------------------------------------
    for stmt in indexes + EXTRA_INDEXES:
        conn.execute(stmt)
    print(
        f"built {len(indexes)} declared + {len(EXTRA_INDEXES)} added indexes "
        f"({time.time() - t0:.1f}s)",
        flush=True,
    )

    conn.execute("ANALYZE")
    conn.execute("PRAGMA journal_mode = DELETE")
    conn.close()

    mb = os.path.getsize(dst) / 1e6
    print(f"done: {dst}  {mb:,.0f} MB  {time.time() - t0:.1f}s total")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
