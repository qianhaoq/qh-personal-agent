# Agent 反复踩的坑（server）

> **事实来源**：服务端不变量以 [docs/server-invariants.md](server-invariants.md)（全部 INV，当前 INV-1..INV-15，随 PR #2 起逐轮扩写）为准。与本文冲突时以它为准，并回来改本文。本文只收集“同一类问题被 Codex 反复提出”的模式和正反例，**不另立新的不变量**。
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

机检 `INV-NUL`：FastAPI 路由里类型为 `str`、`str | None`、`Optional[str]`，或者没有 NUL 安全约束的 `Annotated[str, …]` 的 path 参数。接受 `NoNulStr`、`reject_nul`、`Annotated` 里任何 `*Validator(...)`（Before/After/Plain/WrapValidator，含 `pydantic.X` 写法）、`Path/Query(pattern=…)`，以及 `{id:int}` 这类非字符串转换器；默认值或 `Annotated` 里带 `Depends(...)`/`Security(...)` 的参数（依赖注入，由依赖负责校验）直接跳过。只读取装饰器里的路径，不解析 router 的 prefix。

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
await enqueue(s, "extract", {...})
await s.flush()
seq = await allocate_seq(s)
await s.execute(update(Note).where(Note.id == nid).values(change_seq=seq))
```

机检 `INV-SEQ-LAST`：
本规则只报**警告**，不会让 CI 失败（名字解析和顺序判断都是启发式）：
- `allocate_seq` 嵌在 `update(...).values()` 里，且这个 `.values()` 还写了其他业务列。
- 只按源码顺序判断、不做控制流分析：在调用 `allocate_seq(sess)` 的同一事务里，之后又出现同一 session 的 `sess.add/add_all/merge/delete/execute(DML)`（只写 change_seq/updated_at 的 UPDATE 除外）或 `enqueue(sess, …)`。遇到 `sess.commit()/rollback()` 或离开 `with … begin()` 块就停止检查。

## 8. sync：单快照 + 前缀 cursor（依赖 INV-1；无单独 INV） <a id="sync"></a>

一页的选择用一条语句完成（UNION ALL），PG 上用 REPEATABLE READ 并另开会话；cursor 只取页尾记录的 seq；依赖 #7（提交顺序 = 分配顺序）。

## 9. 多进程启动不做 DDL（INV-9） <a id="ddl"></a>

`create_all` / 迁移只在一次性的 bootstrap 步骤里运行（如 `app/cli.py`），不放在 API lifespan 或 worker 启动里。不做机检。

## 10. 并发相关的改动必须在 PG 上测（server-invariants.md §5 测试计划） <a id="pg"></a>

SQLite 是单写者，行锁、SKIP LOCKED、快照隔离在 SQLite 上都“自动正确”。PR #2 里 sync 快照、回收卡死 job、收尾 rev 这三个 P1 都只在 PG 上成立。并发敏感的改动必须有能在 `PG Tests` job 中真正并发跑的测试，缺失按 P1 报。

## 11. 异常原文与用户内容不落**任何**日志面（INV-14；机检 INV-NO-RAW-EXC） <a id="no-raw-exc"></a>

应用代码里 `str(exc)` / f-string 插值 / `exc_info=True` 不进日志、DB 错误字段、错误响应——只记类型名 + 错误码（`app/errors.py::safe_error_text`）。**框架层同样会替你打日志**，这些口子应用代码看不见，必须专门收口（threads9 三个 P1 都出在这里）：

```python
# 错：消息文本可能内嵌 SQL 绑定参数（StatementError 的 str() 带笔记正文）或用户输入
logger.error("organize failed: %s", exc)                    # str(exc) 整段进日志
last_error = f"job failed: {exc}"                            # 原文落 jobs.last_error
# 对：类型名 + 粗粒度错误码
logger.error("organize failed: %s", safe_error_text(exc))
job.last_error = safe_error_text(exc)
```

框架层的口子与收口：

- **uvicorn access log**（默认开）：请求行含完整 query——`GET /v1/search?q=<笔记正文>` 整行进 stdout。收口：compose 与 Dockerfile 的 uvicorn 命令一律 `--no-access-log`；请求级日志用 `app/middleware.py::AccessLogMiddleware`，只记 method、路由模板、status、耗时，不记 query 与路径参数原值。
- **ServerErrorMiddleware 必 re-raise**：注册 `Exception` handler 只消毒响应体，Starlette 调完 handler 后总是 re-raise，原始 traceback 进 uvicorn.error 日志。收口：最外层 `SafeErrorFallbackMiddleware` 吞掉异常——记 `safe_error_text`、回固定 500、**永不 re-raise**。
- **SQLAlchemy echo / 第三方 logger**：`DATABASE_URL` 的 URL query 带 `echo=true` 会让引擎把 SQL+绑定参数打进 sqlalchemy.engine 日志（config 启动即拒）；httpx/httpcore/openai 的 logger 钉 >= WARNING（`app/logs.py::harden_third_party_loggers`，create_app 与 worker main 都要调）。
- **构建上下文也是泄漏面**：无 `.dockerignore` 时 `server/.env`（密钥）与本地 SQLite 笔记库会整体发给 builder——Dockerfile 不 COPY 也拦不住上下文传输。

机检 `INV-NO-RAW-EXC`：`except ... as e` 绑定的名字裸流进 logger 调用（含 `str(e)`/`repr(e)`/f-string 插值/`'%s' % e`/切片）、`logger.exception(...)`、`exc_info=<真值>`、或赋给 *error* 命名的属性（`last_error` 等）。`safe_error_text(e)` 这类 sanitizer 包装放行。

## 12. 等锁之后重算派生值（INV-13） <a id="recompute-after-lock"></a>

拿到锁才做的判定，不能用等锁**之前**算好的值。两个被 Codex 抓到的真实形态（threads9 P1，都在 auth.py 的 login）：

```python
# 错 1：邀请码抢占输方 rollback 重读后，仍拿等锁前的 now 判过期
#       → 等待期间跨过 expires_at 的邀请被接受、登录赢家账号
invite = await session.get(InviteCode, code)     # rollback 后重读，行值是新的
if invite.expires_at <= now:                     # 错：now 是等锁前取的
    raise ApiError(403, ...)
# 对：重读之后重取
now = utcnow()
if invite.expires_at <= now: ...

# 错 2：_token_pair 在 device upsert（行锁等待窗口）之前调用
#       → 短 ACCESS_TOKEN_TTL + 长等锁，返回的 access token 到手即过期
result = _token_pair(user_id, device_id)         # iat/exp 在锁前定下
await session.execute(_device_upsert(...))       # 在这里等锁
# 对：先 upsert（锁落定 + rowcount 复查），再 now = utcnow()、再 _token_pair、
#     再 _append_refresh_token（refresh 路径一直是这个顺序）
```

排查手法：`rg "with_for_update|pg_advisory|on_conflict|rowcount" server/app` 列出全部等待窗口，逐点问「锁后用于决策的 now / 计数 / 过期判定 / iat·exp，是锁内取的还是等锁前的？」纯展示时间戳与方向保守的判定可以豁免但必须注释论证；全仓锁点表见 server-invariants.md INV-13。
