#!/usr/bin/env python3
"""Mechanical server-invariant checks (AST based, low false-positive).

Every rule traces to docs/server-invariants.md (source of truth, lands with
PR #2) and is explained in docs/agent-pitfalls.md. Until that doc exists the
rule text lives in AGENTS.md "写代码" bullets added by #2.

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
        for c in calls:
            if _pos(c) <= _pos(first) or c in seq_calls or _inside(c, first, parents):
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
    return out


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
def _route_path(dec: ast.AST) -> str | None:
    if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and dec.func.attr in HTTP_METHODS:
        if dec.args and isinstance(dec.args[0], ast.Constant) and isinstance(dec.args[0].value, str):
            return dec.args[0].value
        for k in dec.keywords:
            if k.arg == "path" and isinstance(k.value, ast.Constant):
                return k.value.value
    return None


def _is_bare_str(ann: ast.AST | None) -> bool:
    if ann is None:
        return True  # FastAPI treats unannotated path params as str
    if isinstance(ann, ast.Name) and ann.id == "str":
        return True
    if isinstance(ann, ast.Constant) and ann.value == "str":
        return True
    if isinstance(ann, ast.Subscript) and _name(ann.value) == "Annotated":
        sl = ann.slice
        first = sl.elts[0] if isinstance(sl, ast.Tuple) else sl
        if _is_bare_str(first):
            # Annotated[str, AfterValidator(reject_nul)] is fine
            return "reject_nul" not in ast.unparse(sl)
    return False


def check_nul(tree: ast.AST, path: str, parents) -> list[Finding]:
    out = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        params: set[str] = set()
        for d in fn.decorator_list:
            rp = _route_path(d)
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
def _selects_explicitly(test: ast.AST) -> bool:
    for n in ast.walk(test):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and any(w in n.value.lower() for w in SELECTOR_WORDS):
            return True
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
    """Fall-through construction after an `if <selector>: ... raise` guard in the same function."""
    for st in fn.body:
        if getattr(st, "lineno", 0) >= c.lineno:
            break
        if isinstance(st, ast.If) and any(isinstance(n, ast.Raise) for n in ast.walk(st)) and (
            _selects_explicitly(st.test) or "prod" in ast.unparse(st.test).lower() or "postgres" in ast.unparse(st.test).lower()
        ):
            return True
    return False


# ---------------------------------------------------------------- INV-STARTUP-DDL
def check_ddl(tree: ast.AST, path: str, parents) -> list[Finding]:
    p = Path(path)
    if p.name in DDL_ALLOWED_FILES or any(d in p.parts for d in DDL_ALLOWED_DIRS):
        return []
    out = []
    for c in ast.walk(tree):
        if isinstance(c, ast.Call) and _call_name(c) in {"create_all", "drop_all"}:
            fn = _enclosing_func(c, parents)
            if fn is not None and not isinstance(fn, ast.Lambda) and fn.name in {"create_all", "drop_all", "init_db", "bootstrap"}:
                continue  # the helper definition itself
            if isinstance(c.func, ast.Attribute) and isinstance(c.func.value, ast.Attribute) and c.func.value.attr == "metadata":
                if fn is not None and not isinstance(fn, ast.Lambda) and fn.name in {"create_all", "init_db"}:
                    continue
            out.append(Finding(path, c.lineno, "INV-STARTUP-DDL",
                "schema DDL at process startup: API workers and the worker service race on create_all. "
                "Run it once in a bootstrap/migration step (e.g. app/cli.py) before starting services.", warning=True))
    return out


CHECKS = (check_seq_last, check_nul, check_fallback, check_ddl)


def is_test_file(p: Path) -> bool:
    return "tests" in p.parts or p.name.startswith("test_") or p.name.endswith("_test.py") or p.name == "conftest.py"


def check_source(src: str, path: str) -> list[Finding]:
    tree = ast.parse(src, filename=path)
    parents = _parents(tree)
    lines = src.splitlines()
    found = []
    for chk in CHECKS:
        for f in chk(tree, path, parents):
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
    for f in sorted(server.rglob("*.py")):
        rel = f.relative_to(root)
        if is_test_file(rel) or ".venv" in rel.parts or "site-packages" in rel.parts:
            continue
        findings += check_source(f.read_text(encoding="utf-8"), str(rel))
    for f in findings:
        print(f)
    errors = [f for f in findings if not f.warning]
    print(f"check-invariants: {len(errors)} error(s), {len(findings) - len(errors)} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
