# Agent 反复踩的坑（server）

> **事实来源**：服务端不变量以 [docs/server-invariants.md](server-invariants.md)（INV-1..INV-10，随 PR #2 落地，#2 head `a220dd4` 起已有）为准。与本文冲突时以它为准，并回来改本文。本文只收集“同一类问题被 Codex 反复提出”的模式和正反例，**不另立新的不变量**。
>
> `scripts/check-invariants.py` 是 **best-effort 启发式，精确优先于召回**：只标少数高置信度的写法，不证明代码满足不变量。CI 里的 check 名是 **`Invariant Check (non-required)`**（独立的 `Invariants` workflow，不是必需检查）。
> - 漏检按 `P2: defer — heuristic checker; PG concurrency tests + human/Codex review are the backstop` Resolve。
> - 误报或检查器崩溃算针对检查器的 P1。
> - 豁免只有一种写法，写在被标出的那一行行尾，必须带规则 id：`# invariant-ok: <RULE-ID> <理由>`。

## 统计（PR #2 共 5 轮 Codex + PR #3，2026-10-17）

共 45 条 Codex 发现（P0 0 / P1 21 / P2 24）。同一类问题平均要被提 2~3 轮，因为每次只修了被点名的那一行。

| # | 类别 | P1 | P2 | 合计 | 机检（对应 INV） |
| --- | --- | --- | --- | --- | --- |
| 1 | 先读后写竞态（没有条件 UPDATE / ON CONFLICT / FOR UPDATE） | 6 | 2 | 8 | —（INV-1、INV-8） |
| 2 | Job 生命周期（回收、收尾租约、重试次数） | 3 | 3 | 6 | — |
| 3 | 外部输入 / 配置没校验（NUL、时区、JWT 占位、LLM 配置） | 2 | 4 | 6 | INV-NUL（INV-7、INV-10） |
| 4 | 提交后副作用（记忆同步等）没有收敛 | 3 | 2 | 5 | — |
| 5 | 时间语义（锚点、naive 时间、过去的提醒） | 0 | 5 | 5 | — |
| 6 | 静默回落假实现 / 生产配置接错 | 2 | 2 | 4 | INV-NO-FALLBACK（INV-10） |
| 7 | 加锁顺序与 allocate_seq 位置 | 2 | 0 | 2 | INV-SEQ-LAST（INV-1） |
| 8 | sync cursor / 快照一致性 | 2 | 0 | 2 | — |
| 9 | 多进程启动 DDL（create_all） | 0 | 1 | 1 | —（INV-9） |
| 10 | CI 配置 / 只在 SQLite 上测（PR #3 的 3 条；#2 另有 3 个 P1 只在 PG 上成立） | 0 | 3 | 3 | PG Tests job |
| 11 | 其他（第三方 API 字段、软删过滤、文本归一化） | 1 | 2 | 3 | — |

## 评审分级口径（与 .github/copilot-instructions.md 一致）

- CI 红灯是 **P1**；只有造成生产事故时才算 P0。
- 并发敏感的改动（锁、条件更新、job、sync）没有 PG 并发测试，算 **P1**。其他测试建议仍是 P2。

---

## 1. 先读后写必须原子（INV-1 加锁表、INV-8） <a id="race"></a>

读出旧值再决定写什么的状态变更（乐观锁 rev、token 轮换、计数、首用占位、状态机），必须用条件 UPDATE（`WHERE 旧值`，再看 rowcount）、`INSERT … ON CONFLICT`，或者 `SELECT … FOR UPDATE` 之后复查。

```python
# 错：两个请求都读到 owner=None，各建一个用户
invite = await session.get(InviteCode, code)
if invite.owner_user_id is None:
    invite.owner_user_id = new_user_id
# 对：条件更新抢占；rowcount=0 说明输了，回滚后重读
res = await session.execute(update(InviteCode)
    .where(InviteCode.code == code, InviteCode.owner_user_id.is_(None))
    .values(owner_user_id=new_user_id))
```

怎么被发现的：Codex 第 1、3、4、5 轮在 notes、auth 的不同函数里各提了一次。**修一个就把同类全修**：`rg "session.get\(|scalar_one_or_none" server/app` 逐个看有没有“读后写”。

## 2. Job 收尾带租约（INV-2、INV-6） <a id="jobs"></a>

收尾必须带租约条件 `status='running' AND locked_by=:me AND locked_at=:claimed`，被换主的旧 worker 一律放弃收尾。判死条件是 `attempts >= max_attempts`（attempts 是已经用掉的次数），`max_attempts` 从配置读取。extract 与 memory_sync 两种 job 的收尾差异见 INV-6，本文不另作规定。

怎么被发现的：第 2~5 轮在 queue/runner 里连续出现。

## 3. 外部输入在边界校验（INV-7、INV-10；机检 INV-NUL） <a id="inv-nul"></a>

所有外部字符串（body、query、path、header 派生值）都拒绝 `\x00`，在 schema 层返回 422。时区用 `ZoneInfo` 校验并限制长度。配置项（DEFAULT_TIMEZONE、LLM 配置、JWT 密钥）在启动时校验，不对就拒绝启动。

```python
# 错：/v1/notes/n%00x 触发 PG 编码错误，返回 500
async def get_note(note_id: str): ...
# 对（任选其一）
async def get_note(note_id: NoNulStr): ...
async def get_note(note_id: Annotated[str, Path(pattern=r"^nte_[0-9a-f]{16}$")]): ...
```

机检 `INV-NUL`：FastAPI 路由里类型为 `str`、`str | None`、`Optional[str]`，或者没有 NUL 安全约束的 `Annotated[str, …]` 的 path 参数。接受 `NoNulStr`、`reject_nul`/`AfterValidator`、`Path/Query(pattern=…)`，以及 `{id:int}` 这类非字符串转换器。只读取装饰器里的路径，不解析 router 的 prefix。

## 4. 提交后副作用要能收敛（INV-3、INV-4、INV-5） <a id="side-effects"></a>

LLM、mem0、网络调用不能放在事务里。放在提交之后的副作用必须幂等、可重试，并且能收敛到当前状态。谁可以写/删记忆、按 note_id 收敛的语义见 INV-3/4/5（只有 memory_sync job 写删记忆），本文不另作规定。

怎么被发现的：第 1、3、4、5 轮在 organize 里连续出现（复活、误删、清理失败）。每补一个复查条件，就引出一个新的竞态窗口，说明需要换模型，而不是继续补条件。

## 5. 时间语义（无对应 INV） <a id="time"></a>

相对时间以录音时间 / 客户端时间为锚点；naive 时间按请求里声明的时区解释；提醒不能落在过去。5 条都出在 organize/ask 里，改这两个文件时一起看。

## 6. 不静默回落假实现（INV-10；机检 INV-NO-FALLBACK） <a id="inv-no-fallback"></a>

只有配置**显式**选择 mock/inmemory 时，才构造假实现；缺 key、未知值、生产环境配了假实现，都直接抛错。生产模板（.env.example、Compose、Dockerfile）必须选共享后端并安装它的依赖。

```python
# 错：缺 key 时悄悄用 MockLLM
if not s.llm_api_key: return MockLLM()
# 对
if s.provider == "mock": return MockLLM()
if s.provider == "openai": return OpenAI(...)
raise ValueError(s.provider)
```

机检 `INV-NO-FALLBACK`：已知的假实现（`MockLLM`、`FakeLLM`、`InMemoryStore`）出现在以下位置时报错：`except` 里、裸 `else:` 分支里、或条件是取反 / `is None` / 空串判断的 `if` 里。枚举选择器（`provider is Provider.MOCK`）和正常的生产类（如 `InMemoryRateLimiter`）不报。

## 7. allocate_seq 是事务里最后一把锁（INV-1；机检 INV-SEQ-LAST） <a id="inv-seq-last"></a>

与 #2 当前代码一致：先写入并 flush 业务行**和 job**（条件 UPDATE / FOR UPDATE / INSERT / enqueue），**最后**调用 `allocate_seq`，之后只用第二条 UPDATE 写 `change_seq`。`allocate_seq` 不能嵌在同时写业务列的 `.values()` 里。

```python
# 错（3bac263 的 PATCH，与 organize 构成 AB-BA 死锁）
await s.execute(update(Note).values(text=t, change_seq=await allocate_seq(s)))
# 对
await s.execute(update(Note).where(...).values(text=t, rev=Note.rev + 1))
await enqueue(s, "organize_note", {...})
await s.flush()
seq = await allocate_seq(s)
await s.execute(update(Note).where(Note.id == nid).values(change_seq=seq))
```

机检 `INV-SEQ-LAST`：在调用 `allocate_seq(sess)` 的同一事务里，之后出现同一 session 的 `sess.add/add_all/merge/delete/execute(…)`（只写 change_seq/updated_at 的 UPDATE 除外）或 `enqueue(sess, …)`。遇到 `sess.commit()/rollback()` 或离开 `with … begin()` 块就停止检查。其他对象上的方法（`set.add`、`store.delete`）不算。

## 8. sync：单快照 + 前缀 cursor（依赖 INV-1；无单独 INV） <a id="sync"></a>

一页的选择用一条语句完成（UNION ALL），PG 上用 REPEATABLE READ 并另开会话；cursor 只取页尾记录的 seq；依赖 #7（提交顺序 = 分配顺序）。

## 9. 多进程启动不做 DDL（INV-9） <a id="ddl"></a>

`create_all` / 迁移只在一次性的 bootstrap 步骤里运行（如 `app/cli.py`），不放在 API lifespan 或 worker 启动里。不做机检。

## 10. 并发相关的改动必须在 PG 上测（server-invariants.md §5 测试计划） <a id="pg"></a>

SQLite 是单写者，行锁、SKIP LOCKED、快照隔离在 SQLite 上都“自动正确”。PR #2 里 sync 快照、回收卡死 job、收尾 rev 这三个 P1 都只在 PG 上成立。并发敏感的改动必须有能在 `PG Tests` job 中真正并发跑的测试，缺失按 P1 报。
