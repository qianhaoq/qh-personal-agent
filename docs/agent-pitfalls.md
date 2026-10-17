# Agent 反复踩的坑（server）

> **事实来源**：服务端不变量以 [docs/server-invariants.md](server-invariants.md) 为准（随 PR #2 落地；落地前以 #2 中 AGENTS.md「写代码」各条为准）。本文只收集“同一类问题被 Codex 反复提出”的模式、正反例和怎么被发现的，帮助后来的 agent 少走回头路。两者冲突时以 server-invariants.md 为准，并回来改本文。
>
> 机检是 **best-effort 启发式**（AST），只覆盖常见写法，不证明代码满足不变量。漏检按 `P2: defer — heuristic checker; PG concurrency tests + human/Codex review are the backstop` Resolve；只有误报或 CI 坏了才算针对检查器的 P1。不要为补全覆盖面继续扩 AST。能机械检查的规则由 `scripts/check-invariants.py` 在 CI 的 `ci / Invariant Check (non-required)` 里执行；豁免写 `# invariant-ok[<规则id>]: <理由>`（不带规则 id 的 `# invariant-ok: <理由>` 只在该行恰好一条发现时生效），评审时要能说清理由。

## 统计（PR #2 共 5 轮 Codex + PR #3，2026-10-17）

共 45 条 Codex 发现（P0 0 / P1 21 / P2 24）。同一类问题平均要被提 2~3 轮，因为每次只修了被点名的那一行。

| # | 类别 | P1 | P2 | 合计 | 典型线程（PR #2 文件） | 机检 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 先读后写竞态（没有条件 UPDATE / ON CONFLICT / FOR UPDATE） | 6 | 2 | 8 | notes 乐观锁、refresh 轮换×2、邀请码首用、同设备并发登录、organize 写前锁行、幂等创建、序列初始化 | 否（靠 PG 并发测试） |
| 2 | Job 生命周期：回收 / 收尾的 rev 与租约 / 重试次数 | 3 | 3 | 6 | 回收卡死 running、收尾按 rev、抽取后 rev、最后一次重试、JOB_MAX_ATTEMPTS、合并过期任务 | 否 |
| 3 | 外部输入 / 配置没校验（NUL、时区、JWT 占位、LLM 配置） | 2 | 4 | 6 | NUL path 参数、JWT 占位符、timezone、DEFAULT_TIMEZONE、OpenAI 配置不全、未知记忆后端 | **INV-NUL** |
| 4 | 提交后副作用（记忆同步）没复查 / 没回滚 / 跨版本误删 | 3 | 2 | 5 | 重试前回滚副作用、删除后记忆复活、旧版本删掉新版本记忆、PATCH 后清理失败、外部调用持有事务 | 否 |
| 5 | 时间语义（相对时间锚点、naive 时间、过去的提醒） | 0 | 5 | 5 | organize/ask 的时间锚点、naive 时间、过去提醒、默认时区 | 否 |
| 6 | 静默回落到假实现 / 生产配置接错 | 2 | 2 | 4 | 镜像没装 mem0、.env.example inmemory、MockLLM 回落、健康检查不报错 | **INV-NO-FALLBACK** |
| 7 | 加锁顺序与 allocate_seq 位置 | 2 | 0 | 2 | PATCH 与 organize AB-BA 死锁、创建时 enqueue 在 seq 之后 | **INV-SEQ-LAST** |
| 8 | sync cursor / 快照一致性 | 2 | 0 | 2 | 截断页 cursor、两表两快照 | 否 |
| 9 | 多进程启动（create_all 并发 DDL） | 0 | 1 | 1 | main.py lifespan create_all | INV-STARTUP-DDL（警告） |
| 10 | CI / 只在 SQLite 上测（PG 专属 bug 被掩盖） | 0 | 3 | 3 | PR #3：测试布局、先跳过再起 PG、显式装 pytest；另：sync 快照、回收、收尾三个 P1 只在 PG 上成立 | PG Tests job |
| 11 | 其他（第三方 API 字段、软删过滤、文本归一化） | 1 | 2 | 3 | mem0 MemoryItem 字段、清理时排除已删提醒、填充词归一化 | 否 |

---

## 1. 先读后写必须原子 <a id="race"></a>

**规则**（server-invariants.md：并发 / 先读后写）：读出旧值再决定写什么的状态变更（乐观锁 rev、token 轮换、计数、首用占位、状态机），必须用条件 UPDATE（`WHERE 旧值` + 看 rowcount）、`INSERT … ON CONFLICT`，或 `SELECT … FOR UPDATE` 后复查。

```python
# 错：两个请求都读到 owner=None，各建一个用户
invite = await session.get(InviteCode, code)
if invite.owner_user_id is None:
    invite.owner_user_id = new_user_id

# 对：条件更新抢占，rowcount=0 说明输了，回滚后重读
res = await session.execute(update(InviteCode)
    .where(InviteCode.code == code, InviteCode.owner_user_id.is_(None))
    .values(owner_user_id=new_user_id))
if res.rowcount == 0:
    await session.rollback(); invite = await session.get(InviteCode, code)
```

**怎么发现的**：Codex 第 1、3、4 轮在 notes、auth 的不同函数里各提了一次，每次只修被点名的那个。**修一个就把同类全修**：`rg "session.get\(|scalar_one_or_none" server/app` 逐个看有没有“读后写”。

## 2. Job 收尾：租约条件 + 按当前状态收敛 <a id="jobs"></a>

**规则**（server-invariants.md：job 生命周期）：收尾必须带租约条件 `status='running' AND locked_by=:me AND locked_at=:claimed`，换过主的旧 worker 一律放弃收尾；判死的条件是 `attempts >= max_attempts`（attempts 是已经用掉的次数）；`max_attempts` 从配置里读。记忆同步按 note_id 幂等收敛（job 不带 rev，执行时读当前状态，入队去重，同一 note 串行）——**不要再写按 rev 比对的记忆规则**。

```python
# 错：不看租约，被回收的旧 worker 覆盖新主的结果
job.status = "failed"
# 对：锁行后核对租约，不匹配就放弃
job = (await s.execute(select(Job).where(Job.id == jid).with_for_update())).scalar_one()
if job.status != "running" or job.locked_by != me or job.locked_at != claimed_at:
    return None
```

**怎么发现的**：第 2~5 轮在 queue/runner 里连续出现，每轮改完 rev 条件又冒出新的边角情况（只同步记忆的重试）。最后的根治办法是换成“按当前状态幂等收敛”的模型，而不是继续补 rev。

## 3. 所有外部输入在边界校验（INV-NUL） <a id="inv-nul"></a>

**规则**（server-invariants.md：外部输入拒绝 NUL）：所有外部字符串（body、query、**path**、header 派生值）都要拒绝 `\x00`，在 schema 层返回 422；时区用 `ZoneInfo` 校验并限制长度；配置项（DEFAULT_TIMEZONE、LLM 三件套、JWT 密钥）在启动时校验，不对就拒绝启动。

```python
# 错：path 参数用裸 str，/v1/notes/n%00x 会触发 PG 编码错误，返回 500
@router.get("/{note_id}")
async def get_note(note_id: str): ...
# 对
async def get_note(note_id: NoNulStr): ...
```

**机检**：`INV-NUL` 会标出 FastAPI 路由里类型为裸 `str` / `Annotated[str, …]`（没有 `reject_nul`）的 path 参数，同模块 `APIRouter(prefix="/…/{x}")` 引入的参数也算（`include_router(prefix=…)` 跨模块的前缀暂不解析）。**怎么发现的**：第 4 轮给 body/query 加了 NoNulStr，漏了 path，第 5 轮 Codex 又提出来。

## 4. 提交后副作用要能收敛 <a id="side-effects"></a>

**规则**（server-invariants.md：记忆归属 / 谁可以删除）：LLM、mem0、网络调用不能放在事务里；放在提交之后的副作用必须幂等，并且“最终一致”：由按 note_id 串行、幂等的同步任务读取当前 note 状态来决定 add 还是 delete。不允许某个旧任务无差别地 `delete_by_note` 删掉别人写的记忆。

```python
# 错：旧任务发现自己过期，把这个 note 的全部记忆删掉（包括新任务刚写的）
await store.delete_by_note(uid, note_id)
# 对：入队一个 per-note 去重的 sync job，执行时读取当前 note：
#   已删除 → 删光；存活 → 让记忆与当前文本一致；同一 note 不并发执行
```

**怎么发现的**：第 1、3、4 轮在 organize 里连续出现（复活、误删、清理失败），每次补一个复查条件又引出新的竞态窗口。

## 5. 时间语义 <a id="time"></a>

**规则**：相对时间以录音时间 / 客户端时间为锚点，不用服务器 now；naive 时间按请求里声明的时区解释；生成的提醒不能落在过去。均为 P2，但 5 次都出在 organize/ask 里——改这两个文件时必须一起看。

## 6. 不静默回落假实现（INV-NO-FALLBACK） <a id="inv-no-fallback"></a>

**规则**（server-invariants.md：生产默认值安全）：只有配置**显式**选择 mock/inmemory 时才构造 Mock*/InMemory*；未知值、缺 key、生产配了假实现都要直接抛错。生产模板（.env.example、Compose、Dockerfile）必须选共享后端并安装它的依赖。

```python
# 错：配错 provider 或缺 key 时悄悄用 MockLLM
if s.provider == "openai" and s.key: return OpenAI(...)
return MockLLM()
# 对：显式选择，否则报错
if s.provider == "openai": ...
elif s.provider == "mock": return MockLLM()
else: raise ValueError(s.provider)
```

**机检**：`INV-NO-FALLBACK` 标出没有出现在 `== "mock"`/`"inmemory"` 分支里、前面也没有顶层“生产 / postgresql 即 raise”守卫的 `Mock*`/`InMemory*`/`Fake*` 构造（tests 目录不检查）。

## 7. allocate_seq 是事务里最后一把锁（INV-SEQ-LAST） <a id="inv-seq-last"></a>

**规则**（server-invariants.md：加锁顺序）：先锁 / 写全部业务行（条件 UPDATE / FOR UPDATE / INSERT / enqueue，业务行之间按 note → reminders → device…），显式 flush，**最后**才 `allocate_seq`，然后只用第二条 UPDATE 写 `change_seq`。`allocate_seq` 不能嵌在同时写业务列的 `.values()` 里。

```python
# 错（3bac263 的 PATCH，与 organize 构成 AB-BA 死锁）
await s.execute(update(Note).values(text=t, rev=Note.rev + 1,
                                    change_seq=await allocate_seq(s)))
# 错（4c1c9be 的 create）：seq 之后还 enqueue
note.change_seq = await allocate_seq(s); await enqueue(s, ...)
# 对
await s.execute(update(Note).where(...).values(text=t, rev=Note.rev + 1))
await enqueue(s, ...)
seq = await allocate_seq(s)
await s.execute(update(Note).where(Note.id == nid).values(change_seq=seq))
```

**机检**：`INV-SEQ-LAST`：同一函数里第一次调用 `allocate_seq` 之后，出现 `enqueue*`/`add`/`add_all`/`insert`/`merge`/`delete`/`with_for_update`，或者 `.values()` 写了 `change_seq`/`updated_at` 以外的列；以及 `allocate_seq` 嵌在写业务列的 `.values()` 里；同一循环体里既有写入又有 `allocate_seq` 时（下一轮的写入落在上一轮的 seq 之后）也会报。对 3bac263 回放能抓到当时的死锁行 notes.py:151 和 queue.py 的收尾顺序问题。

## 8. sync：单快照 + 前缀 cursor <a id="sync"></a>

**规则**（server-invariants.md：sync 一致性）：一页的选择用一条语句完成（UNION ALL），PG 上用 REPEATABLE READ 并另开会话；cursor 只取页尾记录的 seq；不变量依赖 #7（提交顺序 = 分配顺序）。

## 9. 多进程启动不做 DDL（INV-STARTUP-DDL，提议） <a id="inv-startup-ddl"></a>

**规则**（提议加入 server-invariants.md）：`create_all` / 迁移只在一次性的 bootstrap 步骤里运行（如 `app/cli.py`），不能放在 API lifespan 或 worker 启动里——多个 worker 会并发执行 DDL。机检会识别包装函数（函数体里有 `metadata.create_all` 的 helper，跨模块也算）在非 bootstrap 文件里的调用；目前只报**警告**，等 server-invariants.md 收录这条后再改成错误。

## 10. 并发相关的改动必须在 PG 上测 <a id="pg"></a>

SQLite 是单写者，行锁、SKIP LOCKED、快照隔离在 SQLite 上都“自动正确”。PR #2 里 sync 快照、回收卡死 job、收尾 rev 这三个 P1 都只在 PG 上成立。每个并发敏感的改动都要在 `server/tests` 里有一个能在 `PG Tests` job 中真正并发跑的测试。
