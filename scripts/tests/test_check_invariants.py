"""Unit tests for scripts/check-invariants.py (stdlib unittest).

Run: python3 -m unittest discover -s scripts/tests -v
"""
import contextlib
import importlib.util
import io
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location("check_invariants", Path(__file__).resolve().parents[1] / "check-invariants.py")
ci = importlib.util.module_from_spec(_SPEC)
sys.modules["check_invariants"] = ci
_SPEC.loader.exec_module(ci)


def rules(src):
    return [(f.rule, f.line) for f in ci.check_source(textwrap.dedent(src), "server/app/x.py")]


class SeqLast(unittest.TestCase):
    def test_enqueue_after_allocate_flagged(self):
        self.assertEqual(rules("""
        async def create(session):
            session.add(note)
            await session.flush()
            note.change_seq = await allocate_seq(session)
            await enqueue(session, "organize_note", {})
        """), [("INV-SEQ-LAST", 6)])

    def test_nested_in_business_update_flagged(self):
        self.assertEqual(rules("""
        async def patch(session):
            await session.execute(update(Note).values(text=t, change_seq=await allocate_seq(session)))
        """), [("INV-SEQ-LAST", 3)])

    def test_correct_order_ok(self):
        self.assertEqual(rules("""
        async def patch(session):
            await session.execute(update(Note).values(text=t))
            await enqueue(session, "organize_note", {})
            await session.flush()
            seq = await allocate_seq(session)
            await session.execute(update(Note).where(Note.id == i).values(change_seq=seq))
            await session.execute(update(Note).values({Note.change_seq: seq, "updated_at": now}))
            await session.flush()
        """), [])

    def test_qa_false_positives_ok(self):
        # set.add, post-commit store.delete / enqueue in a new txn, non-session receivers
        self.assertEqual(rules("""
        async def f(session, maker, store):
            seq = await allocate_seq(session)
            touched.add(n.id)
            out.items.append(x)
            await session.commit()
            await store.delete(uid, mid)
            await session.execute(select(Note).with_for_update())
            async with maker() as s2, s2.begin():
                await enqueue(s2, "sync", {})
        """), [])

    def test_codex_round_false_positives_ok(self):
        self.assertEqual(rules("""
        async def a(session):
            seq = await allocate_seq(session)
            await session.execute(select(Note.id))
        async def b(session, flag):
            if flag:
                seq = await allocate_seq(session)
            else:
                session.add(row)
        async def c(self, other):
            seq = await allocate_seq(self.session)
            other.session.add(audit)
        """), [])

    def test_scope_ends_with_begin_block(self):
        self.assertEqual(rules("""
        async def f(maker):
            async with maker() as session, session.begin():
                note.change_seq = await allocate_seq(session)
            async with maker() as session, session.begin():
                session.add(note)
        """), [])

    def test_exemption_requires_rule_id(self):
        base = """
        async def f(session):
            seq = await allocate_seq(session)
            await enqueue(session, "k", {})  # MARK
        """
        self.assertEqual(rules(base.replace("MARK", "invariant-ok: INV-SEQ-LAST new row only")), [])
        self.assertEqual(len(rules(base.replace("MARK", "invariant-ok: because"))), 1)
        self.assertEqual(len(rules(base.replace("MARK", "invariant-ok: INV-NUL wrong rule"))), 1)


class Nul(unittest.TestCase):
    def test_bare_and_optional_str_flagged(self):
        self.assertEqual([r for r, _ in rules("""
        @router.get("/{a}/{b}/{c}")
        async def g(a: str, b: str | None, c: Optional[str]): ...
        """)], ["INV-NUL"] * 3)

    def test_accepted_forms_ok(self):
        self.assertEqual(rules("""
        @router.get("/{a}/{b}/{c}/{d:int}/{e}")
        async def g(
            a: NoNulStr,
            b: Annotated[str, Path(pattern=r"^nte_[0-9a-f]{16}$")],
            c: Annotated[str, AfterValidator(reject_nul)],
            d,
            e: int,
        ): ...
        @router.get("/{f}")
        async def h(f: str = Path(pattern=r"^nte_[0-9a-f]{16}$")): ...
        """), [])

    def test_router_prefix(self):
        self.assertEqual(rules("""
        router = APIRouter(prefix="/u/{user_id}")
        @router.get("/notes")
        async def g(user_id: str): ...
        """), [("INV-NUL", 4)])


class Fallback(unittest.TestCase):
    def test_implicit_fallbacks_flagged(self):
        self.assertEqual([r for r, _ in rules("""
        def a(s):
            if not s.llm_api_key:
                return MockLLM()
        def b(s):
            try:
                return OpenAI(s)
            except Exception:
                return MockLLM()
        def c(s):
            if s.backend == "mem0":
                return Mem0()
            else:
                return InMemoryStore()
        """)], ["INV-NO-FALLBACK"] * 3)

    def test_explicit_selectors_and_prod_classes_ok(self):
        self.assertEqual(rules("""
        def a(provider):
            if provider is Provider.MOCK:
                return MockLLM()
            raise ValueError(provider)
        def b(s):
            if _store is None:
                if s.backend == "mem0":
                    _store = Mem0()
                elif s.backend == "inmemory":
                    _store = InMemoryStore()
                else:
                    raise ValueError(s.backend)
        def c(s):
            return InMemoryRateLimiter(s.rate)
        def d(s):
            if s.provider == "mock" and s.mock_seed is None:
                return MockLLM()
            raise ValueError(s.provider)
        """), [])


class Robustness(unittest.TestCase):
    def _run(self, d):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = ci.main(["x", d])
        return rc, buf.getvalue()

    def test_no_server_dir(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(self._run(d)[0], 0)

    def test_bom_encoding_broken_and_unparsable(self):
        with tempfile.TemporaryDirectory() as d:
            app = Path(d, "server/app")
            app.mkdir(parents=True)
            (app / "bom.py").write_bytes(b"\xef\xbb\xbfx = 1\n")
            (app / "latin.py").write_bytes(b"# -*- coding: latin-1 -*-\nx = '\xe9'\n")
            (app / "bad.py").write_text("def (:\n")
            os.symlink(app / "missing.py", app / "dangling.py")
            rc, out = self._run(d)
            self.assertEqual(rc, 0, out)
            self.assertIn("bad.py: warning: skipped", out)
            self.assertIn("dangling.py: warning: skipped", out)
            self.assertNotIn("Traceback", out)

    def test_rule_error_exits_one_tests_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            app = Path(d, "server/app")
            app.mkdir(parents=True)
            Path(d, "server/tests").mkdir()
            Path(d, "server/tests/test_x.py").write_text("def f(s):\n    if not s.k:\n        return MockLLM()\n")
            self.assertEqual(self._run(d)[0], 0)
            (app / "llm.py").write_text("def f(s):\n    if not s.k:\n        return MockLLM()\n")
            self.assertEqual(self._run(d)[0], 1)


if __name__ == "__main__":
    unittest.main()
