"""INV-1 加锁顺序（docs/server-invariants.md）：SQL 语句流断言 + 静态检查。

用 engine 的 before_cursor_execute 事件捕获真实执行的 SQL，断言各写路径
的语句顺序符合全局锁序：业务行（note → reminders → jobs INSERT）全部
落定之后，才写 sequences（allocate_seq），change_seq 用其后的第二条
UPDATE 写回。SQLite+PG 双后端语义一致。
"""
import re
import uuid

import sqlalchemy.event

from app.db import get_engine
from tests.conftest import auth_headers


class _SqlTrace:
    """记录指定表上的写语句顺序（下标即先后）。"""

    def __init__(self, tables: tuple[str, ...]):
        self.tables = tables
        self.trace: list[tuple[str, str]] = []  # (table, verb)

    def __call__(self, conn, cursor, statement, parameters, context, executemany):
        head = " ".join(statement.split())
        m = re.match(r"(INSERT INTO|UPDATE|DELETE FROM)\s+([a-z_]+)", head)
        if m and m.group(2) in self.tables:
            verb = m.group(1)
            if verb == "INSERT INTO":
                verb = "INSERT"
            self.trace.append((m.group(2), verb))

    def indices(self, table: str, verb: str | None = None) -> list[int]:
        return [
            i
            for i, (t, v) in enumerate(self.trace)
            if t == table and (verb is None or v == verb)
        ]

    def __enter__(self):
        sqlalchemy.event.listen(get_engine().sync_engine, "before_cursor_execute", self)
        return self

    def __exit__(self, *exc):
        sqlalchemy.event.remove(get_engine().sync_engine, "before_cursor_execute", self)


def _first_seq_write(trace: _SqlTrace) -> int:
    """sequences 的第一次写入（INSERT 初始化或 UPDATE 计数）。"""
    idx = trace.indices("sequences")
    assert idx, "应至少有一次 sequences 写入（allocate_seq）"
    return idx[0]


async def test_create_note_sql_order_job_insert_before_seq_update(client, user):
    """INV-1.2（修 C2）：create 里 jobs INSERT 先 flush 落定，之后才
    allocate_seq（sequences 是提交前最后一把锁），change_seq 其后写回。"""
    headers = auth_headers(user)
    with _SqlTrace(("notes", "jobs", "sequences")) as trace:
        r = await client.post(
            "/v1/notes",
            json={"client_note_id": str(uuid.uuid4()), "text": "顺序验证"},
            headers=headers,
        )
    assert r.status_code == 202, r.text
    note_id = r.json()["note_id"]

    note_insert = trace.indices("notes", "INSERT")
    job_insert = trace.indices("jobs", "INSERT")
    seq_write = _first_seq_write(trace)
    assert note_insert, "应先 INSERT notes"
    assert job_insert, "应 INSERT jobs（extract 任务）"
    assert note_insert[0] < job_insert[0] < seq_write, (
        f"create 锁序违规：note INSERT @ {note_insert}, jobs INSERT @ {job_insert}, "
        f"sequences 写 @ {seq_write}，trace={trace.trace}"
    )
    # change_seq 写回（对 notes 的 UPDATE）必须在 sequences 写之后
    note_updates = [i for i in trace.indices("notes", "UPDATE") if i > seq_write]
    assert note_updates, "change_seq 应在 allocate_seq 之后用第二条 UPDATE 写回"

    # 入队的确实是 extract 任务且携带入队 rev
    from sqlalchemy import select

    from app.db import get_sessionmaker
    from app.models import Job

    async with get_sessionmaker()() as session:
        job = (
            await session.execute(select(Job).where(Job.kind == "extract", Job.note_id == note_id))
        ).scalar_one()
    assert job.note_rev == 1


async def test_patch_sql_order_note_reminders_jobs_before_seq_update(client, user, run_jobs):
    """INV-1.2：PATCH 的锁序 = note 条件 UPDATE → reminders 条件 UPDATE →
    jobs INSERT（extract + memory_sync，均新行）→ flush → allocate_seq →
    change_seq 写回。"""
    headers = auth_headers(user)
    created = await client.post(
        "/v1/notes",
        json={"client_note_id": str(uuid.uuid4()), "text": "明天上午十点交周报"},
        headers=headers,
    )
    note_id = created.json()["note_id"]
    await run_jobs()  # 产出建议提醒（PATCH 时要作废它）

    with _SqlTrace(("notes", "reminders", "jobs", "sequences")) as trace:
        patched = await client.patch(
            f"/v1/notes/{note_id}",
            json={"text": "改期到明天上午十一点", "rev": 2},
            headers=headers,
        )
    assert patched.status_code == 200, patched.text

    note_update = trace.indices("notes", "UPDATE")
    reminder_update = trace.indices("reminders", "UPDATE")
    job_inserts = trace.indices("jobs", "INSERT")
    seq_write = _first_seq_write(trace)
    assert note_update, "PATCH 应以 note 条件 UPDATE 开头（拿行锁/乐观锁）"
    assert reminder_update, "应作废建议提醒"
    assert len(job_inserts) >= 2, "应入队 extract 与 memory_sync 两个新 job 行"
    order_ok = (
        note_update[0]
        < reminder_update[0]
        < job_inserts[0]
        < job_inserts[-1]
        < seq_write
    )
    assert order_ok, (
        f"PATCH 锁序违规：note UPDATE @ {note_update}, reminders UPDATE @ {reminder_update}, "
        f"jobs INSERT @ {job_inserts}, sequences 写 @ {seq_write}"
    )
    # change_seq 写回在 sequences 之后
    assert [i for i in trace.indices("notes", "UPDATE") if i > seq_write], (
        "note 的 change_seq 应在 allocate_seq 之后写回"
    )
    assert [i for i in trace.indices("reminders", "UPDATE") if i > seq_write], (
        "reminders 的 change_seq 应在 allocate_seq 之后写回"
    )


async def test_extract_apply_sql_order_jobs_before_seq(client, user):
    """INV-1.2：extract 写回事务同样 jobs INSERT（memory_sync）先于
    allocate_seq（MockLLM 不访问数据库，无 LLM 语句混入）。"""
    headers = auth_headers(user)
    from app.pipeline.runner import run_pending_jobs

    created = await client.post(
        "/v1/notes",
        json={"client_note_id": str(uuid.uuid4()), "text": "抽取写回顺序"},
        headers=headers,
    )
    assert created.status_code == 202, created.text

    with _SqlTrace(("jobs", "sequences")) as trace:
        await run_pending_jobs()
    job_inserts = trace.indices("jobs", "INSERT")
    seq_writes = trace.indices("sequences")
    assert job_inserts, "apply 事务应入队 memory_sync"
    assert job_inserts[0] < seq_writes[-1], (
        f"apply 锁序违规：jobs INSERT @ {job_inserts} 应先于最后一次 sequences 写 @ {seq_writes}"
    )


# ---------------------------------------------------------------------------
# INV-1.3：业务路径对既存 job 行只有 INSERT（ON CONFLICT DO NOTHING），
# 永远不 UPDATE 既存 job 行。
# ---------------------------------------------------------------------------


def test_business_paths_never_update_existing_job_rows():
    import pathlib

    app_dir = pathlib.Path(__file__).resolve().parent.parent / "app"
    business_files = [
        app_dir / "api" / "notes.py",
        app_dir / "api" / "auth.py",
        app_dir / "api" / "sync.py",
        app_dir / "api" / "ask.py",
        app_dir / "pipeline" / "extract.py",
        app_dir / "pipeline" / "memory_sync.py",
    ]
    forbidden = re.compile(r"update\s*\(\s*Job\b")
    # 对 job 对象的状态赋值（业务路径改状态 = UPDATE 既存行的前兆）
    status_assign = re.compile(r"^\s*\w*job\w*\.status\s*=[^=]", re.MULTILINE)
    for f in business_files:
        src = f.read_text(encoding="utf-8")
        assert not forbidden.search(src), f"{f.name} 不得 UPDATE 既存 job 行（INV-1.3）"
        assert not status_assign.search(src), f"{f.name} 不得直接改 job 状态"

    # 对照组：队列内部（claim/finalize/回收）允许 UPDATE job 行——正则本身
    # 要能匹配到，防止断言恒真
    queue_src = (app_dir / "jobs" / "queue.py").read_text(encoding="utf-8")
    assert forbidden.search(queue_src), "对照组失败：queue.py 应包含 update(Job)"


async def test_business_paths_job_rows_only_inserted_via_enqueue(client, user):
    """行为层复核（双后端）：连续两次 PATCH 同一笔记（第二次 PATCH 时，
    第一次入队的 memory_sync 仍 pending）不产生 500/唯一约束冲突——
    pending 去重靠 ON CONFLICT DO NOTHING，而不是 UPDATE 既存行。"""
    from sqlalchemy import func, select

    from app.db import get_sessionmaker
    from app.models import Job

    headers = auth_headers(user)
    created = await client.post(
        "/v1/notes",
        json={"client_note_id": str(uuid.uuid4()), "text": "去重入队验证"},
        headers=headers,
    )
    note_id = created.json()["note_id"]

    r1 = await client.patch(
        f"/v1/notes/{note_id}", json={"text": "第一改", "rev": 1}, headers=headers
    )
    assert r1.status_code == 200, r1.text
    r2 = await client.patch(
        f"/v1/notes/{note_id}", json={"text": "第二改", "rev": 2}, headers=headers
    )
    assert r2.status_code == 200, r2.text

    async with get_sessionmaker()() as session:
        pendings = (
            await session.execute(
                select(func.count())
                .select_from(Job)
                .where(
                    Job.kind == "memory_sync",
                    Job.note_id == note_id,
                    Job.status == "pending",
                )
            )
        ).scalar_one()
    assert pendings == 1, "同 note 的 pending memory_sync 必须恰好一个"
