"""Unit tests for scripts/check-invariants.py (stdlib unittest, no deps).

Run: python3 -m unittest discover -s scripts/tests -v
"""
import importlib.util
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "check_invariants", Path(__file__).resolve().parents[1] / "check-invariants.py"
)
ci = importlib.util.module_from_spec(_SPEC)
sys.modules["check_invariants"] = ci
_SPEC.loader.exec_module(ci)


def rules(src: str, path: str = "server/app/x.py"):
    return [(f.rule, f.line) for f in ci.check_source(textwrap.dedent(src), path)]


class SeqLast(unittest.TestCase):
    def test_enqueue_after_allocate_flagged(self):
        src = """
        async def create(session):
            session.add(note)
            await session.flush()
            note.change_seq = await allocate_seq(session)
            await enqueue(session, "organize_note", {})
        """
        self.assertEqual(rules(src), [("INV-SEQ-LAST", 6)])

    def test_allocate_nested_in_business_update_flagged(self):
        src = """
        async def patch(session):
            await session.execute(update(Note).values(text=t, rev=Note.rev + 1, change_seq=await allocate_seq(session)))
        """
        self.assertEqual(rules(src), [("INV-SEQ-LAST", 3)])

    def test_business_update_after_allocate_flagged(self):
        src = """
        async def fail(session):
            seq = await allocate_seq(session)
            await session.execute(update(Note).values(status="failed", change_seq=seq))
        """
        self.assertEqual(rules(src), [("INV-SEQ-LAST", 4)])

    def test_correct_order_ok(self):
        src = """
        async def patch(session):
            await session.execute(update(Note).values(text=t, rev=Note.rev + 1))
            await enqueue(session, "organize_note", {})
            seq = await allocate_seq(session)
            await session.execute(update(Note).where(Note.id == i).values(change_seq=seq))
            for r in ids:
                await session.execute(update(Reminder).values(change_seq=next(s), updated_at=now))
        """
        self.assertEqual(rules(src), [])

    def test_allowlist_comment(self):
        src = """
        async def create(session):
            seq = await allocate_seq(session)
            await enqueue(session, "k", {})  # invariant-ok: new row, no lock contention
        """
        self.assertEqual(rules(src), [])

    def test_definition_itself_ignored(self):
        src = """
        async def allocate_seq(session):
            await session.execute(insert(Sequence).values(name="g", value=0))
        """
        self.assertEqual(rules(src), [])


class SeqOrmAssign(unittest.TestCase):
    def test_attribute_write_after_allocate_flagged(self):
        src = """
        async def f(session, note):
            seq = await allocate_seq(session)
            note.change_seq = seq
            note.text = "v"
        """
        self.assertEqual(rules(src), [("INV-SEQ-LAST", 5)])

    def test_attribute_write_before_allocate_ok(self):
        src = """
        async def f(session, note):
            note.text = "v"
            await session.flush()
            note.change_seq = await allocate_seq(session)
            note.updated_at = now
        """
        self.assertEqual(rules(src), [])


class SeqLoop(unittest.TestCase):
    def test_write_before_allocate_in_same_loop_flagged(self):
        src = """
        async def f(session, rows):
            for row in rows:
                session.add(row)
                row.change_seq = await allocate_seq(session)
        """
        self.assertEqual(rules(src), [("INV-SEQ-LAST", 4)])

    def test_batch_allocate_then_change_seq_loop_ok(self):
        src = """
        async def f(session, rows):
            for row in rows:
                session.add(row)
            await session.flush()
            last = await allocate_seq(session, count=len(rows))
            for i, row in enumerate(rows):
                row.change_seq = last - i
        """
        self.assertEqual(rules(src), [])


class Nul(unittest.TestCase):
    def test_router_prefix_param_flagged(self):
        src = """
        router = APIRouter(prefix="/users/{user_id}")
        @router.get("/notes")
        async def list_notes(user_id: str):
            ...
        """
        self.assertEqual(rules(src), [("INV-NUL", 4)])

    def test_bare_str_path_param_flagged(self):
        src = """
        @router.get("/{note_id}")
        async def get_note(note_id: str, user_id: str = Depends(u)):
            ...
        """
        self.assertEqual(rules(src), [("INV-NUL", 3)])

    def test_nonulstr_ok_and_query_param_ignored(self):
        src = """
        @router.patch("/{note_id}")
        async def patch_note(note_id: NoNulStr, q: str = ""):
            ...
        @router.get("/{a}/{b:path}")
        async def g(a: Annotated[str, AfterValidator(reject_nul)], b: NoNulStr):
            ...
        """
        self.assertEqual(rules(src), [])

    def test_optional_and_union_str_flagged(self):
        src = """
        @router.get("/{a}/{b}/{c}")
        async def g(a: str | None, b: Optional[str], c: Union[int, str]):
            ...
        """
        self.assertEqual([r for r, _ in rules(src)], ["INV-NUL"] * 3)

    def test_annotated_bare_str_flagged(self):
        src = """
        @app.delete(path="/{x}")
        async def d(x: Annotated[str, Path(max_length=64)]):
            ...
        """
        self.assertEqual(rules(src), [("INV-NUL", 3)])


class Fallback(unittest.TestCase):
    def test_silent_fallback_flagged(self):
        src = """
        def get_llm(s):
            if s.provider == "openai" and s.key:
                return OpenAI()
            return MockLLM()
        """
        self.assertEqual(rules(src), [("INV-NO-FALLBACK", 5)])

    def test_explicit_selector_ok(self):
        src = """
        def get_store(backend):
            if backend == "mem0":
                return Mem0()
            elif backend == "inmemory":
                return InMemoryStore()
            raise ValueError(backend)
        """
        self.assertEqual(rules(src), [])

    def test_guarded_by_prod_raise_ok(self):
        src = """
        def get_llm(s):
            if s.provider == "openai":
                return OpenAI()
            if s.database_url.startswith("postgresql") and not s.allow:
                raise RuntimeError("no mock in prod")
            return MockLLM()
        """
        self.assertEqual(rules(src), [])

    def test_guard_mentioning_mock_does_not_excuse_fallthrough(self):
        src = """
        def get_llm(provider, key):
            if provider not in {"openai", "mock"}:
                raise ValueError(provider)
            if provider == "openai" and key:
                return OpenAI()
            return MockLLM()
        """
        self.assertEqual(rules(src), [("INV-NO-FALLBACK", 7)])

    def test_permissive_or_selector_flagged(self):
        src = """
        def get_llm(provider, api_key):
            if provider == "mock" or not api_key:
                return MockLLM()
            if provider != "mock":
                return OpenAI()
        """
        self.assertEqual(rules(src), [("INV-NO-FALLBACK", 4)])

    def test_negated_and_substring_selectors_flagged(self):
        src = """
        def a(p):
            if p != "openai":
                return MockLLM()
        def b(p):
            if "mock" in p:
                return MockLLM()
        """
        self.assertEqual([r for r, _ in rules(src)], ["INV-NO-FALLBACK"] * 2)

    def test_and_and_membership_selectors_ok(self):
        src = """
        def a(p, s):
            if p == "mock" and not s.prod:
                return MockLLM()
            if p in ("inmemory", "fake"):
                return InMemoryStore()
            raise ValueError(p)
        """
        self.assertEqual(rules(src), [])

    def test_mock_class_name_without_call_or_in_mock_factory_ok(self):
        src = """
        def make_mock_llm():
            return MockLLM()
        """
        self.assertEqual(rules(src), [])


class Ddl(unittest.TestCase):
    def test_lifespan_create_all_warns(self):
        src = """
        async def lifespan(app):
            await create_all()
            yield
        """
        found = ci.check_source(textwrap.dedent(src), "server/app/main.py")
        self.assertEqual([(f.rule, f.warning) for f in found], [("INV-STARTUP-DDL", True)])

    def test_wrapper_helper_called_at_startup_warns(self):
        src = """
        async def init_db():
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
        async def lifespan(app):
            await init_db()
            yield
        """
        found = ci.check_source(textwrap.dedent(src), "server/app/main.py")
        self.assertEqual([(f.rule, f.line) for f in found], [("INV-STARTUP-DDL", 6)])

    def test_ddl_callable_inside_lifespan_warns(self):
        src = """
        @asynccontextmanager
        async def lifespan(app):
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            yield
        """
        found = ci.check_source(textwrap.dedent(src), "server/app/main.py")
        self.assertEqual([(f.rule, f.line) for f in found], [("INV-STARTUP-DDL", 5)])

    def test_cross_module_helper_warns(self):
        found = ci.check_source("async def lifespan(a):\n    await init_db()\n", "server/app/main.py", {"init_db"})
        self.assertEqual([f.rule for f in found], ["INV-STARTUP-DDL"])

    def test_helper_and_cli_ok(self):
        helper = """
        async def create_all():
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
        """
        self.assertEqual(rules(helper, "server/app/db.py"), [])
        self.assertEqual(rules("async def main():\n    await create_all()\n", "server/app/cli.py"), [])


class Round3(unittest.TestCase):
    def test_negated_prod_guard_does_not_excuse(self):
        src = """
        def get_llm(s):
            if not s.prod:
                raise RuntimeError("x")
            return MockLLM()
        """
        self.assertEqual(rules(src), [("INV-NO-FALLBACK", 5)])

    def test_positive_prod_guard_with_optin_ok(self):
        src = """
        def get_llm(s):
            if s.database_url.startswith("postgresql") and not s.allow_mock:
                raise RuntimeError("x")
            return MockLLM()
        """
        self.assertEqual(rules(src), [])

    def test_name_substring_not_exempt_but_token_is(self):
        src = """
        def latest_client():
            return MockLLM()
        def make_fake_store():
            return FakeStore()
        """
        self.assertEqual(rules(src), [("INV-NO-FALLBACK", 3)])

    def test_match_case_selector_ok_wildcard_flagged(self):
        src = """
        def get(p):
            match p:
                case "mock":
                    return MockLLM()
                case _:
                    return MockLLM()
        """
        self.assertEqual(rules(src), [("INV-NO-FALLBACK", 7)])

    def test_quoted_annotations_flagged(self):
        src = """
        @router.get("/{a}/{b}")
        async def g(a: "str | None", b: "Optional[str]"):
            ...
        """
        self.assertEqual([r for r, _ in rules(src)], ["INV-NUL"] * 2)

    def test_head_and_websocket_routes(self):
        src = """
        @router.head("/{a}")
        async def h(a: str): ...
        @router.websocket("/ws/{b}")
        async def w(b: str): ...
        """
        self.assertEqual([r for r, _ in rules(src)], ["INV-NUL"] * 2)

    def test_relationship_mutation_after_seq_flagged_local_list_ok(self):
        src = """
        async def f(session, note, tag):
            seq = await allocate_seq(session)
            out = []
            out.append(seq)
            note.tags.append(tag)
        """
        self.assertEqual(rules(src), [("INV-SEQ-LAST", 6)])

    def test_exclusive_branches_ok(self):
        src = """
        async def f(session, row, flag):
            if flag:
                row.change_seq = await allocate_seq(session)
            else:
                session.add(row)
        """
        self.assertEqual(rules(src), [])

    def test_exemption_scoped_to_rule(self):
        base = """
        async def f(session, note):
            seq = await allocate_seq(session)
            note.text = await enqueue(session, "k", {})  # MARK
        """
        bare = base.replace("# MARK", "# invariant-ok: enqueue is safe")
        self.assertEqual(len(rules(bare)), 2)  # ambiguous: two findings on the line
        scoped = base.replace("# MARK", "# invariant-ok[INV-SEQ-LAST]: both reviewed")
        self.assertEqual(rules(scoped), [])

    def test_aliased_ddl_helper_warns(self):
        with tempfile.TemporaryDirectory() as d:
            app = Path(d, "server/app")
            app.mkdir(parents=True)
            (app / "db.py").write_text("async def init_db():\n    await conn.run_sync(Base.metadata.create_all)\n")
            (app / "main.py").write_text("from .db import init_db as setup_database\nasync def lifespan(a):\n    await setup_database()\n")
            import io, contextlib
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                ci.main(["x", d])
            self.assertIn("server/app/main.py:3: warning [INV-STARTUP-DDL]", buf.getvalue())


class Main(unittest.TestCase):
    def test_no_server_dir_exits_zero(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(ci.main(["x", d]), 0)

    def test_errors_exit_one_tests_skipped_warnings_zero(self):
        with tempfile.TemporaryDirectory() as d:
            app = Path(d, "server/app")
            app.mkdir(parents=True)
            tests = Path(d, "server/tests")
            tests.mkdir()
            (tests / "test_x.py").write_text("def f():\n    return MockLLM()\n")
            (app / "main.py").write_text("async def lifespan(a):\n    await create_all()\n")
            self.assertEqual(ci.main(["x", d]), 0)
            (app / "llm.py").write_text("def f(p):\n    return MockLLM()\n")
            self.assertEqual(ci.main(["x", d]), 1)


if __name__ == "__main__":
    unittest.main()
