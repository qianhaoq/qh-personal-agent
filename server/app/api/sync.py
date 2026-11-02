"""增量同步：GET /v1/sync?since=<cursor>。

一致性设计（两条不变量，任一被破坏客户端按 cursor 续拉时都会丢数据）：

1. 单快照读：notes / reminders / actions 的本页选择用一条 UNION ALL 查询完成
   （单语句天然一个快照），并在同一事务里按 id 补取整行。PG 下整个
   读取跑在 REPEATABLE READ 事务里，且必须另开会话：auth 依赖已经在
   请求会话上执行过查询（事务已开始），而 SET TRANSACTION 必须是
   事务的第一条语句。SQLite 是单写者模型，事务内读取本来就是一个
   快照，跳过 SET 即可。
   之前是两条独立 SELECT：READ COMMITTED 下每条语句各用各的快照，
   两条查询之间提交的“note(小 seq) + reminder(大 seq)”会让本页带出
   reminder 却漏掉 note，cursor 越过 note 的 seq 后它被永久跳过。

2. 提交顺序 = 分配顺序：allocate_seq（app/common.py）用
   UPDATE sequences ... RETURNING 分配，PG 上该行的排他锁一直持有到
   事务结束，所以拿到小 seq 的写事务必然先于拿到大 seq 的事务提交。
   于是任一时刻已提交的 seq 集合是全局前缀，本页快照里未见的记录
   seq 一定大于页内所有 seq——把页尾 seq 当 cursor 是安全的，之后
   提交的变更会在下一次 sync 返回，不会被跳过。
   备选方案 seq_inflight 表（写入方登记在途 seq，读侧算安全上界
   safe_seq）被否决：READ COMMITTED 下读事务看不到未提交的登记行，
   探测“进行中的写”最终还是得靠锁；且崩溃残留需要额外 GC。序列行锁
   方案不加表、不加查询，代价是分配 seq 的写事务提交前相互串行
   （同一时刻至多一个在途写事务）——MVP 单用户量级的写吞吐可接受。
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy import literal, select, text, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_sessionmaker
from app.deps import get_current_user_id
from app.jobs.queue import is_postgres
from app.models import Note, Reminder, SaChannelAction
from app.schemas import action_to_dict, note_to_dict, reminder_to_dict

router = APIRouter(prefix="/v1/sync", tags=["sync"])


async def _page_keys(
    session: AsyncSession, user_id: str, since: int, limit: int
) -> list:
    """单条 UNION ALL 选出本页 (kind, change_seq, id)，按 change_seq 升序。

    单语句 = 单快照：页的组成不可能被并发提交劈成两半（一张表看到、
    另一张表没看到）。多取 1 条仅用于判断 has_more。
    """
    notes_part = select(
        literal("note").label("kind"),
        Note.change_seq.label("change_seq"),
        Note.id.label("id"),
    ).where(Note.user_id == user_id, Note.change_seq > since)
    reminders_part = select(
        literal("reminder").label("kind"),
        Reminder.change_seq.label("change_seq"),
        Reminder.id.label("id"),
    ).where(Reminder.user_id == user_id, Reminder.change_seq > since)
    actions_part = select(
        literal("action").label("kind"),
        SaChannelAction.change_seq.label("change_seq"),
        SaChannelAction.id.label("id"),
    ).where(SaChannelAction.user_id == user_id, SaChannelAction.change_seq > since)
    merged = union_all(notes_part, reminders_part, actions_part).subquery()
    stmt = (
        select(merged.c.kind, merged.c.change_seq, merged.c.id)
        .order_by(merged.c.change_seq, merged.c.kind)
        .limit(limit + 1)
    )
    return (await session.execute(stmt)).fetchall()


@router.get("")
async def sync(
    # change_seq 是 int8：上界钉住，超大整数绑定 SQL 会 OverflowError → 500
    since: int = Query(default=0, ge=0, le=2**63 - 1),
    user_id: str = Depends(get_current_user_id),
    limit: int = Query(default=100, ge=1, le=500),
):
    """返回 cursor 之后变化的 notes/reminders/actions 与被删除的 id。

    notes、reminders 与 Channel actions（含软删墓碑）合并为按 change_seq
    升序的单一流分页。actions 是端侧可消费的通知/闹钟载荷（如
    schedule_reminder）；observe 下整理不会产生 schedule/alarm，只有用户
    确认提醒后才会出现。
    """
    maker = get_sessionmaker()
    async with maker() as session, session.begin():
        if is_postgres():
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
        rows = await _page_keys(session, user_id, since, limit)
        page = rows[:limit]
        has_more = len(rows) > limit
        note_ids = [row.id for row in page if row.kind == "note"]
        reminder_ids = [row.id for row in page if row.kind == "reminder"]
        action_ids = [row.id for row in page if row.kind == "action"]
        notes_by_id: dict[str, Note] = {}
        reminders_by_id: dict[str, Reminder] = {}
        actions_by_id: dict[str, SaChannelAction] = {}
        if note_ids:
            found = (
                await session.execute(select(Note).where(Note.id.in_(note_ids)))
            ).scalars()
            notes_by_id = {n.id: n for n in found}
        if reminder_ids:
            found = (
                await session.execute(select(Reminder).where(Reminder.id.in_(reminder_ids)))
            ).scalars()
            reminders_by_id = {r.id: r for r in found}
        if action_ids:
            found = (
                await session.execute(
                    select(SaChannelAction).where(SaChannelAction.id.in_(action_ids))
                )
            ).scalars()
            actions_by_id = {a.id: a for a in found}

    live_notes: list[dict] = []
    live_reminders: list[dict] = []
    live_actions: list[dict] = []
    deleted_notes: list[str] = []
    deleted_reminders: list[str] = []
    deleted_actions: list[str] = []
    for row in page:
        if row.kind == "note":
            note = notes_by_id.get(row.id)
            if note is None:
                continue
            if note.deleted_at is None:
                live_notes.append(note_to_dict(note))
            else:
                deleted_notes.append(note.id)
        elif row.kind == "reminder":
            reminder = reminders_by_id.get(row.id)
            if reminder is None:
                continue
            if reminder.deleted_at is None:
                live_reminders.append(reminder_to_dict(reminder))
            else:
                deleted_reminders.append(reminder.id)
        else:
            action = actions_by_id.get(row.id)
            if action is None:
                continue
            if action.deleted_at is None:
                live_actions.append(action_to_dict(action))
            else:
                deleted_actions.append(action.id)

    return {
        "cursor": page[-1].change_seq if page else since,
        "notes": live_notes,
        "reminders": live_reminders,
        "actions": live_actions,
        "deleted": {
            "notes": deleted_notes,
            "reminders": deleted_reminders,
            "actions": deleted_actions,
        },
        "has_more": has_more,
    }
