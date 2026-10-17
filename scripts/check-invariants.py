#!/usr/bin/env python3
"""Mechanical server-invariant checks (AST based, low false-positive).

Every rule traces to docs/server-invariants.md (source of truth, lands with
PR #2) and is explained in docs/agent-pitfalls.md. Until that doc is on main
the rule text lives in the AGENTS.md "写代码" bullets added by #2.

Rules (id -> invariant):
  INV-SEQ-LAST       lock order: allocate_seq is the last lock in the write
                     transaction; no business writes after it, never nested
                     inside an UPDATE that also writes business columns.
  INV-NUL            every external string input rejects NUL: FastAPI path
                     params must use NoNulStr, not bare str.
  INV-NO-FALLBACK    no silent fallback to Mock*/InMemory* implementations:
                     construction must sit under an explicit `== "mock"` /
                     `== "inmemory"` (or similar) selector branch.
  INV-STARTUP-DDL    (warning only, proposed) schema DDL (create_all) only in
                     a one-shot bootstrap/migration entrypoint, never in the
                     API lifespan or worker startup.

Suppress a single finding with a trailing comment on the flagged line:
    # invariant-ok: <reason>
Exit 0 when server/ does not exist (e.g. main before PR #2).
"""
from __future__ import annotations

import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

DOC = "docs/server-invariants.md"
SEQ_FUNC = "allocate_seq"
SEQ_COLUMNS = {"change_seq", "updated_at"}
WRITE_CALLS = {"enqueue", "add", "add_all", "insert", "merge", "with_for_update", "delete"}
FALLBACK_PREFIXES = ("Mock", "InMemory", "Fake")
SELECTOR_WORDS = ("mock", "inmemory", "fake", "test")
DDL_ALLOWED_FILES = ("cli.py", "bootstrap.py", "migrate.py")
DDL_ALLOWED_DIRS = ("migrations", "alembic")
HTTP_METHODS = {"get", "post", "put", "patch", "delete", "api_route"}
OK_MARK = re.compile(r"#\s*invariant-ok:\s*\S")


@dataclass
class Finding:
    path: str
    line: int
    rule: str
    msg: str
    warning: bool = False

    def __str__(self) -> str:
        kind = "warning" if self.warning else "error"
        return f"{self.path}:{self.line}: {kind} [{self.rule}] {self.msg} (see {DOC} / docs/agent-pitfalls.md#{self.rule.lower()})"


def _name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _call_name(node: ast.Call) -> str | None:
    return _name(node.func)


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    out: dict[ast.AST, ast.AST] = {}
    for p in ast.walk(tree):
        for c in ast.iter_child_nodes(p):
            out[c] = p
    return out


def _pos(n: ast.AST) -> tuple[int, int]:
    return (getattr(n, "lineno", 0), getattr(n, "col_offset", 0))


# ---------------------------------------------------------------- INV-SEQ-LAST
def check_seq_last(tree: ast.AST, path: str, parents) -> list[Finding]:
    out: list[Finding] = []
    funcs = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for fn in funcs:
        if fn.name == SEQ_FUNC:
            continue
        calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)]
        # ignore calls belonging to nested functions
        calls = [c for c in calls if _enclosing_func(c, parents) is fn]
        seq_calls = [c for c in calls if _call_name(c) == SEQ_FUNC]
        if not seq_calls:
            continue
        # nested inside .values(...) together with business columns
        for sc in seq_calls:
            p = parents.get(sc)
            while p is not None and p is not fn:
                if isinstance(p, ast.Call) and _call_name(p) == "values":
                    keys = {k.arg for k in p.keywords if k.arg}
                    extra = sorted(keys - SEQ_COLUMNS)
                    if extra:
                        out.append(Finding(path, sc.lineno, "INV-SEQ-LAST",
                            f"{SEQ_FUNC}() evaluated inside an UPDATE that also writes {extra}: "
                            "the sequence lock is taken before the business row lock (AB-BA deadlock on PG). "
                            "Write business rows first, then allocate_seq, then a second UPDATE for change_seq."))
                    break
                p = parents.get(p)
        first = min(seq_calls, key=_pos)
        # loop back-edge: a write in a loop body that also allocates runs after
        # the previous iteration's allocate_seq
        loop_bodies = [lp for sc in seq_calls for lp in [_enclosing_loop(sc, fn, parents)] if lp is not None]
        for c in calls:
            if c in seq_calls or any(_inside(c, sc, parents) for sc in seq_calls):
                continue
            in_loop = any(_inside(c, lp, parents) for lp in loop_bodies)
            if _pos(c) <= _pos(first) and not in_loop:
                continue
            name = _call_name(c)
            bad = None
            if name in WRITE_CALLS or (name or "").startswith("enqueue"):
                bad = f"{name}()"
            elif name == "values":
                extra = sorted({k.arg for k in c.keywords if k.arg} - SEQ_COLUMNS)
                if extra or any(k.arg is None for k in c.keywords) or c.args:
                    bad = f"UPDATE .values({', '.join(extra) or '...'})"
            if bad:
                out.append(Finding(path, c.lineno, "INV-SEQ-LAST",
                    f"{bad} after {SEQ_FUNC}() (line {first.lineno}) in {fn.name}(): allocate_seq must be the "
                    "last lock before commit; only change_seq/updated_at may be written after it."))
        # ORM attribute writes (`note.text = v`) are flushed later and lock the row after the seq lock
        for st in ast.walk(fn):
            if not isinstance(st, (ast.Assign, ast.AugAssign, ast.AnnAssign)) or _enclosing_func(st, parents) is not fn:
                continue
            targets = st.targets if isinstance(st, ast.Assign) else [st.target]
            attrs = [t.attr for t in targets if isinstance(t, ast.Attribute) and t.attr not in SEQ_COLUMNS]
            if not attrs or any(_inside(sc, st, parents) for sc in seq_calls):
                continue
            in_loop = any(_inside(st, lp, parents) for lp in loop_bodies)
            if _pos(st) <= _pos(first) and not in_loop:
                continue
            out.append(Finding(path, st.lineno, "INV-SEQ-LAST",
                f"ORM attribute write .{attrs[0]} after {SEQ_FUNC}() (line {first.lineno}) in {fn.name}(): it is flushed "
                "(row-locked) after the sequence lock; write business columns before allocate_seq."))
    return out


def _enclosing_loop(n, fn, parents):
    p = parents.get(n)
    while p is not None and p is not fn:
        if isinstance(p, (ast.For, ast.AsyncFor, ast.While)):
            return p
        p = parents.get(p)
    return None


def _enclosing_func(n, parents):
    p = parents.get(n)
    while p is not None and not isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        p = parents.get(p)
    return p


def _inside(n, anc, parents) -> bool:
    p = parents.get(n)
    while p is not None:
        if p is anc:
            return True
        p = parents.get(p)
    return False


# ---------------------------------------------------------------- INV-NUL
def _router_prefixes(tree: ast.AST) -> dict[str, str]:
    """`router = APIRouter(prefix="/users/{user_id}")` -> {"router": "/users/{user_id}"}."""
    out: dict[str, str] = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call) and _call_name(n.value) in {"APIRouter", "FastAPI"}:
            for k in n.value.keywords:
                if k.arg == "prefix" and isinstance(k.value, ast.Constant) and isinstance(k.value.value, str):
                    for t in n.targets:
                        if isinstance(t, ast.Name):
                            out[t.id] = k.value.value
    return out


def _route_path(dec: ast.AST, prefixes: dict[str, str] | None = None) -> str | None:
    if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and dec.func.attr in HTTP_METHODS:
        prefix = (prefixes or {}).get(_name(dec.func.value) or "", "")
        if dec.args and isinstance(dec.args[0], ast.Constant) and isinstance(dec.args[0].value, str):
            return prefix + dec.args[0].value
        for k in dec.keywords:
            if k.arg == "path" and isinstance(k.value, ast.Constant):
                return prefix + k.value.value
        return prefix or None
    return None


def _is_bare_str(ann: ast.AST | None) -> bool:
    if ann is None:
        return True  # FastAPI treats unannotated path params as str
    if isinstance(ann, ast.Name) and ann.id == "str":
        return True
    if isinstance(ann, ast.Constant) and ann.value == "str":
        return True
    if isinstance(ann, ast.BinOp) and isinstance(ann.op, ast.BitOr):  # str | None
        return _is_bare_str(ann.left) or _is_bare_str(ann.right)
    if isinstance(ann, ast.Subscript) and _name(ann.value) in {"Optional", "Union"}:
        sl = ann.slice
        return any(_is_bare_str(e) for e in (sl.elts if isinstance(sl, ast.Tuple) else [sl]))
    if isinstance(ann, ast.Subscript) and _name(ann.value) == "Annotated":
        sl = ann.slice
        first = sl.elts[0] if isinstance(sl, ast.Tuple) else sl
        if _is_bare_str(first):
            # Annotated[str, AfterValidator(reject_nul)] is fine
            return "reject_nul" not in ast.unparse(sl)
    return False


def check_nul(tree: ast.AST, path: str, parents) -> list[Finding]:
    out = []
    prefixes = _router_prefixes(tree)
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        params: set[str] = set()
        for d in fn.decorator_list:
            rp = _route_path(d, prefixes)
            if rp:
                params |= {m.split(":")[0] for m in re.findall(r"{([^}]+)}", rp)}
        if not params:
            continue
        for a in fn.args.args + fn.args.kwonlyargs:
            if a.arg in params and _is_bare_str(a.annotation):
                out.append(Finding(path, a.lineno, "INV-NUL",
                    f"path parameter '{a.arg}' of {fn.name}() is a bare str: use NoNulStr so '%00' is a 422, not a PG 500."))
    return out


# ---------------------------------------------------------------- INV-NO-FALLBACK
def _is_fake_literal(n: ast.AST) -> bool:
    vals = []
    if isinstance(n, ast.Constant) and isinstance(n.value, str):
        vals = [n.value]
    elif isinstance(n, (ast.Tuple, ast.List, ast.Set)):
        if not n.elts or not all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in n.elts):
            return False
        vals = [e.value for e in n.elts]
    return bool(vals) and all(v.lower() in SELECTOR_WORDS for v in vals)


def _selects_explicitly(test: ast.AST) -> bool:
    """True only if the condition being true *implies* a fake was selected:
    `x == "mock"`, `x in ("mock", "fake")`, or an `and` with such a term.
    `or`, negations, `!=`, `not in` and substrings never qualify."""
    if isinstance(test, ast.BoolOp):
        if isinstance(test.op, ast.And):
            return any(_selects_explicitly(v) for v in test.values)
        return all(_selects_explicitly(v) for v in test.values)  # Or: every branch
    if isinstance(test, ast.Compare) and len(test.ops) == 1:
        op, left, right = test.ops[0], test.left, test.comparators[0]
        if isinstance(op, ast.Eq):
            return _is_fake_literal(left) or _is_fake_literal(right)
        if isinstance(op, ast.In):
            return _is_fake_literal(right)
    return False


def check_fallback(tree: ast.AST, path: str, parents) -> list[Finding]:
    out = []
    for c in ast.walk(tree):
        if not isinstance(c, ast.Call):
            continue
        name = _call_name(c) or ""
        if not name.startswith(FALLBACK_PREFIXES) or not name[:1].isupper():
            continue
        fn = _enclosing_func(c, parents)
        if fn is None or isinstance(fn, ast.Lambda) or any(w in fn.name.lower() for w in SELECTOR_WORDS):
            continue
        ok = False
        child, p = c, parents.get(c)
        while p is not None and p is not fn:
            if isinstance(p, ast.If) and child in p.body and _selects_explicitly(p.test):
                ok = True
                break
            child, p = p, parents.get(p)
        if not ok and _guarded_by_earlier_raise(c, fn):
            ok = True
        if not ok:
            out.append(Finding(path, c.lineno, "INV-NO-FALLBACK",
                f"{name}() is reachable without an explicit mock/inmemory selector in {fn.name}(): "
                "a misconfigured prod would silently run the fake. Select it only on an explicit value and raise otherwise."))
    return out


def _guarded_by_earlier_raise(c: ast.Call, fn) -> bool:
    """Fall-through construction after a top-level `if <is production>: raise` guard."""
    for st in fn.body:
        if getattr(st, "lineno", 0) >= c.lineno:
            break
        # Only an unconditional prod/PostgreSQL rejection counts: a guard that
        # merely mentions "mock" (e.g. `provider not in {"openai", "mock"}`)
        # does not stop a later fall-through from silently picking the fake.
        if isinstance(st, ast.If) and not st.orelse and any(isinstance(n, ast.Raise) for n in st.body):
            t = ast.unparse(st.test).lower()
            if "prod" in t or "postgres" in t:
                return True
    return False


# ---------------------------------------------------------------- INV-STARTUP-DDL
DDL_NAMES = {"create_all", "drop_all"}


def ddl_helpers(tree: ast.AST) -> set[str]:
    """Functions whose body references create_all/drop_all (called or passed, e.g. run_sync(Base.metadata.create_all))."""
    out = set()
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # only real DDL (`<x>.metadata.create_all`), not callers of a wrapper
            if any(
                isinstance(n, ast.Attribute) and n.attr in DDL_NAMES
                and isinstance(n.value, ast.Attribute) and n.value.attr == "metadata"
                for n in ast.walk(fn)
            ):
                out.add(fn.name)
    return out


def check_ddl(tree: ast.AST, path: str, parents, helpers: set[str] | None = None) -> list[Finding]:
    p = Path(path)
    if p.name in DDL_ALLOWED_FILES or any(d in p.parts for d in DDL_ALLOWED_DIRS):
        return []
    names = DDL_NAMES | (helpers or set()) | ddl_helpers(tree)
    out = []
    for c in ast.walk(tree):
        if isinstance(c, ast.Call) and _call_name(c) in names:
            fn = _enclosing_func(c, parents)
            if fn is not None and not isinstance(fn, ast.Lambda) and fn.name in names and not _is_startup(fn):
                continue  # inside a DDL helper definition: flag its callers instead
            out.append(Finding(path, c.lineno, "INV-STARTUP-DDL",
                "schema DDL at process startup: API workers and the worker service race on create_all. "
                "Run it once in a bootstrap/migration step (e.g. app/cli.py) before starting services.", warning=True))
    # DDL passed as a callable directly inside a startup entrypoint
    # (lifespan: `await conn.run_sync(Base.metadata.create_all)`)
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and _is_startup(fn) and fn.name in ddl_helpers(fn):
            for n in ast.walk(fn):
                if isinstance(n, ast.Attribute) and n.attr in DDL_NAMES and isinstance(n.value, ast.Attribute) and n.value.attr == "metadata":
                    out.append(Finding(path, n.lineno, "INV-STARTUP-DDL",
                        f"metadata.{n.attr} inside startup entrypoint {fn.name}(): every API/worker process runs it. "
                        "Run DDL once in a bootstrap/migration step (e.g. app/cli.py).", warning=True))
    return out


STARTUP_NAME = re.compile(r"lifespan|startup|worker_main|run_worker", re.I)


def _is_startup(fn) -> bool:
    if STARTUP_NAME.search(fn.name):
        return True
    return any("on_event" in ast.unparse(d) or "asynccontextmanager" in ast.unparse(d) for d in fn.decorator_list)


CHECKS = (check_seq_last, check_nul, check_fallback)  # check_ddl needs cross-module helpers


def is_test_file(p: Path) -> bool:
    return "tests" in p.parts or p.name.startswith("test_") or p.name.endswith("_test.py") or p.name == "conftest.py"


def check_source(src: str, path: str, helpers: set[str] | None = None) -> list[Finding]:
    tree = ast.parse(src, filename=path)
    parents = _parents(tree)
    lines = src.splitlines()
    found = []
    results = [f for chk in CHECKS for f in chk(tree, path, parents)]
    results += check_ddl(tree, path, parents, helpers)
    for f in results:
        line = lines[f.line - 1] if 0 < f.line <= len(lines) else ""
        if not OK_MARK.search(line):
            found.append(f)
    return sorted(found, key=lambda f: (f.path, f.line, f.rule))


def main(argv: list[str]) -> int:
    root = Path(argv[1]) if len(argv) > 1 else Path(".")
    server = root / "server"
    if not server.is_dir():
        print("check-invariants: no server/ directory, nothing to check")
        return 0
    findings: list[Finding] = []
    files = []
    for f in sorted(server.rglob("*.py")):
        rel = f.relative_to(root)
        if is_test_file(rel) or ".venv" in rel.parts or "site-packages" in rel.parts:
            continue
        files.append((rel, f.read_text(encoding="utf-8")))
    # cross-module DDL helpers (e.g. db.init_db wrapping metadata.create_all)
    helpers = set()
    for _, src in files:
        helpers |= ddl_helpers(ast.parse(src))
    for rel, src in files:
        findings += check_source(src, str(rel), helpers)
    for f in findings:
        print(f)
    errors = [f for f in findings if not f.warning]
    print(f"check-invariants: {len(errors)} error(s), {len(findings) - len(errors)} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
