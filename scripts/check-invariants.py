#!/usr/bin/env python3
"""Mechanical server-invariant checks (AST, stdlib only).

BEST-EFFORT HEURISTIC, PRECISION OVER RECALL. It flags only a few narrow,
high-confidence patterns. It does not prove the code satisfies the
invariants. Missed cases (aliases, control-flow paths, dependency-injected
params, ...) are not P0/P1 against this checker; resolve them as
  P2: defer — heuristic checker; PG concurrency tests + human/Codex review are the backstop
A false positive on legitimate code, or a crash, is a P1 against the checker.

Source of truth: docs/server-invariants.md (lands with PR #2; its rules win).
Rationale and examples: docs/agent-pitfalls.md.

Rules (checker id -> docs/server-invariants.md id):
  INV-SEQ-LAST     [INV-1 锁顺序] WARNING only (never fails CI; name resolution and
                   ordering are heuristics): allocate_seq evaluated inside
                   update(...).values() that also writes business columns; and (source
                   order, no control-flow analysis) in the transaction that calls allocate_seq(sess), no DB write
                   on that same session after it: sess.add/add_all/merge/delete/
                   execute(...) (except an UPDATE that only sets change_seq/
                   updated_at), or enqueue(sess, ...). Scope ends at sess.commit()/
                   rollback() or the end of the enclosing `with ... begin()` block.
  INV-NUL          [INV-7 外部输入硬化] FastAPI path params typed as bare str (str, str | None,
                   Optional[str], Annotated[str, ...] without a NUL-safe constraint). Only the
                   decorator path is read (router prefixes are not resolved).
                   Accepted: NoNulStr; Annotated metadata with reject_nul or any *Validator(...)
                   call (Before/After/Plain/WrapValidator, attribute forms too), or any
                   constraint call with pattern=/regex= (Path, Query, StringConstraints,
                   Field, constr); non-str converters ({id:int}). Params whose default or
                   annotation contains Depends(...)/Security(...) are skipped (injected).
  INV-NO-FALLBACK  [INV-10 配置完备性] A known fake (MockLLM, FakeLLM, InMemoryStore) constructed as an
                   implicit fallback: inside an `except` handler, a bare `else:`
                   branch, or an `if` whose test is a negation / `is None` /
                   empty-string check (e.g. missing key).
  INV-NO-RAW-EXC  [INV-14 异常不落原文] Inside an `except ... as <name>` handler, that bound name must not
                   reach a log call or an *error*-named attribute raw. Flagged, for the
                   handler-bound name only (unbound or attribute-rooted uses are ignored,
                   precision over recall): a logger call (receiver named logger/log/
                   *_logger) with the bare name, str(name)/repr(name) (slices included),
                   an f-string interpolating it, or '%s' % name as an argument;
                   logger.exception(...) and exc_info=<truthy constant> in the handler;
                   <anything .error-named> = <raw name or str()/repr() of it>. Sanitizer
                   calls (e.g. safe_error_text(exc)) are accepted — only the name itself
                   flowing raw is a finding.

Exemption (trailing comment on the flagged line, rule id required):
    # invariant-ok: INV-SEQ-LAST <reason>

Exit codes: 0 = no rule errors (warnings about unreadable files are printed);
1 = at least one rule error. Exits 0 when server/ does not exist.
"""
from __future__ import annotations

import ast
import re
import sys
import tokenize
from dataclasses import dataclass
from pathlib import Path

SEQ_FUNC = "allocate_seq"
SEQ_COLUMNS = {"change_seq", "updated_at"}
SESSION_NAMES = {"session", "db", "s", "sess"}
SESSION_TYPES = {"AsyncSession", "Session"}
SESSION_WRITES = {"add", "add_all", "merge", "delete", "execute"}
TXN_END = {"commit", "rollback"}
KNOWN_FAKES = {"MockLLM", "FakeLLM", "InMemoryStore"}
HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "api_route"}
SOURCE_INV = {
    "INV-SEQ-LAST": "INV-1",
    "INV-NUL": "INV-7",
    "INV-NO-FALLBACK": "INV-10",
    "INV-NO-RAW-EXC": "INV-14",
}
OK_MARK = re.compile(r"#\s*invariant-ok:\s*(INV-[A-Z-]+)\s+\S")


@dataclass
class Finding:
    path: str
    line: int
    rule: str
    msg: str
    warning: bool = False

    def __str__(self) -> str:
        inv = SOURCE_INV[self.rule]
        kind = "warning" if self.warning else "error"
        return (f"{self.path}:{self.line}: {kind} [{self.rule} / {inv}] {self.msg} "
                f"(docs/server-invariants.md {inv}; docs/agent-pitfalls.md#{self.rule.lower()})")


def _name(n: ast.AST) -> str | None:
    if isinstance(n, ast.Name):
        return n.id
    if isinstance(n, ast.Attribute):
        return n.attr
    return None


def _parents(tree: ast.AST) -> dict:
    out = {}
    for p in ast.walk(tree):
        for c in ast.iter_child_nodes(p):
            out[c] = p
    return out


def _ancestors(n, parents):
    p = parents.get(n)
    while p is not None:
        yield p
        p = parents.get(p)


def _pos(n) -> tuple[int, int]:
    return (n.lineno, n.col_offset)


# ------------------------------------------------------------------ INV-SEQ-LAST
def _session_names(fn) -> set[str]:
    names = set(SESSION_NAMES)
    for a in fn.args.args + fn.args.kwonlyargs:
        if a.annotation is not None and any(t in ast.unparse(a.annotation) for t in SESSION_TYPES):
            names.add(a.arg)
    return names


def _only_seq_update(call: ast.Call) -> bool:
    """execute(update(X)...values(change_seq=...)) with no other columns."""
    for n in ast.walk(call):
        if isinstance(n, ast.Call) and _name(n.func) == "values":
            keys = set()
            for k in n.keywords:
                if k.arg is None and isinstance(k.value, ast.Dict) and None not in k.value.keys:
                    keys |= {x.value if isinstance(x, ast.Constant) else _name(x) for x in k.value.keys}
                else:
                    keys.add(k.arg)
            for arg in n.args:  # mapping form: .values({Note.change_seq: seq})
                if not isinstance(arg, ast.Dict) or None in arg.keys:
                    return False
                keys |= {k.value if isinstance(k, ast.Constant) else _name(k) for k in arg.keys}
            return bool(keys) and None not in keys and keys <= SEQ_COLUMNS
    return False


_SA: set[str] = set()


def _is_dml(call: ast.Call) -> bool:
    """execute(update/insert/delete(...)) — SELECTs and other reads are not writes."""
    return any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in _SA
               for n in ast.walk(call))


def _exclusive(a, b, parents) -> bool:
    """a and b sit in different arms (body / orelse) of the same if."""
    anc_b = {id(x): x for x in _ancestors(b, parents)}
    child = a
    for p in _ancestors(a, parents):
        if id(p) in anc_b:
            if isinstance(p, ast.Try):  # sibling except handlers never both run
                cb = next((x for x in [b, *_ancestors(b, parents)] if parents.get(x) is p), None)
                return isinstance(child, ast.ExceptHandler) and isinstance(cb, ast.ExceptHandler) and child is not cb
            if isinstance(p, ast.Match):  # different case arms never both run
                cb = next((x for x in [b, *_ancestors(b, parents)] if parents.get(x) is p), None)
                return isinstance(child, ast.match_case) and isinstance(cb, ast.match_case) and child is not cb
            if not isinstance(p, ast.If):
                return False
            cb = b
            for x in _ancestors(b, parents):
                if x is p:
                    break
                cb = x
            in_body = lambda n: n in p.body
            in_else = lambda n: n in p.orelse
            return (in_body(child) and in_else(cb)) or (in_else(child) and in_body(cb))
        child = p
    return False


def _after_terminal_arm(c, seq, fn, parents) -> bool:
    """seq sits in an if/else arm that ends in return/raise/continue/break, and c is outside that if."""
    child = seq
    for p in _ancestors(seq, parents):
        if p is fn:
            return False
        if isinstance(p, ast.If):
            arm = p.body if child in p.body else p.orelse if child in p.orelse else None
            if arm and isinstance(arm[-1], (ast.Return, ast.Raise, ast.Continue, ast.Break)) \
                    and p not in _ancestors(c, parents):
                return True
        child = p
    return False


def _sqlalchemy_names(tree) -> set[str]:
    """Local names bound by `from sqlalchemy[...] import update/insert/delete` (aliases included)."""
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[0] == "sqlalchemy":
            out |= {a.asname or a.name for a in n.names if a.name in {"update", "insert", "delete"}}
    return out


def check_seq_last(tree, path, parents) -> list[Finding]:
    global _SA
    _SA = _sqlalchemy_names(tree)
    out = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) or fn.name == SEQ_FUNC:
            continue
        sessions = _session_names(fn)
        local = {a.arg for a in ast.walk(fn.args) if isinstance(a, ast.arg)} | {
            n.id for n in ast.walk(fn) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
        _SA = _sqlalchemy_names(tree) - local  # locally rebound names no longer mean sqlalchemy
        calls = sorted(
            (c for c in ast.walk(fn) if isinstance(c, ast.Call)
             and next((p for p in _ancestors(c, parents) if isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))), None) is fn),
            key=_pos,
        )
        for seq in (c for c in calls if _name(c.func) == SEQ_FUNC
                    and not any(isinstance(p, (ast.GeneratorExp, ast.Lambda)) for p in _ancestors(c, parents))):
            sess = seq.args[0].id if seq.args and isinstance(seq.args[0], ast.Name) else None
            if sess not in sessions:
                continue
            # allocate_seq nested in a .values() that writes business columns
            for p in _ancestors(seq, parents):
                if p is fn:
                    break
                if isinstance(p, ast.Call) and _name(p.func) == "values" and isinstance(p.func, ast.Attribute) \
                        and any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in _SA
                                for n in ast.walk(p.func.value)):  # update() imported from sqlalchemy
                    extra = sorted({k.arg for k in p.keywords if k.arg} - SEQ_COLUMNS)
                    if extra:
                        out.append(Finding(path, seq.lineno, "INV-SEQ-LAST",
                            f"{SEQ_FUNC}() inside an UPDATE that also writes {extra}: sequence lock taken before the row lock", warning=True))
                    break
            txn = next((p for p in _ancestors(seq, parents)
                        if isinstance(p, (ast.With, ast.AsyncWith)) and "begin" in " ".join(ast.unparse(i) for i in p.items)), None)
            for c in calls:
                if _pos(c) <= _pos(seq) or seq in _ancestors(c, parents) or c in _ancestors(seq, parents):
                    continue
                if txn is not None and txn not in _ancestors(c, parents):
                    break  # left the transaction block
                f = c.func
                recv = f.value.id if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) else None
                if recv == sess and f.attr in TXN_END:
                    break
                bad = None
                if _exclusive(c, seq, parents) or _after_terminal_arm(c, seq, fn, parents):
                    continue
                if recv == sess and f.attr in SESSION_WRITES:
                    if f.attr != "execute" or (_is_dml(c) and not _only_seq_update(c)):
                        bad = f"{sess}.{f.attr}()"
                elif isinstance(f, ast.Name) and f.id.startswith("enqueue") and any(isinstance(a, ast.Name) and a.id == sess for a in c.args):
                    bad = f"{f.id}({sess}, ...)"
                if bad:
                    out.append(Finding(path, c.lineno, "INV-SEQ-LAST",
                        f"{bad} after {SEQ_FUNC}() (line {seq.lineno}) in the same transaction: flush business rows and "
                        "jobs first, call allocate_seq last, then only write change_seq", warning=True))
    return out


# ------------------------------------------------------------------ INV-NUL
INJECTORS = {"Depends", "Security"}


def _call_named(n, pred) -> bool:
    return isinstance(n, ast.Call) and (_name(n.func) or "") != "" and pred(_name(n.func))


def _nul_safe_meta(meta: list[ast.AST]) -> bool:
    for m in meta:
        if "reject_nul" in ast.unparse(m):
            return True
        # Before/After/Plain/WrapValidator(...) (or pydantic.functional_validators.X): the validator owns the
        # value, so assume it rejects NUL (precision over recall).
        if any(_call_named(n, lambda x: x.endswith("Validator")) for n in ast.walk(m)):
            return True
        if isinstance(m, ast.Call) and any(k.arg in {"pattern", "regex"} for k in m.keywords):  # Path/Query/StringConstraints/constr/Field
            return True
    return False


def _parsed(ann):
    """String (forward-reference) annotations parsed to an expression; None if unparsable."""
    if isinstance(ann, ast.Constant) and isinstance(ann.value, str):
        try:
            return ast.parse(ann.value, mode="eval").body
        except SyntaxError:
            return None
    return ann


def _injected(ann, default) -> bool:
    """Depends(...)/Security(...) (incl. fastapi.Depends) in the default or anywhere in the annotation:
    the value comes from a dependency, which owns validation (it is not the raw path string)."""
    nodes = [x for x in (_parsed(ann), default) if x is not None]
    return any(_call_named(n, lambda x: x in INJECTORS) for root in nodes for n in ast.walk(root))


def _bare_str(ann) -> bool:
    if ann is None:
        return True
    if isinstance(ann, ast.Constant) and isinstance(ann.value, str):
        try:
            return _bare_str(ast.parse(ann.value, mode="eval").body)
        except SyntaxError:
            return False
    if isinstance(ann, ast.Name):
        return ann.id == "str"
    if isinstance(ann, ast.BinOp) and isinstance(ann.op, ast.BitOr):
        return _bare_str(ann.left) or _bare_str(ann.right)
    if isinstance(ann, ast.Subscript):
        head = _name(ann.value)
        elts = ann.slice.elts if isinstance(ann.slice, ast.Tuple) else [ann.slice]
        if head == "Optional":
            return _bare_str(elts[0])
        if head == "Annotated":
            return _bare_str(elts[0]) and not _nul_safe_meta(elts[1:])
    return False


def check_nul(tree, path, parents) -> list[Finding]:
    out = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        params = set()
        for d in fn.decorator_list:
            if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr in HTTP_METHODS:
                route = d.args[0].value if d.args and isinstance(d.args[0], ast.Constant) else ""
                for m in re.findall(r"{([^}]+)}", str(route)):
                    name, _, conv = m.partition(":")
                    if conv in {"", "str", "path"}:
                        params.add(name)
        pos = fn.args.args
        defaults = dict(zip([a.arg for a in pos[len(pos) - len(fn.args.defaults):]], fn.args.defaults))
        defaults.update({a.arg: d for a, d in zip(fn.args.kwonlyargs, fn.args.kw_defaults) if d is not None})
        for a in fn.args.args + fn.args.kwonlyargs:
            if _injected(a.annotation, defaults.get(a.arg)):
                continue  # note_id: str = Depends(dep) / Annotated[str, Depends(dep)]
            if a.arg in defaults and _nul_safe_meta([defaults[a.arg]]):
                continue  # note_id: str = Path(pattern=...)
            if a.arg in params and _bare_str(a.annotation):
                out.append(Finding(path, a.lineno, "INV-NUL",
                    f"path parameter '{a.arg}' of {fn.name}() is a bare str: use NoNulStr (or a Path pattern) so %00 is a 422, not a PG 500"))
    return out


# ------------------------------------------------------------------ INV-NO-FALLBACK
def _implicit_test(t) -> bool:
    if isinstance(t, ast.UnaryOp) and isinstance(t.op, ast.Not):
        return True
    if isinstance(t, ast.Compare) and len(t.ops) == 1:
        r = t.comparators[0]
        if isinstance(t.ops[0], (ast.Is, ast.Eq)) and isinstance(r, ast.Constant) and r.value in (None, ""):
            return True
    if isinstance(t, ast.BoolOp):
        if isinstance(t.op, ast.And):  # an explicit selector term keeps the whole branch explicit
            return all(_implicit_test(v) for v in t.values)
        return any(_implicit_test(v) for v in t.values)
    return False


def check_fallback(tree, path, parents) -> list[Finding]:
    out = []
    for c in ast.walk(tree):
        if not (isinstance(c, ast.Call) and _name(c.func) in KNOWN_FAKES):
            continue
        child, why, explicit = c, None, False
        for p in _ancestors(c, parents):
            if isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                break
            if isinstance(p, ast.match_case) and (
                not (isinstance(p.pattern, ast.MatchAs) and p.pattern.pattern is None)
                or (p.guard is not None and not _implicit_test(p.guard))
            ):
                explicit = True  # enclosing literal/value case (case "mock": / case Provider.MOCK:)
                break
            if isinstance(p, ast.ExceptHandler):
                why = why or "in an except handler"
            elif isinstance(p, ast.If):
                if child in p.body and not _implicit_test(p.test):
                    explicit = True  # any enclosing explicit selector (e.g. provider == "mock") wins
                    break
                if child in p.orelse and isinstance(p.test, ast.Compare) and len(p.test.ops) == 1 \
                        and isinstance(p.test.ops[0], (ast.NotEq, ast.IsNot, ast.NotIn)):
                    explicit = True  # `if provider != "mock": ... else: MockLLM()` selects the fake explicitly
                    break
                if child in p.orelse and not (len(p.orelse) == 1 and isinstance(p.orelse[0], ast.If)):
                    why = why or "in an else branch"
                elif child in p.body:
                    why = why or f"under `if {ast.unparse(p.test)}`"
            child = p
        if why and not explicit:
            out.append(Finding(path, c.lineno, "INV-NO-FALLBACK",
                f"{_name(c.func)}() constructed as an implicit fallback ({why}): select fakes only on an explicit "
                "config value and raise on missing/invalid config"))
    return out


# ------------------------------------------------------------------ INV-NO-RAW-EXC
# 日志接收者：logger / log / *_logger / *_log（首尾精确匹配——"catalog" 这类
# 尾缀巧合不算）。receiver 取点表达式最后一段（logger、self.log 都识别）。
_LOGGER_RECV = re.compile(r"^(\w+[_])?log(ger)?$")


def _logger_call(call: ast.Call) -> bool:
    f = call.func
    return (
        isinstance(f, ast.Attribute)
        and _name(f.value) is not None
        and _LOGGER_RECV.match(_name(f.value)) is not None
    )


def _exc_leaks(node: ast.AST, bound: str) -> bool:
    """该表达式的值会把异常原文带出去：裸 bound 名、str/repr(bound)、
    f-string 插值 bound、'%s' % bound（含元组）、以上再取切片。属性访问
    （exc.code）与别的函数调用（safe_error_text(exc)）不算——精确优先。"""
    if isinstance(node, ast.Name):
        return node.id == bound
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("str", "repr"):
        return len(node.args) == 1 and _exc_leaks(node.args[0], bound)
    if isinstance(node, ast.JoinedStr):
        return any(isinstance(v, ast.FormattedValue) and _exc_leaks(v.value, bound) for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        return _exc_leaks(node.right, bound)
    if isinstance(node, ast.Tuple):
        return any(_exc_leaks(e, bound) for e in node.elts)
    if isinstance(node, ast.Subscript):
        return _exc_leaks(node.value, bound)
    return False


def check_raw_exc(tree, path, parents) -> list[Finding]:
    """INV-14（threads8 P1）：except 子句绑定的异常名不得原文进入日志、
    traceback 或 *error* 命名的属性（jobs.last_error 等）。只看绑定名本身
    的裸流动，sanitizer 包装放行；logger.exception/exc_info=True 在 handler
    内无条件命中（traceback 必带消息原文）。"""
    out = []
    for handler in ast.walk(tree):
        if not isinstance(handler, ast.ExceptHandler) or not handler.name:
            continue
        bound = handler.name
        for node in ast.walk(handler):
            if node is handler:
                continue
            if isinstance(node, ast.Call) and _logger_call(node):
                if node.func.attr == "exception":
                    out.append(Finding(path, node.lineno, "INV-NO-RAW-EXC",
                        "logger.exception(...) logs the raw traceback (the exception message may embed user content): "
                        "use logger.error with a safe summary (app/errors.py::safe_error_text)"))
                    continue
                if any(k.arg == "exc_info" and isinstance(k.value, ast.Constant) and k.value.value
                       for k in node.keywords):
                    out.append(Finding(path, node.lineno, "INV-NO-RAW-EXC",
                        "exc_info=<truthy> logs the raw traceback (the exception message may embed user content): "
                        "log type+code only (app/errors.py::safe_error_text)"))
                    continue
                if any(_exc_leaks(a, bound) for a in node.args):
                    out.append(Finding(path, node.lineno, "INV-NO-RAW-EXC",
                        f"logger call receives the exception ('{bound}') raw: its message may embed user content "
                        "(SQLAlchemy StatementError str() includes SQL bound params); log type+code only "
                        "(app/errors.py::safe_error_text)"))
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                value = node.value
                if value is None:
                    continue
                for t in targets:
                    if isinstance(t, ast.Attribute) and "error" in t.attr.lower() and _exc_leaks(value, bound):
                        out.append(Finding(path, node.lineno, "INV-NO-RAW-EXC",
                            f"'{ast.unparse(t)}' would persist the raw exception text: store "
                            "safe_error_text(exc) (type+code) instead"))
                        break
    return out


CHECKS = (check_seq_last, check_nul, check_fallback, check_raw_exc)


def check_source(src: str, path: str) -> list[Finding]:
    tree = ast.parse(src, filename=path)
    parents = _parents(tree)
    lines = src.splitlines()
    found = []
    for f in (f for chk in CHECKS for f in chk(tree, path, parents)):
        m = OK_MARK.search(lines[f.line - 1]) if 0 < f.line <= len(lines) else None
        if m and m.group(1) == f.rule:
            continue
        found.append(f)
    return sorted(found, key=lambda f: (f.path, f.line, f.rule))


def _is_test(rel: Path) -> bool:
    return "tests" in rel.parts or rel.name.startswith("test_") or rel.name.endswith("_test.py") or rel.name == "conftest.py"


def main(argv: list[str]) -> int:
    root = Path(argv[1]) if len(argv) > 1 else Path(".")
    server = root / "server"
    if not server.is_dir():
        print("check-invariants: no server/ directory, nothing to check")
        return 0
    findings, warnings = [], 0
    for f in sorted(server.rglob("*.py")):
        rel = f.relative_to(root)
        if _is_test(rel) or ".venv" in rel.parts or "site-packages" in rel.parts:
            continue
        try:
            with tokenize.open(f) as fh:  # honours BOM and PEP 263 declarations
                src = fh.read()
            findings += check_source(src, str(rel))
        except (OSError, SyntaxError, UnicodeDecodeError, ValueError, RecursionError, MemoryError) as e:
            # MemoryError/RecursionError: pathologically nested source CPython itself cannot compile
            warnings += 1
            print(f"{rel}: warning: skipped ({type(e).__name__}: {e})")
    for x in findings:
        print(x)
    errors = [x for x in findings if not x.warning]
    warnings += len(findings) - len(errors)
    print(f"check-invariants: {len(errors)} error(s), {warnings} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
