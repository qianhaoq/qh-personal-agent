"""增量同步：/v1/sync 返回变化与删除。"""
import uuid

from tests.conftest import auth_headers, requires_postgres


async def _create(client, user, text="明天早上九点提醒我交周报") -> str:
    r = await client.post(
        "/v1/notes",
        json={"client_note_id": str(uuid.uuid4()), "text": text},
        headers=auth_headers(user),
    )
    return r.json()["note_id"]


async def test_sync_returns_changes_since_cursor(client, user, run_jobs):
    first_id = await _create(client, user)
    r1 = await client.get("/v1/sync", headers=auth_headers(user))
    d1 = r1.json()
    assert d1["cursor"] > 0
    assert [n["id"] for n in d1["notes"]] == [first_id]
    assert d1["deleted"] == {"notes": [], "reminders": []}

    # 第二次用 cursor 拉：无新变化
    r2 = await client.get(f"/v1/sync?since={d1['cursor']}", headers=auth_headers(user))
    assert r2.json()["notes"] == []

    # 整理完成后：笔记变化 + 建议提醒出现
    await run_jobs()
    r3 = await client.get(f"/v1/sync?since={d1['cursor']}", headers=auth_headers(user))
    d3 = r3.json()
    assert d3["notes"][0]["status"] == "organized"
    assert any(n["id"] == first_id for n in d3["notes"])
    assert len(d3["reminders"]) >= 1
    assert d3["cursor"] > d1["cursor"]


async def test_sync_reports_deleted_ids(client, user, run_jobs):
    note_id = await _create(client, user)
    await run_jobs()
    await client.delete(f"/v1/notes/{note_id}", headers=auth_headers(user))

    resp = await client.get("/v1/sync", headers=auth_headers(user))
    d = resp.json()
    assert note_id in d["deleted"]["notes"]
    assert all(n["id"] != note_id for n in d["notes"])


async def _walk(client, headers, limit: int) -> tuple[dict[str, dict], list[dict]]:
    """按 cursor 用给定 limit 循环拉到 has_more=false。返回 (notes_by_id, reminders)。"""
    notes: dict[str, dict] = {}
    reminders: list[dict] = []
    cursor = 0
    for _ in range(200):  # 上限保护，has_more 一直为 true 时不要死循环
        resp = await client.get(
            f"/v1/sync?since={cursor}&limit={limit}", headers=headers
        )
        assert resp.status_code == 200, resp.text
        page = resp.json()
        page_size = (
            len(page["notes"])
            + len(page["reminders"])
            + len(page["deleted"]["notes"])
            + len(page["deleted"]["reminders"])
        )
        assert page_size <= limit, f"单页条数 {page_size} 超过 limit={limit}"
        for n in page["notes"]:
            notes[n["id"]] = n
        for nid in page["deleted"]["notes"]:
            notes.setdefault(nid, {"id": nid})
        reminders.extend(page["reminders"])
        reminders.extend({"id": rid} for rid in page["deleted"]["reminders"])
        cursor = max(cursor, page["cursor"])
        if not page["has_more"]:
            return notes, reminders
    raise AssertionError("分页未在 200 页内结束")


async def test_sync_pagination_merged_stream_no_loss(client, user, run_jobs):
    """notes/reminders 合并流分页：小 limit 循环拉取不漏数据。

    3 条普通笔记 + 1 条“明天上午十点交周报”，整理后 notes 与 reminders
    交错出现在同一流里；按 limit=2 / limit=1 续拉必须拿全 4 条笔记和建议提醒。
    """
    headers = auth_headers(user)
    plain = ["今天天气不错随手一记", "中午吃了一碗面", "下班路上听了个播客"]
    note_ids = []
    for text in plain:
        note_ids.append(await _create(client, user, text))
    reminder_note_id = await _create(client, user, "明天上午十点交周报")
    await run_jobs()

    for limit in (2, 1):
        notes, reminders = await _walk(client, headers, limit)
        collected = set(notes)
        expected = set(note_ids) | {reminder_note_id}
        assert collected == expected, f"limit={limit} 期望 {expected}，实际 {collected}"
        suggested = [r for r in reminders if r.get("status") == "suggested"]
        assert suggested, f"limit={limit} 未拉到建议提醒"
        assert all(r["note_id"] == reminder_note_id for r in suggested)


async def test_sync_multi_reminders_one_note_limit1_no_loss(client, user, run_jobs, monkeypatch):
    """回归：同一笔记的多条建议提醒各自分配 change_seq。

    旧实现一批提醒共用同一个 seq，limit=1 分页恰好切在组中间时，cursor
    取本页最后一条的 seq，同组剩余提醒被 `> cursor` 永久跳过。
    """
    import json
    from datetime import UTC, datetime, timedelta

    from app.pipeline import organize

    class _FakeLLM:
        async def chat(self, messages, response_format=None):
            base = datetime.now(UTC)
            reminders = [
                {"title": f"提醒事项{i}", "fire_at": (base + timedelta(hours=1 + i)).isoformat()}
                for i in range(4)
            ]
            return json.dumps(
                {"title": "多条提醒", "tags": [], "todos": [], "suggested_reminders": reminders},
                ensure_ascii=False,
            )

    async def _fake_client():
        return _FakeLLM()

    monkeypatch.setattr(organize, "get_llm_client", _fake_client)

    headers = auth_headers(user)
    await _create(client, user, "一条产生多条建议提醒的笔记")
    await run_jobs()

    _notes, reminders = await _walk(client, headers, limit=1)
    suggested = [r for r in reminders if r.get("status") == "suggested"]
    assert {r["title"] for r in suggested} == {f"提醒事项{i}" for i in range(4)}


async def test_sync_isolated_per_user(client, user):
    from app.db import get_sessionmaker
    from tests.conftest import make_invite

    code2 = await make_invite(get_sessionmaker())
    r2 = await client.post(
        "/v1/auth/login", json={"invite_code": code2, "device_id": "dev_sync_other2"}
    )
    other_headers = {"authorization": f"Bearer {r2.json()['access_token']}"}

    await _create(client, user)
    resp = await client.get("/v1/sync", headers=other_headers)
    assert resp.json()["notes"] == []


async def test_each_old_reminder_tombstoned_once_across_reorganizes(client, user, run_jobs):
    """回归（P2）：多次 PATCH 重新整理后，续拉 sync 的客户端每条旧提醒只收到一次墓碑。

    _clear_prev_outputs 之前不过滤 deleted_at，把已软删的 suggested 提醒
    再删一遍并分配新 change_seq：行的 seq 被顶到 cursor 之后，按 cursor
    续拉的客户端会对同一条提醒收到重复的删除事件。
    """
    from collections import Counter

    headers = auth_headers(user)
    r = await client.post(
        "/v1/notes",
        json={"client_note_id": "tomb-once", "text": "明天上午十点交周报"},
        headers=headers,
    )
    note_id = r.json()["note_id"]

    tombstones: Counter = Counter()
    cursor = 0

    async def drain():
        """按 cursor 续拉到头，累计收到的墓碑。"""
        nonlocal cursor
        for _ in range(100):  # 上限保护
            page = (await client.get(f"/v1/sync?since={cursor}&limit=3", headers=headers)).json()
            for rid in page["deleted"]["reminders"]:
                tombstones[rid] += 1
            cursor = page["cursor"]
            if not page["has_more"]:
                return

    await run_jobs()
    await drain()
    for i in range(2):
        rev = (await client.get(f"/v1/notes/{note_id}", headers=headers)).json()["rev"]
        patched = await client.patch(
            f"/v1/notes/{note_id}",
            json={"text": f"明天上午十点交周报（第{i + 2}版）", "rev": rev},
            headers=headers,
        )
        assert patched.status_code == 200, patched.text
        await run_jobs()
        await drain()  # 每轮重新整理后按上次的 cursor 续拉

    assert tombstones, "两轮重新整理后应有被作废的建议提醒"
    assert all(count == 1 for count in tombstones.values()), dict(tombstones)


@requires_postgres
async def test_sync_cursor_never_passes_uncommitted_smaller_seq(client, user):
    """P1-A 提交顺序：小 seq 在途时大 seq 的写必须等待，cursor 不能越过它。

    会话 A 分配 seq 并写一条 note 但不提交；会话 B 并发分配 seq——必须
    被 A 持有的 sequences 行锁阻塞（分配序 = 提交序）。A 提交前 sync 任何
    一方的记录都看不到；A 提交、B 拿到更大 seq 写 reminder 并提交后，
    sync 必须同时拿到 A 的 note 与 B 的 reminder。

    仅 PG：验证的是行锁阻塞语义；SQLite 单写者模型下并发写本来就串行，
    构造不出“小 seq 晚于大 seq 提交”的窗口。
    """
    import asyncio

    from app.common import allocate_seq, new_id
    from app.db import get_sessionmaker
    from app.models import Note, Reminder

    maker = get_sessionmaker()
    headers = auth_headers(user)
    uid = user["user_id"]
    note_a_id, reminder_b_id = new_id("nte"), new_id("rmd")

    async with maker() as s0, s0.begin():
        # 预初始化 sequences 行：否则 A 的首次分配要 INSERT（未提交），
        # B 会卡在同主键插入上而不是 UPDATE 行锁，测试语义就变了
        await allocate_seq(s0)

    async def b_writes() -> int:
        async with maker() as sb, sb.begin():
            seq_b = await allocate_seq(sb)
            sb.add(Reminder(id=reminder_b_id, user_id=uid, title="B 的提醒", change_seq=seq_b))
            await sb.flush()
        return seq_b

    async with maker() as sa, sa.begin():
        seq_a = await allocate_seq(sa)
        sa.add(
            Note(
                id=note_a_id,
                user_id=uid,
                client_note_id="pg-commit-order-a",
                text="A 先分配 seq 但不提交",
                change_seq=seq_a,
            )
        )
        await sa.flush()

        task = asyncio.create_task(b_writes())
        await asyncio.sleep(0.5)
        assert not task.done(), "B 的 allocate_seq 应被 A 的序列行锁阻塞"

        # A 未提交：sync 看不到 A/B 任何一方，cursor 不会越过 A 的 seq
        page = (await client.get("/v1/sync", headers=headers)).json()
        assert page["notes"] == [] and page["reminders"] == []
        assert page["cursor"] < seq_a
    # 事务块退出即 A 提交，序列行锁释放 → B 解除阻塞，拿到更大 seq 并提交
    seq_b = await asyncio.wait_for(task, timeout=10)
    assert seq_b > seq_a

    page = (await client.get("/v1/sync", headers=headers)).json()
    assert [n["id"] for n in page["notes"]] == [note_a_id]
    assert [r["id"] for r in page["reminders"]] == [reminder_b_id]
    assert page["cursor"] == seq_b


@requires_postgres
async def test_sync_page_not_split_by_commit_between_reads(client, user, monkeypatch):
    """P1-A 回归：两次读之间插入一次提交，本页不得跳号或读到新快照。

    旧实现 notes/reminders 两条独立 SELECT 各用各的语句级快照（READ
    COMMITTED）：两条查询之间提交的“note(小 seq) + reminder(大 seq)”
    会让本页带出 reminder 却漏掉 note，cursor 越过 note 的 seq 后它被
    永久跳过。现在页选择是单条 UNION ALL，页选择与补取行同在 REPEATABLE
    READ 事务里：读之间提交的变更（新插入的行 + 对页内行的修改）整体
    落到下一页，本页内容对应本页 cursor 的快照版本。

    仅 PG：SQLite 读事务持有共享锁，读之间注入写会互相阻塞等待，
    构造不出该竞态（其单写者模型本身也保证了一致快照）。
    """
    from sqlalchemy import update as sa_update

    from app.api import sync as sync_module
    from app.common import allocate_seq, new_id
    from app.db import get_sessionmaker
    from app.models import Note, Reminder
    from app.timeutil import utcnow

    maker = get_sessionmaker()
    uid, headers = user["user_id"], auth_headers(user)

    # 请求前就存在、会进本页的笔记（hook 会并发修改它）
    old_text = "读之前就存在的笔记"
    note_e_id = new_id("nte")
    async with maker() as s1, s1.begin():
        seq_e = await allocate_seq(s1)
        s1.add(
            Note(
                id=note_e_id,
                user_id=uid,
                client_note_id="pg-snapshot-exist",
                text=old_text,
                change_seq=seq_e,
            )
        )

    note_n_id, reminder_id = new_id("nte"), new_id("rmd")
    real_page_keys = sync_module._page_keys

    async def page_keys_with_injected_commit(session, u, since, limit):
        rows = await real_page_keys(session, u, since, limit)
        # 两次读之间插入一次提交：修改页内已有的 note + 写入 note(seq X)
        # 与 reminder(seq X+1)，同一事务
        async with maker() as s2, s2.begin():
            await s2.execute(
                sa_update(Note)
                .where(Note.id == note_e_id)
                .values(
                    text="读之间被并发修改的新内容",
                    change_seq=await allocate_seq(s2),
                    updated_at=utcnow(),
                )
            )
            seq_n = await allocate_seq(s2)
            s2.add(
                Note(
                    id=note_n_id,
                    user_id=u,
                    client_note_id="pg-snapshot-mid",
                    text="读之间提交的笔记",
                    change_seq=seq_n,
                )
            )
            await s2.flush()
            seq_r = await allocate_seq(s2)
            s2.add(Reminder(id=reminder_id, user_id=u, title="读之间提交的提醒", change_seq=seq_r))
            await s2.flush()
        return rows

    monkeypatch.setattr(sync_module, "_page_keys", page_keys_with_injected_commit)

    resp = await client.get("/v1/sync?limit=5", headers=headers)
    assert resp.status_code == 200, resp.text
    d = resp.json()
    # 本页 = 提交前的快照：只有已存在的 note 且是旧文本；新写的两条与
    # note 的新版本都不进本页，cursor 停在旧 seq 上
    assert [n["id"] for n in d["notes"]] == [note_e_id]
    assert d["notes"][0]["text"] == old_text
    assert d["reminders"] == []
    assert d["cursor"] == seq_e

    monkeypatch.undo()
    d2 = (await client.get(f"/v1/sync?since={d['cursor']}&limit=5", headers=headers)).json()
    assert {n["id"] for n in d2["notes"]} == {note_e_id, note_n_id}
    assert next(n for n in d2["notes"] if n["id"] == note_e_id)["text"] == "读之间被并发修改的新内容"
    assert {r["id"] for r in d2["reminders"]} == {reminder_id}
