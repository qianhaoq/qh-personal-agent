# 服务端不变量与新模型设计（server-invariants.md）

> 状态：已实施（PR #2）。每条 INV 的「保证代码」与「验证测试」均为现存代码与测试（`test_invariants_doc_test_names_exist` 机械校验测试名存在）；机械检查器见 `scripts/check-invariants.py`。
> 范围：`server/`。本文回答两件事：**服务端必须永远成立的不变量**（INV-x，每条含精确陈述、保证代码、验证测试——测试名为第二步的计划名），以及**任务/记忆新模型**的设计与理由。
> 输入：threads4.txt 的 9 条 Codex 评审意见，下文以 C1–C9 指代：
> C1 auth 并发登录同设备竞态（P1）；C2 create 中 enqueue 晚于 allocate_seq（P1）；C3 openai 配置需三项齐备（P2）；C4 PATCH 后旧记忆无兜底清理（P2）；C5 整理后 rev 变化导致失败收尾永不命中（P1）；C6 create_all 并发建表（P2）；C7 DEFAULT_TIMEZONE 未校验（P2）；C8 path/query 参数 NUL 进 PG（P1）；C9 旧整理任务 delete_by_note 误删新 rev 记忆（P1）。
> 第五轮输入：threads5.txt 的 6 条（2 P1 + 4 P2），在对应 INV 内注明（INV-8 重写为多 refresh token 模型；INV-3 补枚举完整性；INV-5/6 补租约栅栏；INV-2 补内部字段不碰 updated_at；INV-7 补 aware 时间边界；INV-10 补后端可安装性/拓扑校验）。
> 第六轮输入：threads6.txt 的 6 条（3 P1 + 3 P2），在对应 INV 内注明：INV-5 补「丢租约必再入队」（迟到的 delete_by_note 会删掉换主后写入的记忆）；新增 INV-11（写接口乐观锁与 rev 语义，含幂等/无操作快路径）；新增 INV-12（条件更新/删除 0 行后的归因规则）；INV-8 补 refresh 行消失/过期的分类与过期行清理；INV-7 补 LLM 输出时间同套 ±14h 安全界；INV-10 补 MEM0_EMBEDDING_API_KEY。
> 第七轮输入：threads7.txt 的 5 条 P1（2 条 C 类 auth、1 条 A 类 memory_sync、1 条 compose；mem0_store.py 的 infer 一条**不在本分支修**，随生产记忆后端整体移交 feat/mem0-backend 分支），在对应 INV 内注明：INV-12 补「条件写后的重读必须绕过 identity map」（synchronize_session=False 不更新已加载对象）；INV-8 补「所有失败分支（含吊销/重放分支）先 _lock_device、锁内重读 token 行再判定」；INV-5 补「补偿入队必须在 try/finally——store.delete 抛异常也一定入队」；INV-9/INV-10 补「init-db 只校验数据库配置（校验分层），compose 各服务按 command 通过各自的启动校验（有测试）」。同轮把 mem0 生产后端拆分出去：`MEMORY_BACKEND` 取值变为 `inmemory | disabled`（新增空后端），mem0 相关实现细节从本仓移除、由后续 PR（feat/mem0-backend）提供，本文相应标注。
> 第八轮输入：threads8.txt 的 7 条（3 P1 + 4 P2），按类全仓排查后修：新增 INV-13（加锁后使用的派生值必须在锁内由重读数据重算，含 now；P1 auth.py:294）、INV-14（日志/数据库/错误响应不存异常原文与用户内容；P1 runner.py:71）、INV-15（改源字段必须让派生字段失效；P1 notes.py:215）；INV-5/6 补「置位 UPDATE 与租约校验原子」（P2 memory_sync.py:131）；INV-7 补「LLM 输出字符串剔除 NUL/不安全控制字符」（P2 extract.py:120）与「MockLLM 相对时间数量上界」（P2 mock.py:78）；INV-10 补「数值配置必须为正」（P2 config.py:99）。机械检查器同步新增 INV-NO-RAW-EXC（映射 INV-14）。
> 第九轮输入：threads9.txt 的 6 条（5 P1 + 1 P2），按类全仓排查后修：INV-13 补 login 的两个锁点（P1 auth.py:240 邀请码输方重读后沿用等锁前的 now 判过期；P1 auth.py:248 `_token_pair` 在 device upsert 锁前签发 iat/exp）并附全仓锁点核查表；INV-14 扩到**框架层与构建上下文**（P1 main.py:56 ServerErrorMiddleware 必 re-raise、P1 docker-compose.yml:44 uvicorn access log 带 query、P1 docker-compose.yml:21 无 .dockerignore——.env 与本地库进构建上下文；另 SQLAlchemy echo 的 URL query 向量、httpx/httpcore/openai logger 级别钉扎）；INV-7 补「MockLLM 十进制数量位数上界」（P2 mock.py:49，CPython int() 约 4300 位上限）。
> 第十轮输入：threads10.txt 的 4 条 P1，按类全仓排查后修：INV-13 补「条件更新/claim **成功之后**必须用成功时刻的 now 再验过期/签发」（P1 auth.py:211 邀请码 claim 的 rowcount==1 路径——PG READ COMMITTED 等锁后谓词重评仍用等待前绑定的 now 参数，等待期间过期的码照样抢占建号；同轮把租约签发 locked_at 归入签发时刻规则，queue.py `_mark_running`/`_reclaim_running` 改为写入时刻取值）；INV-14 补三条（P1 schemas.py:156「422 msg 固定文案不插值请求原值」——剥 input/ctx 不够，msg 本身就是回显面；P1 config.py:68「启动校验失败不得含密钥明文」——pydantic ValidationError 的 input_value 截断 repr 会露出尾部字段密钥，get_settings/get_db_settings 边界转 ConfigError；P1 README.md:52「文档启动示例必须 --no-access-log」——照文档起步的本地开发同样是日志收集面）。
> 第十一轮输入：threads11.txt 的 3 P1 + 1 P2 + Secret Scan 1 处，按类全仓排查后修：INV-1/8 补「logout 删 refresh_tokens 前必须先 `_lock_device`」（P1 auth.py:394——无锁普通读不与同设备并发 login/refresh 串行化，PG READ COMMITTED 下随后的 DELETE 看不到等锁期间未提交的新 token 行，204 返回后新签发的 token 仍有效）与「claim 循环判死（`_fail_expired` 锁 note + allocate_seq）后必须先提交再找下一个候选」（P1 queue.py:182——同一事务继续锁下一个 job/note 与并发 PATCH 构成 INV-1 明禁的 AB-BA）；INV-14.5 补 `.dockerignore` 覆盖 `*.sqlite3` 与其 sidecar（P1 .dockerignore:11——README 记载的本地默认库是 `app.sqlite3`，`*.sqlite`/`*.sqlite-*` 不匹配）；INV-7 补「MockLLM 时钟时分秒范围」（P2 mock.py:139——「3点99分」让 `replace(minute=99)` 抛 ValueError，确定性重试耗尽把有效笔记标 failed；越界不出提醒不上抛）。同轮收口 Secret Scan：threads10 写进文档的示例串（编造类密钥形态）触发 gitleaks generic-api-key，改为无类密钥形态占位并 `.gitleaksignore` 登记历史提交 fingerprint。
> 第十二轮输入：threads12.txt 的 3 P1 + 3 P2，按类全仓排查后修：新增 INV-16（「密钥拼进 URL 前必须 percent-encode」——P1 docker-compose.yml:24 三个服务把 `${POSTGRES_PASSWORD}` 原样嵌进 DATABASE_URL，密码含 `@:/#` 时 SQLAlchemy 解析组件错位、migrate 连不上而 api/worker 永不启动；改为 compose 注入分项 PG* 环境变量、应用侧 `postgres_url_from_env` quote(safe="") 拼接）；INV-8 补「设备凭证代际 fence」（P1 auth.py:82——reuse 吊销后同 device 再登录只清 `revoked_at`：吊销前的 access 随复活重新放行、被吊销旧 refresh 重放连环吊销每个新会话；`credential_generation` 吊销/复活 +1，get_auth/refresh 校验代一致否则普通 401）；INV-14 框架层补 `.gitignore` 与 `.dockerignore` 的 sqlite sidecar 模式对齐（P1 .gitignore:8）；INV-7 补「asr 体积按 UTF-8 字节量测」（P2 schemas.py:199）、「LLM 出站字符串清洗公共化 `app/schemas.py::clean_llm_str` 并覆盖 /v1/ask 的 answer」（P2 ask.py:70——转义 lone surrogate 让 Starlette JSON 编码 500）与「MockLLM 完整保留已校验 timezone」（P2 mock.py:222——UTC 漏捕回退 +08:00、三段 IANA 名被截断后 ZoneInfo 抛错）。

## 0. 总览：新任务与记忆模型

现状：单一 `organize_note` job 既做 LLM 抽取又做记忆写入（`memory_synced` bool + `memory_only` 补写路径），PATCH/DELETE 在提交后直接调 `MemoryStore.delete_by_note`，另有 `cleanup_memory` job 兜底。问题：两类职责共享 attempts、失败语义互相污染（C5）、记忆写删入口分散导致交错窗口（C4/C9）。

新模型只有两种 job，记忆写删入口唯一：

```
POST /v1/notes       ──► enqueue extract(rev=1)
PATCH /v1/notes/:id  ──► note: rev+1, status=uploaded, memory_status=pending
                         enqueue extract(rev=新rev)；enqueue memory_sync（pending 去重）
DELETE /v1/notes/:id ──► note: 软删；enqueue memory_sync（pending 去重）

extract(note_id, rev)    LLM 抽取 + 短写事务落整理产物（title/tags/todos/
                         suggested reminders；rev+1、status=organized、
                         memory_status=pending），同事务 enqueue memory_sync。
                         执行时 note.rev != rev → 跳过 = done。
memory_sync(note_id)     不带 rev。短读事务取 note 当前状态 →（事务外）删/写
                         记忆 → 复查 (rev, deleted_at) → 不一致则只删本次新增的
                         记忆 id 并再入队 memory_sync。记忆写删的唯一入口。
```

- 记忆归属：每条记忆 `metadata={note_id, source_rev}`，只属于 note 的当前 rev（INV-3）。
- 收敛目标（终态断言）：**记忆 == f(note 终态)**——已删除或未 organized → 该 note 无记忆；organized → 恰好一组记忆，对应 source_rev=当前 rev 的 text（INV-5 给论证）。
- `notes.memory_synced: bool` 替换为 `notes.memory_status: pending|synced|failed`（服务端内部状态，不进 API 响应、不占 change_seq）。
- `cleanup_memory` job 取消，语义并入 `memory_sync`。

---

## 1. 不变量

### INV-1 锁顺序（防 PG AB-BA 死锁）

**陈述**：

1. 同一写事务内，业务行锁按固定顺序获取：**invite → user → device → refresh_tokens → note → reminders → jobs**（refresh_tokens 只被 auth 路径触碰，且永远排在 device 之后，见 INV-8）。
2. 所有业务行的 INSERT/UPDATE（**含 job 行的 INSERT**，修 C2）显式 flush 落定之后，`allocate_seq`（sequences 行锁）必须是提交前获取的**最后一把锁**；change_seq 由其后的第二条 UPDATE 写回（行锁已持有，不新获取）。不得依赖 ORM autoflush 隐式落序。
3. **API/业务路径对既存 job 行只有 INSERT（`ON CONFLICT DO NOTHING`），永远不 UPDATE 既存 job 行**；既存 job 行的 UPDATE 只允许出现在队列内部（`app/jobs/queue.py` 的 claim / finalize / 回收判死路径），这些路径对业务行的加锁序列固定为 **jobs → note（仅失败标记）→ sequences → change_seq 写回**，不碰 reminders/device。因此「业务路径」与「队列路径」的锁序单向不相交，全库无环。**claim 循环里判死路径的转移必须即时提交（threads11 P1，queue.py:182）**：`_fail_expired` 已按上序锁了 note 与 sequences 之后，循环若**不结束当前事务**继续找下一个候选，会在持有 sequences 锁的同时再去锁下一个 job/note——与并发 PATCH（先持 note 行锁、正等 sequences）构成 AB-BA，正是本条要禁的环。判死转移（job→failed + note 标记 + seq 写回）commit 落定后再进入下一轮扫描；连带契约：`claim_next` 可能中途 commit，调用方不得把它包在 `session.begin()` 上下文管理器里继续用同一事务（runner `_claim_one` 在 claim 上下文退出后自行提交领取写入）。
4. 事务边界内不得调用 LLM / 记忆后端 / 任何外部网络服务（既有规则，保留）。

**各写路径加锁序列**（第二步实现后逐一对应）：

| 路径 | 加锁序列（左先右后） | seq 分配 |
| --- | --- | --- |
| POST /v1/notes（create） | note INSERT（新行）→ jobs INSERT extract（新行）→ flush → **allocate_seq** → note change_seq 写回 → commit。幂等冲突分支（client_note_id 撞唯一约束）整体回滚后只读复查，无二次加锁 | 1 |
| PATCH /v1/notes/:id | note 条件 UPDATE（锁行，乐观锁）→ reminders 条件 UPDATE（作废 suggested）→ jobs INSERT extract + INSERT memory_sync（均新行，DO NOTHING）→ flush → **allocate_seq** → note+reminders change_seq 写回 → commit | 1+N |
| DELETE /v1/notes/:id | note 条件 UPDATE（软删）→ reminders 条件 UPDATE（全量墓碑）→ jobs INSERT memory_sync → flush → **allocate_seq** → 写回 → commit | 1+N |
| extract 短写事务（apply） | note FOR UPDATE（PG）→ reminders（stale 条件 UPDATE + fresh INSERT）→ jobs INSERT memory_sync → flush → **allocate_seq** → 写回 → commit | 1+S+F |
| memory_sync 各短事务 | 读事务：note + job 行纯读无锁。外部调用：无任何会话/锁。置位事务（threads8 P2 起）：jobs 行（PG FOR UPDATE，锁内复核租约，锁序 = 队列路径 jobs → note）→ note 条件 UPDATE（memory_status，不分配 seq、不碰 updated_at/change_seq，见 INV-2.6）。再入队：jobs INSERT。**该路径永不碰 sequences** | 0 |
| 队列 claim | jobs 行（FOR UPDATE SKIP LOCKED / 条件 UPDATE）；循环里判死候选先走下一行 `_fail_expired` 并**即时 commit**（threads11）再继续扫 | 0 |
| 队列回收判死 _fail_expired | jobs 行 → note 条件 UPDATE → **allocate_seq** → 写回（jobs→note→sequences 单向）；**转移随本事务即时提交，绝不与后续候选的领取同事务**（threads11 P1：继续锁下一个 job/note 会与并发 PATCH 成 AB-BA） | 1 |
| 队列 finalize 成功 | jobs 行（FOR UPDATE + 租约条件） | 0 |
| 队列 finalize 失败 | jobs 行（FOR UPDATE + 租约条件）→ note 条件 UPDATE → **allocate_seq** → 写回 | 1 |
| POST /v1/auth/login | invite（读 + 首用条件 UPDATE）→ user INSERT（新行）→ device 原子 upsert（单条 INSERT…ON CONFLICT，见 INV-8）→ refresh_tokens INSERT（新行）+ 修剪 DELETE（本设备最老的超限行）。不碰 sequences | 0 |
| POST /v1/auth/refresh | device 行（PG FOR UPDATE 串行化轮换/吊销）→ refresh_tokens 条件 UPDATE（作废被轮换的那枚）+ 锁内重读归因（threads6 P1-3，纯 SELECT 无新锁）+ INSERT（新枚）+ 修剪 DELETE（超限活行 + 过期行）。不碰 sequences | 0 |
| POST /v1/auth/logout | device 行（`_lock_device`，PG FOR UPDATE——threads11 P1：原实现 `session.get` 无锁读，DELETE 快照看不到并发 login/refresh 未提交的新 token 行）→ refresh_tokens DELETE（清除该设备全部 token 行） | 0 |
| GET /v1/sync | 只读。PG 下 REPEATABLE READ 单快照，无行锁（既有设计，见 app/api/sync.py docstring，不变） | 0 |

**保证代码**：`app/common.py::allocate_seq`（docstring 已载规则，保留）、`app/api/notes.py`（create 重排：job INSERT 先 flush 再 allocate_seq，修 C2；PATCH/DELETE 移除提交后直删记忆）、`app/pipeline/extract.py`（原 organize 的 apply 事务）、`app/pipeline/memory_sync.py`（新）、`app/jobs/queue.py`（队列路径）、`app/api/auth.py`（INV-8）。全局规则同步写进 AGENTS.md 的既有条目。

**验证测试**（计划名）：
- `test_create_note_sql_order_job_insert_before_seq_update`（engine 事件监听 SQL 流，断言 jobs INSERT 先于 sequences UPDATE；SQLite+PG 双后端语义一致）
- `test_patch_sql_order_note_reminders_jobs_before_seq_update`（同上）
- 已有 PG 并发回归保留：`test_patch_and_organize_writeback_overlap_no_deadlock`、`test_patch_delete_organize_three_way_overlap_no_deadlock`
- 新增 `test_extract_finalize_concurrent_patch_no_deadlock`（PG，真并发：extract 耗尽_attempts 收尾 与 PATCH 同 note 并行，独立会话）
- `test_claim_commits_dead_job_transition_before_next_candidate`（threads11 P1，双后端：claim 循环判死一个过期 job 后领取下一个候选时，判死转移必须已提交——独立会话可见）
- `test_claim_dead_jobs_commit_between_candidates_no_deadlock`（threads11 P1，PG 真并发：判死 job A（持 note A + sequences）后扫到 job B 时，并发 PATCH 已持 note B 行锁、正等 sequences——正确实现下 PATCH 只等 note 行锁即放行，无 DeadlockDetected/500）
- `test_logout_waits_for_device_lock_then_deletes_concurrent_login_token`（threads11 P1，PG 真并发：login 事务持 device 行锁且新 token 行未提交时，logout 阻塞等锁；login 提交后 logout 的 DELETE 必须把新 token 行一并删掉——锁定「logout 204 ⇒ 该设备无存活 token」的可观察契约；旧实现靠 last_seen UPDATE 隐式满足它，显式 `_lock_device` 后不再依赖该偶然写）
- `test_business_paths_never_update_existing_job_rows`（静态断言：grep/AST 检查 app/api 与 app/pipeline/extract.py 中对 Job 的写仅为 INSERT）

### INV-2 任务模型：extract 与 memory_sync 两种 job

**陈述**：

1. `jobs.kind ∈ {extract, memory_sync}`（`organize_note`/`cleanup_memory` 移除；MVP 未上线，dev 库随 init-db 重建）。
2. extract 入队时携带 `note_rev`（新列）；执行时读 note：`deleted_at` 非空 → done；`note.rev != note_rev` → **跳过 = done**（过期任务无副作用）；否则 LLM → 短写事务 apply（rev+1、status=organized、memory_status=pending，同事务 enqueue memory_sync）。
3. memory_sync 不带 rev；**每个 note 至多一个 pending 的 memory_sync**：部分唯一索引 `UNIQUE(note_id) WHERE kind='memory_sync' AND status='pending'`，入队用 `INSERT … ON CONFLICT DO NOTHING`；running 期间再入队允许新建一个 pending（pending 唯一性只约束 pending 之间）。extract **不**做 pending 去重（同 note 多个不同 rev 的 pending extract 合法，旧rev 执行时自跳过）。
4. **同一 note 同时至多一个 running 的 memory_sync**（串行化，见 INV-5）：部分唯一索引 `UNIQUE(note_id) WHERE kind='memory_sync' AND status='running'` 数据库层硬保证。
5. 两类 job 各自独立的 attempts/max_attempts/失败语义：extract 耗尽 → `note.status='failed'`（条件：`rev=入队rev AND deleted_at IS NULL AND status!='organized'`）；memory_sync 耗尽 → `note.memory_status='failed'`（条件：note 未删除且**无其他** pending/running 的 memory_sync，`NOT EXISTS(... AND id != :job_id)`）。互不污染（修 C5：memory 失败不再触碰 note.status，extract 失败不触碰 memory_status）。
6. **内部状态更新不碰 `updated_at`/`change_seq`**（threads5 P2-2）：`memory_status` 是服务端内部字段，不进任何 API 响应（`note_to_dict` 不含它）、不占 change_seq——因此 `_mark_memory_status` 与 `_mark_memory_sync_failed` 的 UPDATE 显式写 `updated_at=notes.updated_at`（自赋值抑制列级 onupdate），`updated_at` 与 `change_seq` 都不动。反方向：对外可见字段的任何变更必须按 INV-1 分配 change_seq 并推进 `updated_at`。若允许内部更新推进 `updated_at` 而不变更 seq，`/v1/sync` 只回 seq 更大的行，客户端永远观察不到那个新时间戳（「不可观察的时间戳」）。

**保证代码**：`app/models.py`（Job 增 `note_id`/`note_rev` 列与三条部分唯一索引，`postgresql_where`/`sqlite_where` 双方言）、`app/jobs/queue.py::enqueue`（带 note_id/note_rev 与 DO NOTHING）、`app/pipeline/extract.py`、`app/pipeline/memory_sync.py`、`app/pipeline/runner.py::execute_job`（分发两种 kind）。

**验证测试**：
- `test_extract_skips_when_rev_moved_marks_done`
- `test_extract_attempts_exhausted_marks_note_failed_by_rev`（改造自旧的 test_failure_marks_note_failed）
- `test_extract_finalize_skips_note_patched_after_enqueue`（改造自现有同名测试）
- `test_memory_sync_single_pending_per_note`（并发/重复入队只剩一个 pending，双后端）
- `test_memory_sync_pending_allowed_while_running`
- `test_memory_sync_single_running_per_note`（PG，两 worker 独立连接并发 claim，断言任一时刻只有一个 running；SQLite 单写者天然满足，仅验证语义）
- `test_extract_and_memory_sync_independent_attempts`（extract 耗尽置 status=failed 不影响 memory_sync 运行；memory_sync 耗尽置 memory_status=failed 不影响 note.status=organized）
- `test_memory_sync_status_updates_do_not_touch_updated_at_or_change_seq`（threads5 P2-2 / INV-2.6：重放 memory_sync 前后 note 的 updated_at 与 change_seq 不变，/v1/sync 不新增行）

### INV-3 记忆归属：note_id + source_rev；后端契约（枚举完整、不越权改动、不推断合并）

**陈述**：MemoryStore 中每条由本服务写入的记忆，`metadata` 必含 `note_id` 与 `source_rev`；**记忆只属于 note 的当前 rev**——note 已删除或当前 rev 未 organized 时，store 中不应存在该 note 的记忆；organized 时存在的记忆其 source_rev 应等于 note 当前 rev。API 不向客户端暴露 source_rev（内部字段）。

**后端契约**（任何记忆后端实现——本仓的 inmemory/disabled，以及后续 feat/mem0-backend PR 的生产后端——都必须满足；threads5 P1-1 与 threads7 移交部分合并陈述）：

1. **全量枚举必须完整——一页取满又无法推进时必须报错，绝不当成已取全/已同步**：
   - `delete_by_note(user_id, note_id)`：按 metadata 过滤 + **边取边删**的循环（每轮删掉本轮取到的，下一轮自然前进，无需游标分页）；同一批 id 重复出现（删除未生效）即抛 `MemoryEnumerationIncomplete` 让 job 失败重试。
   - `list(user_id)`：一页取满且后端无游标/offset 可推进时**无法证明已取全**，直接抛 `MemoryEnumerationIncomplete`（不静默截断；调用方让 job 失败重试）。页大小可注入（`list_page_size`），测试用极小值模拟超限。
   - `delete_all(user_id)`：与 `delete_by_note` 同一个「过滤 + 边取边删」循环，返回确切删除数。
   - `InMemoryStore` 同语义：可注入 `list_page_size` 上限，`list()` 取满即抛同款错误（进程内字典本身无截断，上限只为让「取不全即报错」的行为脱离具体后端可测）。
2. **任何操作不得修改其他 note_id 的记忆**（threads7 移交）：删除/枚举的过滤必须同时约束 user 与 note；同步一个 note 绝不能触碰另一个 note 的记录（对端没有任务能修复被误删/误改的记忆）。
3. **add 不得基于既有记忆改写/合并（infer 类行为禁止，threads7 移交）**：`add` 必须是「插入属于本 note_id/source_rev 的新记录」——像 mem0 默认 `infer=True` 那样基于 user 级既有记忆做 UPDATE/DELETE/no-op 判定，会把同步 note B 变成改写 note A 的记录（A 无任务可修复，metadata 也不再满足归属模型）。生产后端必须用 `infer=False` 之类的原始插入，或等效地把推断约束在当前 note 内。

**生产后端（mem0）的实现细节已在 feat/mem0-backend 分支**（threads7 拆分）：mem0 2.2.1 的调用形状（`filters` 作用域、`top_k` 截断、`get_all` 无分页、`payload->>'note_id'` 过滤下推、`delete` 对不存在 id 抛 ValueError 的幂等吞掉）、版本钉死 `mem0ai==2.2.1`、psycopg 驱动与 embedding 凭据（原 INV-10.5/10.6）都随 `server/app/memory/mem0_store.py` 与 `requirements-mem0.txt` 移出本仓；该分支落地时按上面三条契约验收，契约本身不因后端换人而放宽。

**保证代码**：`app/memory/base.py`（`MemoryEnumerationIncomplete` + `add` 签名 `add(user_id, text, *, note_id, source_rev)` + 工厂只认 `inmemory | disabled`）、`app/memory/inmemory.py`、`app/memory/disabled.py`（空后端，见 INV-10.5）、唯一写入口 `app/pipeline/memory_sync.py`（INV-4/5 保证只有它写）。

**验证测试**：
- `test_memory_metadata_contains_note_id_and_source_rev`
- `test_no_stale_rev_memory_after_repatch`（PATCH 重整理后 /v1/search 不再命中旧文本；C9 的端到端回归）
- 枚举完整性（threads5 P1-1；现由 InMemoryStore 的可注入页上限承载，生产后端的等价测试随 feat/mem0-backend 回归）：`test_inmemory_list_page_cap_raises_when_full`、`test_memory_sync_fails_not_marks_synced_when_enumeration_incomplete`（枚举不完整 → job 失败重试、memory_status 不置 synced）

### INV-4 谁可以删/写记忆

**陈述**：调用 `MemoryStore.add / delete / delete_by_note` 的只有 **memory_sync job 的执行体**（`app/pipeline/memory_sync.py`）与账户级 `delete_all`（未来账户注销；当前无 API）。API 请求路径（PATCH/DELETE note）与 extract job **不得**直接改记忆，只能在写事务内 enqueue memory_sync（修 C4：不再依赖提交后 best-effort 直删）。

**保证代码**：`app/api/notes.py`（PATCH/DELETE 删除所有 `get_memory_store()` 直调）、`app/pipeline/extract.py`（不 import memory store）、`app/pipeline/memory_sync.py`（唯一入口）。

**验证测试**：
- `test_patch_and_delete_never_touch_memory_store_directly`（用记录型假 store：HTTP PATCH/DELETE 请求期间 store 写方法调用数必须为 0；排空 job 后调用只来自 memory_sync）

### INV-5 memory_sync 执行语义（按 note_id 幂等收敛）

**陈述**（单次执行的固定四步）：

1. **读**：短读事务读 note 的 `(deleted_at, rev, text, status, user_id)`，随即提交，不持锁。
2. **写外部**（事务外，无任何 DB 会话）：
   - `deleted_at` 非空 → `delete_by_note`；
   - 未删除且 `status='organized'` → `delete_by_note` 后 `add(text, metadata={note_id, source_rev=rev})`，**记录本次 add 返回的全部 memory id**（mem0 一次 add 可产生多条记忆）；
   - 未删除且未 organized（uploaded/failed）→ 只 `delete_by_note`、不 add（清掉属于旧 rev 的残留记忆；新的记忆等 extract 成功后由其入队的 memory_sync 补写。注意这里与字面「不写」的差异：**不清旧记忆就无法收敛**——PATCH 后 extract 永久失败时旧记忆必须消失，这正是 C4 的修复）。
3. **复查**：新短读事务重读 `(rev, deleted_at)` **并确认自己仍持有租约**（threads5 P2-1：`job 行 status='running' AND locked_by=:worker_id AND locked_at=:claimed`——外部写（mem0 的 LLM/embedding）超过 `JOB_LOCK_TIMEOUT_SECONDS` 时任务会被回收换主，仅比 `(rev, deleted_at)` 挡不住「复活慢 worker 的迟到写入」）。三种结局：
   - 租约仍在且 `(rev, deleted_at)` 与第 1 步相同 → 置位 `memory_status='synced'`（若 note 已 failed 且记忆为空则置 `failed`——该状态下不可能再有记忆）。**置位与租约校验必须原子（threads8 P2，memory_sync.py:131）**：复查读发生在已关闭的短读事务里，读与置位之间协程可暂停——租约被回收换主、新持有者删完记忆又停滞/失败后，旧 worker 迟到的置位仍会把 `memory_status='synced'` 写上、掩盖未收敛状态。实现：置位事务内先锁 job 行（PG `FOR UPDATE`；锁序 = 队列路径 jobs → note，INV-1）在 Python 里用 `as_utc` 复核 `(status, locked_by, locked_at)`（SQLite 存取不带时区，SQL 等值比较不可靠），租约仍在才发 note 的条件 UPDATE（`id=:id AND rev=读取rev AND deleted_at IS NULL`，不占 seq、不碰 `updated_at`/`change_seq`，见 INV-2.6）；租约已丢则**不置位**、静默让位——回收者即后继任务（INV-5 的 P1 前提），无需补偿再入队；
   - 租约仍在但 `(rev, deleted_at)` 已变 → **只删本次 add 产生的 memory id**（按 id 删，绝不 delete_by_note，修 C9）+ `INSERT memory_sync … ON CONFLICT DO NOTHING` 再入队，job 仍算 done；
   - **租约已丢（被回收换主）→ 只删本次 add 产生的 memory id、不置 memory_status，然后无条件再入队一次 memory_sync（`ON CONFLICT DO NOTHING` 与已有 pending 去重），不得静默返回**（threads6 P1-1：「删己 id」只对账自己 add 的部分，**第 2 步的 delete_by_note 本身也可能耗时超过锁超时、在换主执行完成之后才落地**，把新持有者刚写的记忆删掉而队列里再无后继任务——note 永久无记忆且 memory_status=synced。迟到 worker 的全部外部写（delete_by_note + add）在复查前已顺序完成，补偿任务在其后执行，读到最新状态幂等收敛；补偿任务拿新租约，若再丢租约同样再入队——每轮至多一个 pending（部分唯一索引），收敛有界）。**「删己 id」与补偿入队必须在 try/finally 里**（threads7 P1，memory_sync.py:123）：删己 id 的 `store.delete` 抛异常时执行会直接退出、补偿入队被吞掉——本 job 的收尾因租约已丢而被拒（无法重试），替换 worker 可能已完成，留下永不收敛的损害；正确形态是 `try: 删己 finally: 入队`，异常照常上抛让 job 按重试语义处理，入队无论如何发生（每 note 至多一个 pending，与别处入队去重）。
4. **收尾**：只校验租约（INV-6）。`(rev, deleted_at)` 足够作为复查键：status 无法在不改 rev/deleted_at 的前提下变化（PATCH 改 rev，DELETE 改 deleted_at，apply 改 rev）。

**没有锁保护的外部写之后必须无条件再对账一次（threads6 P1-1 的总原则，threads7 补强）**：任何绕过数据库锁的写（记忆后端的 add / delete / delete_by_note）都无法被栅栏挡住迟到落地；执行体在结束前必须保证「存在一个后继任务会按 note 当前状态收敛」——正常路径靠置位/换主路径靠补偿再入队，唯一例外是复查确认自己仍持租约且状态未变（此时本次执行就是终态收敛）。丢 lease 一律重新入队 memory_sync（唯一索引去重），不得静默返回。**补偿入队必须与它前面的清理外部写构成 try/finally**：清理（删己 id）抛异常时入队照样发生，否则对账承诺被一个可重试的临时故障打破且无人兜底（threads7 P1）。

**串行化方案的选择**（题目要求评估）：

- ❌ **A. PG session 级 advisory lock 覆盖外部调用**：把池化连接钉在秒级 mem0/LLM 调用上，违反本仓「外部调用不占连接」原则（ask/search 专门修过，见 `app/deps.py`）；池回收/断连会静默丢锁；SQLite 无对应物，两后端行为分叉；崩溃安全依赖连接关闭时机。
- ❌ **B. note 行 FOR UPDATE 短事务**：只能串行化「读」，覆盖不了外部调用窗口，无执行串行化效果（题目亦指出不能持锁调 mem0）。
- ✅ **C（选定）. job 行约束串行化**：claim 快路径加 `NOT EXISTS (running 同 note 的 memory_sync)` 过滤 + 部分唯一索引 `UNIQUE(note_id) WHERE kind='memory_sync' AND status='running'` 在**提交时硬保证**（两个 worker 并发把同 note 的两个 pending 都置 running，必有一个在提交时违反索引 → IntegrityError → savepoint 回滚该候选、继续找下一个）。不占连接、PG/SQLite 都支持、崩溃安全（worker 死 → 行仍 running → 既有租约回收语义照常）。
- ⚠️ **D（升级备选）. notes.memory_sync_owner 执行令牌**（条件 UPDATE 抢 note 行所有权）：更强的互斥，但多一列与「回收时偷令牌」逻辑；由于 INV-5 的复查/删己/再入队已保证正确性不依赖串行化（串行化只是减少重复外部调用与交错），当前量级不需要，记录为升级路径。

**正确性论证（收敛到「记忆 == f(note 终态)」）**：

关键前提 P1：**任何改变 note `(rev, deleted_at, status)` 的已提交事务都伴随 memory_sync 入队**——create/apply 入队（apply 事务内），PATCH/DELETE 入队（API 事务内），memory_sync 复查不一致再入队，**丢租约的执行也补偿再入队（threads6 P1-1：外部写的迟到落地不在任何 DB 栅栏内，唯一可靠的兜底是让队列再收敛一次）**。

设 note 终态 R（rev 单调递增且删除至多一次，状态变更次数有限）。对任一次执行 E：读快照 S → 外部写 W(S) → 复查。
- 若复查时状态仍为 S 且 S=R：W(R) 落定，之后状态不再变，终态记忆=f(R) ✓。
- 若复查时状态 ≠ S：删本次新增（精确到 id）+ 再入队；即使 W(S) 误删了别人写的记忆（delete_by_note 粒度），再入队的任务会以当前状态重放。requeue 链每节对应一次状态变更，长度有限 → 最终必有一次执行读到 R 并成功。
- 若复查时状态仍为 S 但 S≠R（变更在复查后才提交）：变更事务本身满足 P1，必然留下一个 pending memory_sync 处理 R ✓。

**并发情形逐项**：

| 情形 | 行为 |
| --- | --- |
| 并发 PATCH（memsync 执行中 rev 变） | 复查 rev≠读取 → 删己 + 再入队（与 PATCH 已入队的 pending 去重合并）→ 新任务按新 rev 收敛 |
| 并发 DELETE（执行中软删） | 复查 deleted_at≠NULL → 删己 + 再入队 → 新任务 delete_by_note → 终态无记忆 |
| 重复任务（pending 与 running 并存） | running 唯一索引串行执行；后一个读到的是前一个完成后的状态，幂等重放（delete_by_note 先清，无重复记忆） |
| worker 被杀（外部调用中途） | job 卡 running → 租约过期回收重跑（memory_sync 幂等）；attempts 用尽 → memory_status=failed（可见、可恢复）；被杀瞬间的部分外部写由重跑或下一个任务的 delete_by_note 收敛 |
| 租约过期被回收后原 worker 复活（慢 add 晚于回收落地） | 串行化索引约束的是 job 行状态，复活 worker 的执行不再被索引挡住——复查发现租约已丢 → **删掉本次 add 的 id、不置位、再入队一次补偿任务**（threads5 P2-1：不再仅凭 `(rev, deleted_at)` 未变就留下多写的一组记忆；threads6 P1-1：补偿任务兜底收敛）；其收尾因租约校验失效而放弃（INV-6），不会写 note；终态记忆由补偿任务收敛为恰好一组 |
| 复活 worker 的 delete_by_note 晚于换主执行落地（threads6 P1-1） | 旧 worker 停在 delete_by_note 里超过锁超时 → 新持有者把 job 跑完（写记忆、置 synced、done）→ 旧 worker 的删除此刻才执行，把正主的记忆删掉 → 旧 worker add 自己的一组 → 复查丢租约 → 删己 id + **再入队补偿** → 补偿任务重放 delete_by_note/add 收敛回恰好一组（没有补偿再入队时：note 永久无记忆、memory_status=synced、队列空） |

**保证代码**：`app/pipeline/memory_sync.py`（新文件，含四步与论证对应实现；租约栅栏接收 `job_id/worker_id/locked_at`，由 `app/pipeline/runner.py::execute_job` 从认领快照传入）、`app/jobs/queue.py`（claim 的 NOT EXISTS 快路径 + running 唯一索引冲突的 savepoint 处理）。

**验证测试**：
- `test_memory_sync_writes_memory_with_source_rev_and_marks_synced`
- `test_memory_sync_on_deleted_note_removes_memory`（替代原 cleanup_memory 系列语义）
- `test_memory_sync_deleted_path_always_calls_delete_by_note`（4c1c9be P2-1：已删除路径无条件 delete_by_note，哪怕 store 里本无记忆）
- `test_two_worker_delete_vs_add_converges_no_orphan`（4c1c9be P2-1 两 worker 交错：旧 cleanup 先跑完、慢 add 落地、worker 复查前崩溃 → 租约回收重跑清掉孤儿，最终搜不到）
- `test_memory_sync_on_unorganized_note_clears_stale_memory_without_add`（C4 回归：PATCH 后 extract 永久失败，旧记忆被清、无新增）
- `test_memory_sync_requeues_and_deletes_only_own_ids_on_rev_change`（C9 回归 / 4c1c9be P2-2：用 DelayedAddStore 模拟慢 add，PATCH 插入后旧任务只删自己写的 id，并发新写入立刻且最终都可检索；双后端语义验证）
- `test_memory_sync_converges_after_delete_during_add`（删除后不复活）
- `test_memory_sync_recheck_fences_reclaimed_lease_deletes_own_ids`（threads5 P2-1：add 窗口内租约被回收换主 → 慢 worker 复查删己 id、不置位，补偿再入队后队列收敛回恰好一组记忆）
- `test_memory_sync_mark_status_skipped_when_lease_lost_before_update`（threads8 P2：复查读之后、置位 UPDATE 之前租约被回收换主 → 置位事务锁内复核租约失败，memory_status 不被迟到 worker 盖成 synced）
- `test_memory_sync_stalled_delete_by_note_past_lease_requeues_and_converges`（threads6 P1-1：旧 worker 停在 delete_by_note 里超过锁超时、新持有者完成、旧删除迟到落地删掉正主记忆 → 复查丢租约后补偿再入队，排空后该笔记仍有记忆、/v1/search 可搜到；PG 必跑，交错用事件钩子强制故两后端同语义）
- `test_memory_sync_lost_lease_delete_failure_still_requeues`（threads7：丢租约路径删己 id 抛异常 → finally 仍留下补偿 pending；该执行收尾被 finalize_claim 放弃，吞掉入队即永久无人收敛）
- `test_memory_sync_rev_change_delete_failure_still_requeues`（threads7：状态已变路径删己 id 抛异常 → 异常上抛且补偿 pending 不缺席——直接推进 rev 制造「变更不留自己的 pending」的交错，补偿是唯一后继）
- `test_memory_sync_worker_killed_mid_run_reclaimed_converges`（模拟租约过期回收后重跑收敛）
- `test_memory_sync_attempts_exhausted_marks_failed_only_without_newer_job`（有更新的 pending/running 时不标 failed）

### INV-6 任务收尾校验

**陈述**：收尾必须带租约条件 `WHERE status='running' AND locked_by=:me AND locked_at=:claimed`（租约失效则放弃一切写入，既有规则）。在此之上：**extract 收尾失败标记额外受入队 rev 条件约束**（`note.rev=入队rev AND deleted_at IS NULL AND status!='organized'`）；**memory_sync 收尾只校验租约**（不带 rev，其失败标记 memory_status 受 INV-2.5 的「无其他 pending/running」条件约束）。回收判死路径（`_fail_expired`）同理按 kind 分流。threads5 P2-1 起这同一租约条件**也约束 memory_sync 执行体的复查**（INV-5 第 3 步）：置位前必须仍持有租约，丢租约的执行不得置位、只删自己刚写的 memory id——threads6 P1-1 起唯一允许的数据库效果是**补偿再入队**（INSERT 新 job 行，不改本 job/note；见 INV-5 第 3 步第三结局）。threads8 P2 补：**置位时刻的租约校验必须与 memory_status 的 UPDATE 原子**（复查读在已关闭的会话里，读与写之间租约可能被回收）——置位事务内锁 job 行复核 `(status, locked_by, locked_at)`，失败即放弃置位（回收者是后继，见 INV-5 第 3 步第一结局的原子化实现）。

**保证代码**：`app/jobs/queue.py::finalize_claim`、`_fail_expired`（按 kind 选择 note 标记条件）、`app/pipeline/runner.py::_finalize`、`app/pipeline/memory_sync.py`（复查的租约栅栏 + 置位事务内的原子租约复核）。

**验证测试**：
- `test_stale_worker_finalize_ignored_after_lease_reclaimed_extract` / `test_stale_worker_finalize_ignored_after_lease_reclaimed_memory_sync`（两种 kind 各一条）
- `test_extract_finalize_marks_failed_only_at_enqueued_rev`
- `test_memory_sync_finalize_failure_marks_memory_status_not_note_status`
- `test_memory_sync_recheck_fences_reclaimed_lease_deletes_own_ids`（threads5 P2-1：租约栅栏在执行体复查处生效）

### INV-7 外部输入硬化：NUL 拒绝 + 病态输入一律 4xx

**陈述**：任何可能进入数据库（或被拼进 LLM prompt / JSON 序列化）的外部输入，含 `\x00` 时必须得到 422，而不是 PG 编码错误 500。同类「编码/绑定/换算必炸」的病态输入也不允许 500（4c1c9be P2-4，全部拦成 400/422）：lone surrogate（UTF-8 编码失败）、非法 UTF-8 请求体（解码失败）、超深嵌套 JSON（解析器递归栈炸裂）、超大整数（SQL 绑定 OverflowError）、换算/本地化成 UTC 越界的极端日期（astimezone OverflowError）。threads5 P2-4 补：**aware 时间的边界与 naive 统一**——先转 UTC，再套用同一条 `[0001-01-02 00:00:00, 9998-12-31 23:59:59.999999]` 安全界（预留最坏 IANA 偏移 ±14h；此前 `9999-12-31T23:59:59Z` 配用户时区 Asia/Shanghai 能过 schema，却在抽取/提醒解析的 `astimezone` 处 OverflowError → note 被标 failed 而不是 422）。threads6 P2 补：**LLM 输出的时间同样套用这条 ±14h 安全界**——`extract` 的 `suggested_reminders[].fire_at` 解析（`_parse_fire_at`）把换算溢出（`astimezone` OverflowError/ValueError）或越界的值**当无效丢弃**（不生成该条 suggested reminder），绝不把异常上抛让 job 重试耗尽、把本来有效的笔记标 failed（`9999-12-31T23:59:59` 配 America/New_York 这类「语法合法、换算必炸」的值只能丢提醒，不能丢笔记）。threads8 P2 补两条：**(a) LLM 输出的字符串字段与请求体同一条硬化规则**——`_coerce_extraction` 对 title/tags/todos[].text/todos[].due/suggested_reminders[].title 一律剔除 NUL、不安全控制字符（C0 除 `\t\n`、DEL `\x7f`）与 lone surrogate（`json.loads` 会放过这些值，原样写 PG text/JSON 列必炸编码，确定性响应会耗尽重试把有效笔记标 failed）；剔除后变空的 tag/todo/提醒标题直接丢弃。**(b) MockLLM 的相对时间数量加上界**——`parse_datetime` 的「X分钟后/小时后/天后」分支统一走安全加法：`timedelta` 构造/相加溢出（`OverflowError`，如 `999999999999999999分钟后`）或结果越出 `utc_safe_datetime` 的安全界（如 `10000000天后` 超 datetime 可表示范围）都**不产生提醒**（返回 None），异常不上抛。**(c) MockLLM 十进制数量的位数上界（threads9 P2，mock.py:49）**——CPython 的 `int(str)` 对约 4300 位以上的十进制串直接 `ValueError`（`[0-9]+` 无位数限制，一条合法的 5000 字符笔记含「(4900+ 位数字)分钟后」时在 `_safe_relative` **之前**就炸，确定性重试全部失败 → 笔记被标 failed）。`_cn_to_int` 对十进制串限 **≤6 位**（999999 分钟 ≈ 1.9 年，更大数量本就荒谬）并捕获 `ValueError/OverflowError` 返回 None；**有意不用**正则 `[0-9]{1,6}` 限位——超长数字串的**末 6 位**仍会匹配「分钟后」前的窗口，把 5000 位数错当成 999999 分钟生成错误提醒；长度判定让整串不通过 → 该短语无提醒，语义正确。**(d) MockLLM 时钟表达式的时分秒必须落在合法范围（threads11 P2，mock.py:139）**——`X点Y分` 的时钟解析最终走 `datetime.replace(hour=…, minute=…)`，而分钟捕获组是 `[0-9]{1,2}`（0–99）、「九十九分」走中文数字同样可得 99：越界值让 `replace(minute=99)` 直接 `ValueError`，确定性重试全部失败、有效笔记被标 failed。规则：调整（下午 +12）后的**时 ∉ [0,23] 或分 ∉ [0,59]（秒若解析则 [0,59]，当前恒 0）→ 该短语解析不出时间（None，不出提醒），不上抛**——与 `_safe_relative`/位数上界同口径（病态输入丢弃派生物，绝不让异常上去耗尽重试）。小时**不做 `min(hour, 23)` 钳位**（threads11 起删除）：钳位会把「99点」错当成 23:00 出一条错误提醒，越界即无提醒语义才正确。界常量与判定函数放在 `app/schemas.py` 一处（`utc_safe_datetime`），请求体校验（SafeDatetime）、LLM 输出解析与 MockLLM 的数量上界共用，不允许各写一套边界。threads12 P2 补三条：**(e) asr 元数据体积按 UTF-8 字节量测（schemas.py:199）**——`len(json.dumps(...))` 数的是 Unicode 码点，非 ASCII 元数据按最多 3 字节/字放大：约 4000 个 CJK 字符（码点数 < 4096、序列化后 ~12KiB）骗过 4KiB 上限，存储/输入界实际被放大近 3 倍。判定改为 `len(payload.encode("utf-8")) > _MAX_ASR_JSON_BYTES`。**(f) MockLLM 完整保留已校验的 timezone（mock.py:222）**——`_extract_note_and_now` 不得用「必须含斜杠」的窄 regex 截取时区名：`UTC` 漏捕会静默回退 Asia/Shanghai（提醒时间整体偏 8 小时），`America/Argentina/Buenos_Aires` 被截成两段后 `ZoneInfo` 抛错、确定性重试全部失败把完全有效的笔记标 failed。改为完整捕获 `[A-Za-z0-9_+\-/]+`（无斜杠/多段均可）并经 `ZoneInfo` 构造校验后才采用，未知名保持默认不上抛（上游 `_check_timezone` 已保证真实请求里的时区名合法，这里只为 mock 直连调用兜底）。**(h) MockLLM 的时区/当前时间只从 system prompt 解析（threads13 P2，mock.py:210）**——`_extract_note_and_now` 的元数据捕获（`时区：…`/`当前时间：…`）不得扫 user 消息：笔记原文是**数据不是指令**，一条含「时区：UTC」或「当前时间：2099-…」的笔记会在 system 之后被处理、覆盖请求链路已校验的 timezone 与 recorded_at 参照，把相对提醒排到错误时刻。改为仅 `role == "system"` 的消息参与元数据解析（`<note>` 提取仍扫全部消息——笔记本体就在 user 消息里）。**(g) LLM 出站字符串清洗公共化并覆盖 /v1/ask 的 answer（ask.py:70）**——OpenAI 兼容供应商返回的 JSON 可含转义 lone surrogate：`resp.json()` 产出合法 Python str，原样进响应时 Starlette 的 JSON 编码 `UnicodeEncodeError` → 500。原 `_clean_llm_str` 从 extract.py 迁为公开的 `app/schemas.py::clean_llm_str`（转 str、剥 NUL/C0（留 `\t\n`）/DEL/lone surrogate、可选截断），extract 的抽取字段与 ask 的 answer 走同一条规则（「判定函数放 schemas.py 一处」的既有口径）；`/v1/search` 与 ask citations 的 `m.text` 源头是 note.text（入库前已过 NoNulStr + pydantic surrogate 拒绝，无旁路写入口），不在收口面内。统一方案 = **中间件 + schemas + 异常兜底三层**：

1. **中间件**（`app/middleware.py`，注册于 `create_app`）：对每个请求的 URL path 与 query string **解码后**检查 `\x00`（`request.url.path` 与 `query_params` 各值），命中直接返回 422 `{"error":{"code":"validation_error",...}}`，不进入路由。覆盖所有路由的 path/query 参数。
2. **schemas**（既有 `NoNulStr`/`reject_nul` 保留）：所有请求体字符串字段；path/query 参数声明处再加 `Annotated[NoNulStr, ...]`（如 `note_id`）作第二道防线。在此之上：typed str 字段的 lone surrogate 由 pydantic 自带拒绝（string_unicode）；`NoteCreate.asr` 走 `_reject_invalid_unicode_everywhere` 递归（NUL + surrogate + 嵌套深度 ≤ `_MAX_ASR_DEPTH=16`）；`SafeDatetime`（`NoteCreate.recorded_at`/`AskRequest.client_time`）对 **aware 与 naive 一视同仁**：换算/视为 UTC 后的值必须落在 `[0001-01-02 00:00:00, 9998-12-31 23:59:59.999999]`（任何 ±14h 内的 `astimezone` 都不再越界）；`NotePatch.rev` 约束在 int4 范围。
3. **异常兜底**（`app/errors.py::recursion_error_handler`，注册于 `create_app`）：超深嵌套在 json 解析阶段就 RecursionError（schema 校验根本看不到），拦成 422；非法 UTF-8 请求体由 Starlette 解码失败路径返回 400。

**覆盖面清单**：

| 入口 | 机制 |
| --- | --- |
| body：LoginRequest/RefreshRequest/NoteCreate/NotePatch/AskRequest 全部字符串字段 | schemas `NoNulStr`（既有；surrogate 由 pydantic str 校验拒绝） |
| body：NoteCreate.asr（dict 任意层级键/值） | `_reject_invalid_unicode_everywhere` 递归：NUL + lone surrogate + 深度 ≤16 |
| body：NoteCreate.asr 体积（threads12 P2） | `_asr_size` 按 `json.dumps(...).encode("utf-8")` 的**字节数**比较 `_MAX_ASR_JSON_BYTES`（CJK 等非 ASCII 按多字节放大后照样受限；原先数码点，~4000 个 CJK 字符 ≈12KiB 骗过 4KiB 界） |
| body：recorded_at / client_time | `SafeDatetime`：换算/视为 UTC 后的值必须落在 `[0001-01-02, 9998-12-31 23:59:59.999999]`（aware/naive 统一；预留 ±14h） |
| body：NotePatch.rev | `Field(ge=1, le=int4 上界)` |
| body：超深嵌套 JSON / 非法 UTF-8 字节 | RecursionError 处理器 422 / Starlette 解码失败 400 |
| LLM 输出：suggested_reminders[].fire_at（threads6 P2） | `_parse_fire_at` 复用 `utc_safe_datetime`：换算溢出/越界 → 丢弃该提醒（笔记照常 organized），不上抛异常 |
| LLM 输出：title/tags/todos[].text/todos[].due/提醒标题（threads8 P2） | `_coerce_extraction` 的 `_clean_llm_str`：剔除 NUL/C0 控制字符（保留 \t\n）/DEL/lone surrogate；剔后为空的条目丢弃（笔记照常 organized） |
| MockLLM：相对时间数量（threads8 P2） | `parse_datetime` 的 `_safe_relative`：timedelta 构造/相加溢出或结果越出 utc_safe_datetime 安全界 → 不产生提醒（None），不上抛 |
| MockLLM：十进制数量位数（threads9 P2） | `_cn_to_int` 限 ≤6 位并捕获 ValueError/OverflowError → 超长数字串（CPython int() 约 4300 位上限）不产生提醒、不上抛 |
| MockLLM：时钟时分秒范围（threads11 P2） | `parse_datetime` 时钟分支：调整后 hour ∉ [0,23] 或 minute ∉ [0,59]（秒同则 [0,59]，当前恒 0）→ 不产生提醒（None）、不上抛；小时不钳位（min(hour,23) 已删除——钳位会把「99点」错当 23:00 出错误提醒） |
| MockLLM：timezone 解析（threads12 P2） | `_extract_note_and_now` 完整捕获时区名（无斜杠的 `UTC`、多段 IANA 名均可）+ `ZoneInfo` 构造校验，未知名保持默认不上抛——不用「必须含斜杠」的窄 regex |
| MockLLM：时区/当前时间来源（threads13 P2） | `_extract_note_and_now` 的元数据只从 `role == "system"` 消息解析——笔记正文（user 消息）里的「时区：UTC」「当前时间：…」是数据，覆盖不了已校验值 |
| LLM 出站：/v1/ask 的 answer（threads12 P2） | `app/schemas.py::clean_llm_str`：转 str、剥 NUL/C0（留 `\t\n`）/DEL/lone surrogate——转义 lone surrogate 不再让 Starlette JSON 编码 500；与 extract 抽取字段同一条规则 |
| path：`/v1/notes/{note_id}` 等 | 中间件 + 参数 `NoNulStr` 注解（修 C8） |
| query：`/v1/search?q=`、`/v1/sync?since=&limit=`、`/v1/notes?cursor=&limit=` | 中间件（q 已有 `NoNulStr`）；since/cursor 补 `le=int8 上界`（超大整数绑定 OverflowError） |
| header | 当前**无任何 header 值入库**（authorization 只做 JWT 解析，不落库）；规则：未来任何从 header 取值入库的字段必须走 `NoNulStr`（自查清单项） |

**保证代码**：`app/middleware.py`、`app/main.py::create_app`（注册）、`app/schemas.py`（含 `utc_safe_datetime` 界判定；`clean_llm_str`/`_UNSAFE_LLM_CHARS`——LLM 出站字符串公共清洗，threads12 起 extract 与 ask 共用；`NoteCreate._asr_size` 按 UTF-8 字节量测）、`app/errors.py`、`app/api/notes.py`（path 参数注解、cursor 上界）、`app/api/sync.py`（since 上界）、`app/api/ask.py::ask`（answer 过 `clean_llm_str`，threads12 P2）、`app/pipeline/extract.py::_parse_fire_at`（LLM 输出同界）、`app/pipeline/extract.py::_coerce_extraction`（抽取字段过公共 `clean_llm_str`）、`app/llm/mock.py::parse_datetime/_safe_relative`（相对时间数量上界）、`app/llm/mock.py::_cn_to_int`（十进制位数上界，threads9 P2）、`app/llm/mock.py::parse_datetime` 时钟分支（时分秒范围判定，threads11 P2）、`app/llm/mock.py::_extract_note_and_now`（timezone 完整捕获 + ZoneInfo 校验，threads12 P2；元数据只从 system 消息解析，threads13 P2）。

**验证测试**：
- `test_nul_in_path_param_422`（`/v1/notes/n%00x` 的 GET/PATCH/DELETE）
- `test_nul_in_query_param_422`（`/v1/search?q=a%00b` 等）
- `test_nul_middleware_covers_all_routes`（遍历 app.routes，把每个含参路径注入 `%00`，断言 422 而非 500）
- 既有 `test_nul_rejected_in_note_strings_422` / `test_nul_rejected_in_auth_strings_422` / `test_nul_rejected_in_ask_and_search_422` 系列保留
- `test_lone_surrogate_in_body_rejected_422`（text / ask text / asr 键与值）
- `test_invalid_utf8_body_rejected_4xx`
- `test_deeply_nested_asr_over_depth_limit_422`（限内 202、超限 422）
- `test_absurdly_deep_body_recursion_guard_422`（解析期 RecursionError → 422）
- `test_huge_int_inputs_422`（PATCH rev / sync since / notes cursor）
- `test_extreme_datetime_inputs_422`（aware/naive 越界一律 422；边界内值仍 202）
- `test_aware_datetime_boundary_rejected_before_extraction_no_500`（threads5 P2-4：`9999-12-31T23:59:59Z` + `timezone=Asia/Shanghai` 被 422，不再落到抽取/提醒解析里 OverflowError）
- `test_llm_boundary_fire_at_overflow_discarded`（threads6 P2：LLM 返回 `9999-12-31T23:59:59+00:00`、`0001-01-01T00:00:00+00:00`、naive `9999-12-31T23:59:59`（America/New_York 换算溢出）→ 提醒丢弃、笔记照常 organized 不落 failed；界内未来时间照常生成）
- `test_llm_output_unsafe_control_chars_stripped`（threads8 P2：LLM 输出的 title/tag/todo/due/提醒标题含 `\x00`、`\x01`、lone surrogate → 剔除后落库（保留 \t\n），剔空的 tag/提醒丢弃，笔记照常 organized 不 500 不 failed）
- `test_parse_datetime_huge_relative_returns_none` / `test_mockllm_huge_relative_quantity_no_reminder_no_500`（threads8 P2：`999999999999999999分钟后`（timedelta 构造溢出）与 `10000000天后`（越出安全界）→ 不产生提醒；端到端笔记照常 organized、不 500 不 failed）
- `test_parse_datetime_overlong_digit_quantity_returns_none` / `test_mockllm_overlong_digit_quantity_no_reminder_no_500`（threads9 P2：5000 位数字 +「分钟后/天后」→ `_cn_to_int` 返回 None（CPython int() 4300 位上限不再触发 ValueError）；7 位数同样无提醒，≤6 位照常解析；端到端笔记照常 organized、不 500 不 failed）
- `test_parse_datetime_clock_out_of_range_returns_none` / `test_mockllm_out_of_range_clock_no_reminder_no_500`（threads11 P2：「下午3点99分」「九十九分」等分钟越界与「99点」「九十点」等小时越界 → 不产生提醒、不上抛（原实现 `replace(minute=99)` ValueError → 笔记 failed）；23:59/00:00 等边界值照常解析；端到端笔记照常 organized、不 500 不 failed）
- `test_asr_size_measured_in_utf8_bytes_cjk_rejected`（threads12 P2：码点数在 4KiB 内但 UTF-8 序列化超 4KiB 的 CJK asr → 422；贴界的 ASCII asr 照常 202）
- `test_ask_answer_sanitized_unsafe_chars_stripped`（threads12 P2：LLM 返回的 answer 含 lone surrogate/`\x01`/NUL → 200 且响应里被剥除（`\t\n` 保留），不再 UnicodeEncodeError 500）
- `test_extract_note_and_now_preserves_zone_without_slash_and_multisegment`（threads12 P2：`UTC` 与 `America/Argentina/Buenos_Aires` 经 `_extract_note_and_now` 完整保留，不再被窄 regex 漏捕/截断）
- `test_mockllm_utc_note_reminder_uses_utc_offset`（threads12 P2 端到端：timezone=UTC 的笔记其建议提醒 fire_at 偏移为 +00:00——原实现静默回退 Asia/Shanghai 出 +08:00）
- `test_extract_note_and_now_ignores_forged_metadata_in_note`（threads13 P2：笔记正文里的「时区：UTC」「当前时间：2099-…」不得覆盖 system 给出的已校验值）
- `test_mockllm_note_text_forged_timezone_keeps_system_tz`（threads13 P2 端到端：正文含伪造时区的提醒仍按 system 时区排点）

### INV-8 登录与 refresh：device 原子 upsert + 每设备多枚 refresh token

**陈述 1（核心承诺，threads5 P1-2）**：**只要登录（或 refresh）返回成功，返回的凭证在过期（`expires_at`，30 天 TTL 落库强制）或被显式作废（轮换消耗/设备吊销/登出/被每设备上限挤出）之前必须有效**——不存在「200 返回的 token 因为另一个并发 200 而天生无效」。

**陈述 2（存储模型）**：refresh 凭据不再挤在 `devices` 行上（旧模型单列 `refresh_token_hash` 后写者胜，正是并发登录互相覆盖的根因），改存 **`refresh_tokens` 表**（`id, device_id, user_id, token_hash UNIQUE, created_at, expires_at, revoked_at`；DDL 见第 3 节）：

- **登录只追加**：每次成功登录 INSERT 一行新 token（不覆盖任何既有行），同一事务内修剪：每设备至多保留 **5 枚**未过期未作废的 token，修剪条件**排除本行**（按 `created_at DESC, id DESC` 保留最新的 4 枚其他行）——本次返回的 token 一定幸存；同设备并发登录在 device 行 upsert 的行锁上串行化（锁序 device → refresh_tokens，见 INV-1）。修剪同时顺手 DELETE 该设备**已过期**（`expires_at <= now`，无论是否作废）的行（threads6 P2：见下方清理策略）。
- **refresh 轮换只作废被轮换的那一枚**：条件 `UPDATE refresh_tokens SET revoked_at=now WHERE id=:row AND revoked_at IS NULL AND expires_at>now`，**rowcount==1 才**发新 token（追加一行新 TTL）并修剪；**rowcount==0 时不得直接断定为重放（threads6 P1-3 / INV-12）**——首次读行（无锁）与拿到 device 行锁之间存在窗口，期间行可能已被第 6 次登录的上限修剪或 logout 删除，或恰好跨过 `expires_at`。必须在**已持有 device 锁的会话里重读该行**归因：
  - 行不存在（被挤出/登出删除）→ 401 `unauthorized`，**不吊销设备**（挤出/登出的既定语义就是普通 401）；
  - `revoked_at` 非空 → 确系被轮换/吊销后的重放 → 按复用吊销整台设备（原语义）；
  - `revoked_at` 为空却没轮换成功 → 只可能是等待锁期间跨过过期时刻（UPDATE 里的 `now` 是请求起始时刻）→ 401 `unauthorized`（过期），不吊销设备。
  - **重读必须绕过 identity map（threads7 P1，auth.py:280）**：轮换的条件 UPDATE 用 `synchronize_session=False`，不更新会话里已加载的 ORM 对象；随后的 `select(RefreshToken)` 会命中 identity map 里首读时装载的那份旧对象，把并发轮换刚提交的 `revoked_at` 读成 `None`——重放被误判成「过期 401」，设备与攻击者刚轮换到的新 token 都保持活跃。重读只查标量列（`select(RefreshToken.id, RefreshToken.revoked_at)`）或 `populate_existing=True`（细则与全仓清单见 INV-12）。
- **重放已作废（被轮换/吊销）的 token → 一律先 `_lock_device`，锁内重读 token 行后再判定（threads7 P1，auth.py:246）**。首读（无锁快照）看到 `revoked_at` 非空**不足以**直接吊销：等锁期间该行可能已被 logout 删除、或已跨过 `expires_at`（尚未被修剪）——INV-8 对这两类的定义都是普通 401、不吊销设备。锁内重读按固定顺序分类：
  - 行不存在（登出删除等）→ 401 `unauthorized`，**不吊销设备**；
  - 行已过期（`expires_at <= now`，无论是否作废）→ 401 `unauthorized`（过期），**不吊销设备**——先判过期再判作废；
  - 行存在、未过期、`revoked_at` 非空 → 确系重放 → 401 `refresh_reused` + 吊销**整台设备**（`devices.revoked_at` + 作废其全部 token；原语义保留）。
  被 5 枚上限挤出（行已删除）的 token 重放得到普通 401——包括「读行时还在、等锁期间被挤掉」的竞态窗口（重读归因保证它不会被误判成重放）。上限本身是显式作废策略的一种，写入已知取舍。
- **logout** → **先 `_lock_device`（PG FOR UPDATE，threads11 P1，auth.py:394）**再清除（DELETE）该设备全部 token 行（不吊销设备行，access 到期自然失效；登出后的重放按既有语义得普通 401，不触发复用吊销——包括等锁期间被登出删除的竞态，同上由重读归因兜住）。为什么必须显式锁：无锁的 `session.get` 只是读快照、不与同设备并发 login/refresh 串行化，PG READ COMMITTED 下随后的 DELETE 用语句开始时的快照，落在对方事务提交前就拿不到它新插入的 token 行——旧实现实际靠随后的 `last_seen` UPDATE（device 行锁）**隐式**挡住了这个窗口，该写一旦被移除/重排，204 就会漏删并发签发的 token；显式先取 device 行锁使「device → refresh_tokens」的文档锁序成立，logout 必然排在同设备先行事务之后，DELETE 一定看到其已提交的全部 token 行。
- 过期 token（`expires_at<=now`，未作废）→ 401 unauthorized（TTL 首次真正强制；不吊销设备）。
- **过期行清理策略（threads6 P2）**：登录/刷新追加新 token 的同一事务顺手 DELETE 该设备 `expires_at <= now` 的行——**已吊销（revoked_at 非空）但未过期的行保留到过期为止**（重放识别窗口：被轮换后 30 天内重放仍能关联到设备并触发吊销），过期后即无任何价值、随下一次该设备的登录/刷新清掉。活跃设备的表体积由此有界（≤5 枚活行 + 未过期的已吊销行）；完全失联设备的历史行不主动清理（可选的全局定期 GC 记为后续项， MVP 量级接受）。

**陈述 3（device 原子 upsert，修 C1，保留）**：登录对 device 的写入仍是**单条** `INSERT … ON CONFLICT (id) DO UPDATE … WHERE devices.user_id = EXCLUDED.user_id`（原子完成「存在性检查 + 归属检查 + 复活/刷新」，不再写任何 hash 列）：

- 无冲突 → 插入新设备行；
- 冲突且 user_id 相同 → DO UPDATE 清 `revoked_at`、刷新 `last_seen`（name/platform 仅在请求非空时覆盖，`COALESCE(NULLIF(EXCLUDED.x,''), devices.x)`）；
- 冲突且 user_id 不同 → WHERE 不成立，rowcount=0 → 复读该行确认后返回 **403 `device_conflict`**，不触碰原账号的任何 token。

并发语义：同设备同账号并发登录全部 200、**每个返回的 refresh token 都能成功 refresh**（各自一行，互不覆盖）；并发首登同设备（均无行）由 PK 冲突 upsert 原子解决；并发同枚 token 双刷新只有一个轮换成功，另一个按复用吊销设备。

**陈述 4（凭证代际 fence，threads12 P1，auth.py:82）**：refresh 复用吊销（`_revoke_device_for_reuse`）之后同一设备**再登录复活**时，只清 `revoked_at` 是不够的：

- 吊销前签发、尚未过期的 access JWT 会随复活重新通过 `get_auth`（它只看设备当前 `revoked_at`）——安全吊销被登录动作整体回滚；
- 吊销时被置 `revoked_at`、按陈述 2 保留作重放证据的旧 refresh 行，重放时再次命中复用吊销分支，把**新登录的会话**再吊销一次——同一个被盗 token 可以连环吊销之后每一次新登录，直到它自然过期。

fence 模型（`credential_generation` 整型代际，两表各一列、默认 0）：

- `devices.credential_generation` **只在两处 +1**：`_revoke_device_for_reuse` 吊销设备时（吊销同时 fence 掉本代全部未过期 access）；`_device_upsert` 的复活分支（`CASE WHEN devices.revoked_at IS NOT NULL THEN gen+1`）。**普通再登录/并发登录不动代**——陈述 1「200 返回的凭证不得因另一个 200 而失效」保持成立。login 在 upsert 落定后（device 行锁内）重读代，供 access 签发与 refresh 行落库（INV-13：锁后才用的派生值由锁内重读得出）。
- access JWT 携带 `gen` claim（`create_access_token`），`get_auth` 校验 claim 与设备当前代一致，不一致普通 401（不带 `gen` 的历史 token 一并失效——MVP 无存量 token 义务）；`refresh_tokens.credential_generation` 行创建后不可变（无任何 UPDATE 路径）。refresh 主路径（轮换前）与 `_revoke_device_for_reuse`（锁内重读归因时）都校验行代 == 设备当前代，不一致**普通 401：不轮换、不吊销**——旧代凭证既不随复活复活，也不能连环吊销新会话。
- logout 不动代（既有语义：登出不吊销设备行、access 到期自然失效；logout 已删该设备全部行，重放按陈述 2 得普通 401）。

**保证代码**：`app/api/auth.py`（login 的 upsert+追加+修剪、refresh 的条件轮换、logout 全作废；threads12 起代际 fence——upsert 复活分支条件 +1、login 锁内重读代、refresh 与 `_revoke_device_for_reuse` 的代校验、`_revoke_device_for_reuse` 吊销时 +1）、`app/models.py::RefreshToken`（init-db/create_all 建表）、`app/models.py::Device/RefreshToken`（`credential_generation` 列，threads12）、`app/auth/tokens.py`（`refresh_expiry`；`create_access_token` 的 `gen` claim）、`app/deps.py::get_auth`（代校验，threads12）。

**验证测试**：
- `test_login_device_bound_to_other_user_403`
- `test_concurrent_login_same_device_all_returned_tokens_refreshable`（5 个并发登录全 200 且每枚 refresh 都能成功 refresh；PG 真并发独立连接，SQLite 下退化为同语义）
- `test_concurrent_first_login_same_device_no_integrity_error`（PG，真并发）
- `test_refresh_token_cap_evicts_oldest_keeps_current`（第 6 次登录挤出最老的一枚；本次返回的一定可用）
- `test_refresh_rotation_revokes_only_rotated_token`（轮换只作废那一枚，同设备另一枚不受影响；重放被轮换的 → 吊销设备）
- `test_logout_revokes_all_device_refresh_tokens`
- `test_refresh_expired_token_rejected_401`（TTL 强制）
- `test_refresh_race_row_deleted_while_waiting_device_lock_401_no_revoke`（threads6 P1-3：读行后等锁期间行被删除 → 401 unauthorized、设备不吊销、同设备其他 token 照常刷新）
- `test_concurrent_sixth_login_eviction_vs_refresh_no_device_revoke_pg`（threads6 P1-3，PG 真并发：并发第 6 次登录挤掉最老 token + 并发用该 token 刷新——结果 ∈ {401 unauthorized, 合法 200}，绝不 refresh_reused、绝不吊销设备、其余 token 可用）
- `test_refresh_prunes_expired_rows_keeps_revoked_until_expiry`（threads6 P2：过期行在登录/刷新时被顺手清掉；已吊销未过期的行保留、其重放仍识别为 refresh_reused）
- `test_refresh_reuse_row_deleted_under_lock_401_no_revoke`（threads7：重放分支同样锁内重读——首读看到 revoked、等锁期间行被删除 → 401 unauthorized、不吊销设备）
- `test_refresh_reuse_revoked_but_expired_401_no_revoke`（threads7：分类顺序「不存在→401、已过期→401、已作废→重放吊销」——已作废且已过期的重放是普通 401，不为过期凭据吊销整台设备）
- `test_reuse_revoke_then_relogin_fences_old_credentials`（threads12 P1：复用吊销 → 同 device 再登录复活 → 吊销前的 access JWT 401（不随复活复活）、被吊销旧 refresh 重放普通 401 且**不** refresh_reused、不吊销新会话；新 access/refresh 照常可用）
- `test_plain_relogin_keeps_same_generation_all_tokens_valid`（threads12：无吊销的再次/并发登录不动代——先登录的 access 与 refresh 在后登录之后依旧有效）
- `test_concurrent_revive_logins_bump_generation_once_all_tokens_valid`（threads12：吊销后多并发复活登录，代恰好 +1（首个 upsert 见 revoked_at 非空），全部响应的 token 都可用；PG 真并发，SQLite 退化为同语义）
- 既有 `test_refresh_rotation_and_reuse_revokes_device`（重放语义不变）保留

### INV-9 建表/迁移：一次性 init-db；init-db 只校验数据库配置

**陈述**：`create_all` 不得在 api/worker 启动时并发执行（修 C6）；init-db 的配置校验按**分层**进行（threads7 P1，docker-compose.yml:24）：

- 新命令 `python -m app.cli init-db`：PG 下在**单个事务**内先 `SELECT pg_advisory_xact_lock(<固定key>)` 再 `Base.metadata.create_all`（DDL 事务性 + 咨询锁，并发执行安全且串行化）；SQLite 下直接 create_all。
- **init-db 只校验数据库层配置（`DbSettings`：DATABASE_URL）**，不加载完整 `Settings`——运行时记忆后端/LLM/JWT 与建表无关。migrate 服务在 compose 里只覆盖 `DATABASE_URL`，其余沿用 `.env`（本地模板默认 `MEMORY_BACKEND=inmemory`）；若 init-db 走完整校验，它会先撞上「生产拒绝 inmemory」退出，api/worker 等 `service_completed_successfully` 永远等不到，全新部署停在建表一步。
- `main.py` lifespan 与 `app/jobs/worker.py`：`DATABASE_URL` 为 postgresql 时**不**建表（表缺失时让首个查询快速失败，暴露部署错误）；SQLite（本地开发/测试）保留启动建表。
- `server/docker-compose.yml`：one-shot `migrate` 服务（`command: python -m app.cli init-db`），`api`/`worker` 改为 `depends_on: migrate: condition: service_completed_successfully`（postgres 健康后先跑 migrate）。**各服务的环境变量必须通过「按其 command 选择的启动校验」**：migrate → 数据库层校验（DbSettings）；api → 完整 Settings；worker → 完整 Settings + worker 拓扑校验（INV-10.4）。有测试解析 compose 与 `.env.example` 机械验证（env_file 指向 `.env`，测试用 `.env.example` 代替；生产必填的密钥类占位按 README 说明用生成值覆盖）。
- 测试不受影响：conftest 自行 drop/create（测试基建，非应用启动路径）。

**保证代码**：`app/cli.py`（init-db 子命令）、`app/db.py`（init_db 用 DbSettings 自建引擎；create_all 增加 PG 咨询锁包装或新函数）、`app/config.py::DbSettings/get_db_settings`（校验分层）、`app/main.py`、`app/jobs/worker.py`、`server/docker-compose.yml`、`server/tests/test_compose.py`。

**验证测试**：
- `test_init_db_concurrent_no_error_pg`（PG，真并发两个独立连接同时跑 init-db，均成功、无 duplicate-object 错误）
- `test_lifespan_skips_create_all_on_postgres`（PG 测试环境下 monkeypatch 计数 create_all 调用为 0）
- `test_init_db_sqlite_creates_tables`（SQLite 路径）
- `test_compose_services_pass_startup_validation`（threads7：PyYAML 解析 docker-compose.yml + `.env.example` 值 + README 记载的生产必填覆盖，按 command 分别验证 migrate→DbSettings、api→Settings、worker→Settings+worker 拓扑检查）
- `test_compose_migrate_would_reject_full_settings_layering_is_real`（反证：migrate 的环境走完整 Settings 会被「生产拒绝 inmemory」挡住——分层修的不是假问题）
- `test_init_db_does_not_load_full_settings`（毒化 get_settings 后 init_db 照常完成：建表路径不经过完整校验）

### INV-10 配置完备性校验

**陈述**（Settings 加载即校验，任何环境；数据库层的分层校验见 INV-9）：

1. `LLM_PROVIDER=openai` ⇒ `LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL` 三者必须齐备（修 C3：缺任一项启动即 ValueError，而不是运行期拼出相对 URL / 空 model 让每次 organize/ask 失败）。保留既有生产（postgresql）下 mock 拒绝等规则。threads13 P2 补：齐备之外，`LLM_BASE_URL` 还必须是**绝对的 http/https URL**（`urlsplit` 后 scheme ∈ {http, https} 且 netloc 非空，且不含任何空白字符）——裸主机名（`llm.internal`）、`ftp://`、相对路径这类值此前能过「非空」检查，/healthz 照常报 provider，而每次 extract/ask 都在 httpx 构造请求时才失败；任何环境生效，错误文案不含配置原值（INV-14 边界）。
2. `DEFAULT_TIMEZONE` 必须是合法 IANA 时区名（`ZoneInfo(...)` 可构造，修 C7；请求级 timezone 校验复用 `app/schemas.py::_check_timezone` 的逻辑）。
3. **`MEMORY_BACKEND` 只接受 `inmemory | disabled`**（threads7 拆分后；`mem0` 随生产后端移交 feat/mem0-backend，届时随该分支回到枚举）。未知值（含 `mem0`）启动即 ValidationError——绝不让拼错的后端静默退化丢记忆。
3a. **部署门槛**：本分支 compose（migrate/api/worker）以 `MEMORY_BACKEND=disabled` 通过各自启动校验（`tests` 解析 compose 验证），**但在 feat/mem0-backend PR 合入前不得部署生产**——disabled 下记忆功能恒为空，仅供联调/预发。README 生产一节同步写明。
4. **inmemory 只属于单进程**：进程内状态跨 api/worker 两进程互不可见。生产（postgresql）+ inmemory 默认拒绝启动，错误信息写明：inmemory 不能跨 api/worker 进程共享；生产记忆后端由后续 PR（feat/mem0-backend）提供，过渡期可显式设 `MEMORY_BACKEND=disabled`。仅单进程跑 PG 测试套件可设 `ALLOW_INMEMORY_IN_TESTS=true` 放行（原 `ALLOW_INMEMORY_IN_PROD` 改名：名字带 IN_PROD 误导部署者以为生产可开；**worker 进程无论该标志都拒绝 inmemory+PG**，`app/jobs/worker.py::check_memory_backend_shareable`——disabled 不受限）；SQLite 本地开发允许 inmemory，但 `.env.example` 与 README 写明其单进程局限（worker 写的记忆对 api 进程的 /search 不可见）。`.env.example` 的本地默认值因此是 `inmemory`。
5. **`disabled`：显式的空记忆后端**（threads7 拆分引入），任何环境（含生产/worker）都允许：
   - `DisabledMemoryStore` 实现 MemoryStore 全接口：`add` 返回 `[]`、`search`/`list` 返回 `[]`、`delete`/`delete_by_note`/`delete_all` 返回 0，无任何外部写。
   - **memory_sync 照常运行并把 `memory_status` 标 `synced`**：「记忆 == f(note)」在空后端下**平凡成立**——后端里永远没有记忆，任何收敛操作都是 no-op 且正确。不把 disabled 当失败（不标 failed、不无限重试）。
   - `/v1/search` 返回空 items、`/v1/ask` 走既有「无相关记忆」分支（`我没有相关记录。`），不报 500。
   - **为什么是显式 `disabled` 而不是别的形态**：(a) *null-but-pretend*（配个假实现假装在记忆）——假 store 仍会「成功写入」产生不存在的记忆语义，观察者（测试/排障）无法区分「功能关闭」与「功能在跑」；(b) *测试标志放行 inmemory*——把生产行为绑在 `ALLOW_INMEMORY_IN_TESTS` 上等于宣布「测试开关可以打开生产语义」，且 inmemory 在多进程下是真丢数据（不是关闭）；(c) *拒绝启动*——拆分后到 feat/mem0-backend 落地前，api/worker 分进程部署没有任何可用的记忆配置，compose 起不来。`disabled` 把「关闭」做成一个显式、可审计、语义自洽的配置值：零外部依赖、零外部写、状态平凡收敛。
6. （threads5 P2-3 的「mem0ai 安装探测」与 threads6 的「MEM0_EMBEDDING_API_KEY 回退链 / mem0 配置键与 psycopg 驱动」校验，随生产后端移至 feat/mem0-backend 分支，本仓不再有对应代码。）
7. **数值配置必须为正（threads8 P2，config.py:99）**：`WORKER_BATCH_SIZE<=0` 时 `run_pending_jobs(limit=...)` 的循环体一次都不进——worker 常驻、日志照常、却永远不处理任何任务（静默停摆）。同类数值字段一并约束：`ACCESS_TOKEN_TTL_SECONDS`、`REFRESH_TOKEN_TTL_DAYS`、`LLM_TIMEOUT_SECONDS`、`JOB_MAX_ATTEMPTS`、`JOB_LOCK_TIMEOUT_SECONDS`、`WORKER_POLL_INTERVAL_SECONDS`、`WORKER_BATCH_SIZE`、`ASK_TOP_K` 全部必须 > 0，启动即 ValueError（错误信息列出全部违规字段名），而不是各自静默破坏一种行为。新增数值配置时必须加进 `_require_positive_numeric_settings` 的清单（自查清单第 15 条）。

**保证代码**：`app/config.py`（`_require_complete_openai_config`、`_require_absolute_http_llm_base_url`（threads13）、`_validate_default_timezone`、`_reject_inmemory_memory_backend_in_production`——错误信息含 feat/mem0-backend 与 disabled 指引；`_require_positive_numeric_settings`——全部数值字段 > 0）、`app/memory/base.py::get_memory_store`（工厂只认 `inmemory | disabled`）、`app/memory/disabled.py`、`app/jobs/worker.py::check_memory_backend_shareable`、`server/.env.example`、`server/README.md`、`server/docker-compose.yml`（api/worker 显式 `MEMORY_BACKEND=disabled`）。

**验证测试**：
- `test_openai_missing_base_url_rejected` / `test_openai_missing_model_rejected` / `test_openai_missing_api_key_rejected`（已有 key 用例扩展）
- `test_openai_all_three_configured_ok`（另有 `test_openai_missing_two_fields_lists_both` 断言多项缺失一并报出）
- `test_default_timezone_invalid_rejected`（如 `Asia/Bejing`）/ `test_default_timezone_valid_ok`
- `test_worker_refuses_inmemory_with_postgres`（worker 启动检查，无视放行标志；disabled 不受限）
- `test_get_memory_store_unknown_backend_raises`（即使绕过 config 校验，store 工厂也不静默退化）
- `test_memory_backend_only_accepts_known_values`（枚举：`mem0` 等已拆分/未知值启动即拒）、`test_production_inmemory_rejection_mentions_mem0_backend_pr`（拒绝信息含 feat/mem0-backend 与 disabled 指引）、`test_production_disabled_memory_backend_ok`
- `test_disabled_store_empty_contract` / `test_factory_returns_disabled_store` / `test_disabled_memory_sync_marks_synced` / `test_disabled_search_and_ask_return_empty_not_500`（`tests/test_disabled_memory.py`：空后端契约、工厂、memory_sync 平凡收敛、读路径不 500）
- `test_non_positive_numeric_settings_rejected`（threads8 P2：每个数值字段配 0/-1 都启动即 ValidationError 且报出字段名；合法基线通过）
- `test_openai_malformed_base_url_rejected` / `test_openai_absolute_http_base_url_ok` / `test_mock_provider_skips_base_url_shape_check`（threads13 P2：裸主机名/空白/无 scheme/非 http(s) scheme 启动即拒；绝对 http(s) URL 放行；mock 不校验 base_url 形态）

### INV-11 API 写入的乐观锁与 rev 语义（threads6 P1-2）

**陈述**：携带版本号的写接口，rev 的比较必须发生在**任何提前返回之前**，且必须在加锁或条件更新中**原子**完成——「先无锁读、内容没变就直接 200」的快路径会绕过乐观锁，把过期视图谎报成当前态。

1. **哪些接口需要 rev**：
   - `PATCH /v1/notes/{note_id}`：请求体必填 `rev`（`NotePatch.rev`，int，1..int4 上界）——全仓当前**唯一**携带版本号的写接口。
   - 不需要 rev 的写接口及其理由：`POST /v1/notes`（幂等键是 `client_note_id`，无版本概念；重放命中返回数据库当前状态）；`DELETE /v1/notes/{note_id}`（删除是最后写者胜，无内容合并问题）；`POST /v1/auth/*`（凭据接口，无资源版本语义）；`POST /v1/ask`（无 DB 写）。未来新增写接口若表达「对已知版本的变更」（如 reminders 确认/完成），必须照本条携带并原子校验 rev。
2. **比较时机**：任何提前返回/幂等快路径**之前**；在加锁或条件更新中原子比较。PATCH 的两条路径：
   - 完整路径：`UPDATE notes SET … WHERE id=:id AND user_id=:uid AND rev=:body.rev AND deleted_at IS NULL`（条件更新即乐观锁，rowcount==0 → 回滚重读归因，见 INV-12）；
   - 内容不变的快路径（`body.text` 为空或等于当前 text）：先做 `UPDATE notes SET updated_at=updated_at WHERE … AND rev=:body.rev AND deleted_at IS NULL`——自赋值 UPDATE 只取行锁、不改任何列（显式赋值抑制 onupdate，不产生可观察时间戳），rowcount=1 即「rev 在锁内仍匹配」的原子判定；0 行同样回滚重读归因。持锁后 `refresh` 权威读，内容仍相等才返回 200；期间变到不等则落回完整路径。
3. **409 返回体**（沿用 `app/errors.py::error_body` 的统一两段式，当前实现**不回传当前 rev**——客户端收到 409 后应 GET 最新版本再重放）：
   ```json
   {"error": {"code": "conflict", "message": "版本冲突，请拉取最新版本后重试"}}
   ```
   快路径与完整路径的 409 体完全一致；归因为 404（不存在/已删除/他人笔记）时同构返回 `{"error": {"code": "not_found", "message": "笔记不存在"}}`。
4. **幂等快路径也必须先校验 rev**：拿过期 rev 的无操作 PATCH 是 409，不是 200——否则客户端会误以为自己的过期视图就是当前态（threads6 P1-2：读 rev=1 text=X 后并发 PATCH 落了 rev=2 text=Y，`{"text":"X","rev":1}` 得到 200 + rev=1 的旧 note，DB 实际是 Y）。

**保证代码**：`app/api/notes.py::patch_note`（快路径的自赋值条件校验 + `_raise_conflict_or_not_found` 复查归因）、`app/schemas.py::NotePatch`、`app/errors.py`。

**验证测试**：
- `test_patch_unchanged_text_with_stale_rev_409`（threads6 P1-2：过期 rev + 内容不变 → 409；过期 rev + 旧内容 → 409；当前 rev + 内容不变 → 200，rev/updated_at 不动、不新增 job）

### INV-12 条件更新/删除匹配 0 行后的归因规则（threads6 P1-3）

**陈述**：条件 UPDATE/DELETE 匹配 0 行（`rowcount==0`，或本应存在的行 `scalar_one_or_none()` 得 None）时，**必须重读当前状态（或给出等价的因果封闭论证）才能断定原因**——0 行只说明「条件此刻不成立」，不说明你猜的那个原因成立。按猜测归因（当成重放/冲突/已删除/他人已处理）会把语义上无辜的并发交错误伤成对外可见的错误（错吊销设备、错 409/404）。

实现形态（择一，写代码时注明）：

- **回滚后重读**：`rollback` → 重新 SELECT 该行 → 按当前值分类。正面样例：PATCH 的 404/409 复查（`_raise_conflict_or_not_found`）、login 邀请码抢占后重读（owner 已有 → 登录该 owner）、login device upsert rowcount==0 后重读（异 user → 403）、refresh 轮换 rowcount==0 后**锁内**重读 token 行（不存在 → 401 不吊销 / revoked → 重放吊销（再经 INV-8 的锁内复核）/ 未 revoked → 过期 401）。
- **锁内重读（不回滚）**：持有行锁的会话里直接重读归因（refresh 的 token 行）。**必须绕过 identity map（threads7 P1，auth.py:280）**：条件 UPDATE/DELETE 用 `synchronize_session=False` 时**不会**更新该会话先前已加载的 ORM 对象，而 `select(Model)` / `session.get` 对已存在于 identity map 的行返回旧对象且不刷新其未过期属性——「重读」拿到的其实是首读快照（实测：并发轮换提交后，loser 的重读仍把 `revoked_at` 读成 `None` → 重放被误判成过期 401、设备保持活跃）。写法二选一：只查标量列（`select(M.col1, M.col2)`，Row 不是实体、不过 identity map）或 `.execution_options(populate_existing=True)`。**rollback 后重读天然安全**：`Session.rollback()` 默认把 identity map 全部过期（实测验证），本仓「条件写 0 行 → rollback → 重读」的各处因此不受影响；新代码不得依赖「碰巧没缓存」，一律按本条两种形态之一写。
- **因果封闭论证豁免**：若能证明「0 行的所有可达成因都导向同一个正确动作」，可免重读，但必须在代码注释里写明论证。现仓豁免点：DELETE note 的 0 行 → 404（进 UPDATE 前刚确认过行存在，条件只有 `deleted_at IS NULL`，无 un-delete 路径 ⟹ 唯一成因是并发软删）；队列 claim/reclaim/finalize 的各 0 行 → 跳过该候选（成因 ∈ {被抢/已收尾/已回收}，跳过对一切成因都正确）；`_mark_memory_status`/`_mark_extract_failed`/`_mark_memory_sync_failed` 的 0 行 → 不标（条件里显式携带归因前提，前提不满足即不该标；且 (rev, deleted_at) 变化必伴随后继任务，INV-5 P1）。

**保证代码**：`app/api/notes.py`、`app/api/auth.py`、`app/jobs/queue.py`、`app/pipeline/memory_sync.py`（上述各点，含注释论证）。

**验证测试**：
- `test_refresh_race_row_deleted_while_waiting_device_lock_401_no_revoke`（0 行的成因是「行被删除」而非「重放」：错误归因会吊销整台设备）
- `test_refresh_race_revoked_while_waiting_device_lock_detected_as_reuse`（threads7：0 行的成因是「并发轮换作废」，锁内重读必须绕过 identity map——旧行为命中首读旧对象，把重放误判成过期 401、设备保持活跃）
- `test_concurrent_sixth_login_eviction_vs_refresh_no_device_revoke_pg`（同上，PG 真并发窗口）
- 既有正面样例回归：`test_concurrent_patch_same_rev_exactly_one_success`（PATCH 0 行 → 重读归因 409/404）、`test_login_device_bound_to_other_user_403`（upsert 0 行 → 重读归因 403）

### INV-13 加锁后使用的派生值必须在锁内重算（threads8 P1，auth.py:294）

**陈述**：拿到一把锁（`FOR UPDATE` / 条件 UPDATE / advisory lock）之后用于**决策**的每个派生值——过期判定用的 `now`、状态、计数、rev、`expires_at`、是否活跃等——必须满足下列之一，不得直接沿用**等锁之前**算好的值：

1. **锁内重取**：`now` 之类的时间基线在锁内重新取（`utcnow()`）；
2. **锁内重读**：来自行数据的派生值由锁定行的当前值得出——显式重读（绕 identity map，见 INV-12）或由条件 UPDATE 的 WHERE 对行当前值**原子判定**；
3. **版本栅栏**：锁前的快照值被一个锁内原子复查的版本号（如 note 的 rev）守护——快照之后任何会改它的写都必然推进版本，复查不过即放弃（extract apply 对 `timezone/recorded_at` 的用法）。
4. **成功后复验（threads10 P1，auth.py:211）**：条件 UPDATE 的 WHERE 里以**绑定参数**表达的墙钟判定（`expires_at > :now`），在 PG READ COMMITTED 等锁重评时**不重绑参数**——等待期间跨过 expires_at 的行仍会 rowcount=1。凡条件更新/claim/拿锁**成功**之后仍依赖该时间判定的，必须用成功时刻重取的 now 再验一次；**签发类时间戳**（租约 `locked_at`、凭证 iat/exp）同理取**写入时刻**的 now，不用等锁前算好的值。

违反样例：

- threads8 P1（auth.py:294）：`refresh` 在请求起始取 `now`，拿到 device 行锁后的轮换条件 UPDATE 仍用 `RefreshToken.expires_at > now`（旧 now）判过期——token 首读有效、等锁期间跨过 `expires_at` 时已过期的 token 仍被轮换成新凭证（200），「等锁期间跨过期」的归因分支（INV-12）对此场景永不可达。修复：`_lock_device` 之后重取 `now`，条件 UPDATE 对行内当前 `expires_at` 原子判定。
- threads9 P1（auth.py:240，login 邀请码输方）：并发首登同一邀请码，输方的条件 UPDATE 在 invite 行锁上等待赢家事务，输了之后 rollback 重读已提交状态，过期比较却仍用**等锁之前**取的 `now`——等待期间邀请码跨过 `expires_at` 时，输方接受已过期邀请并登录赢家账号（晚于此时刻开始的同一登录反而 403）。修复：`_resolve_lost_invite` 重读之后重取 `now = utcnow()` 再判过期。
- threads9 P1（auth.py:248，login 锁前签发）：`_token_pair`（内部取 `datetime.now` 定 iat/exp）在 `_device_upsert` **之前**调用——device upsert 的行锁是本接口最长的等待窗口，短 `ACCESS_TOKEN_TTL_SECONDS` + 长等待下返回的 access token **到手即过期**。修复：`_token_pair` 移到 upsert（及 rowcount 复查）之后，并在锁后重取 `now` 供签发与 `_append_refresh_token`（与 refresh 路径同序）。
- threads10 P1（auth.py:211，claim 成功后不复验）：`_claim_invite_first_use` 的 rowcount==1 路径——UPDATE 在 invite 行锁上等待赢家事务，醒来后 PG 用等待前绑定的 now 重评 `expires_at > :now`，等待期间过期的邀请码仍抢占成功并创建账号，绕过 `_resolve_lost_invite` 的过期归因。修复：抢占成功后（仍持行锁）标量列重读 `expires_at`、重取 `utcnow()` 复验；已过期则回滚本次 claim（owner/used 撤销）并按过期 403，不签发任何凭证。
- threads10（queue.py `_mark_running`/`_reclaim_running`，租约签发时刻）：`locked_at` 写的是 claim_next 循环开头的 now——`SELECT … FOR UPDATE` 等锁消耗的时间会等量缩短租约（等待越久越早被回收，活 worker 被误判失灵）。修复：签发时刻在函数内重取 `utcnow()`；claim 循环外用于 `run_after<=now`/`locked_at<cutoff` 过滤的旧值维持既有边界论证（方向保守）。

**全仓锁点核查表**（threads9 A 类全仓清扫；「锁点」= 存在等待窗口的写路径，逐点核查锁后用于决策的派生值来源）：

| 锁点 | 等待窗口 | 锁后派生值 | 结论 |
| --- | --- | --- | --- |
| auth.login 邀请码首用条件 UPDATE（抢占） | invite 行锁 | 过期判定的 now 是**语句参数**（等锁重评不重绑，threads10 P1）；used/owner 为行值谓词（原子判定） | **已修（threads10）**：rowcount==1 后锁内重取 now 复验过期，过期回滚 claim + 403 |
| auth.login 抢占输方重读（threads9 P1 修） | 同上（等待后输） | 过期判定的 `now` | **已修**：重读后重取 now |
| auth.login device upsert → 签发/追加/修剪（threads9 P1 修） | device 行锁（PK 冲突串行化） | iat/exp、`_append_refresh_token` 的 created_at/prune 基线、复活分支的 `credential_generation`（threads12：签发要绑的代由 upsert 的条件 +1 决定，必须锁内重读） | **已修**：`_token_pair`、now 与代际重读都在锁后；`last_seen` 陈旧为纯展示边界 |
| auth.login device upsert rowcount==0 复查 | 同上 | 无时间判定（归属比较） | 正面样例（rollback 后重读，INV-12） |
| auth.refresh（threads8 P1 修） | device 行锁 | 轮换/过期判定的 now、iat/exp、追加/修剪基线 | 正面样例：`_lock_device` 后重取 now、锁后签发 |
| auth._revoke_device_for_reuse（threads7 修） | device 行锁 | 过期比较与写入的 utcnow | 正面样例：锁内取 |
| auth.logout | device 行锁（`_lock_device`，threads11 修：原 `session.get` 无锁，DELETE 看不到并发 login/refresh 未提交的新 token 行） | last_seen（纯展示，锁后取） | 正面样例（threads11）：删 refresh_tokens 前先锁 device，锁序 INV-1/8 |
| notes.patch_note / delete_note | note 条件 UPDATE（即锁） | 0 行复查无时间判定；`updated_at=utcnow()` 语句构造时求值 | 复查正面；时间戳为 INV-13 边界（纯展示） |
| extract._apply_extraction（PG note FOR UPDATE） | note 行锁 | 提醒窗口判定的 now、timezone/recorded_at 快照 | 正面样例：锁内 now + rev 版本栅栏 |
| memory_sync._mark_memory_status 置位事务 | job 行 FOR UPDATE | 租约复核（等值比较锁内行值，非时间派生） | 正面样例 |
| queue.claim（PG FOR UPDATE SKIP LOCKED / SQLite 条件 UPDATE） | job 行锁 | 租约签发 `locked_at`（threads10）；`_lock_cutoff` 的 cutoff 与 `run_after<=now`（循环外取） | locked_at **已修（threads10）**：`_mark_running`/`_reclaim_running` 内取写入时刻；cutoff/run_after 维持边界：方向保守（晚一轮回收，不错收/不错放），注释论证 |
| queue.finalize_claim（PG job FOR UPDATE） | job 行锁 | `run_after = utcnow()+delay` | 正面样例：锁内取 |
| db.init_db（pg_advisory_xact_lock） | 咨询锁 | 无业务派生值（create_all） | 不适用 |

**边界（允许的例外，须注释论证）**：纯展示/时间戳用途的旧值（如 PATCH `.values(updated_at=utcnow())` 在语句构造时求值，陈旧 µs–ms 级、无决策依赖；device `last_seen` 无任何决策读它）；方向保守的判定（如 `_claim_postgres` 的 `cutoff = now - timeout` 在循环外取——陈旧只会让刚过期的租约晚一轮被回收，不会错回收/错放行）；无等待窗口的首个写（login 的 invite 条件 UPDATE 前 `now` 即取即用）。全仓各锁点的逐条核查记录见第八/九轮 sweep（`/workspace/.agent-tmp/sweep8.md` D 类、`sweep9.md` A 类）。

**保证代码**：`app/api/auth.py::login`（`_resolve_lost_invite` 重读后重取 now；`_token_pair` 移到 device upsert 之后，threads9 P1）、`app/api/auth.py::_claim_invite_first_use`（rowcount==1 后锁内重取 now 复验过期，threads10 P1）、`app/api/auth.py::refresh`（锁内重取 now；`_revoke_device_for_reuse` 的锁内 `utcnow()` 判过期为 threads7 已有正面样例）、`app/pipeline/extract.py::_apply_extraction`（版本栅栏 + 锁内 now）、`app/jobs/queue.py::finalize_claim`（锁内读值判定）、`app/jobs/queue.py::_mark_running/_reclaim_running`（租约 locked_at 取写入时刻，threads10）、`app/pipeline/memory_sync.py::_mark_memory_status`（置位事务内原子租约复核，INV-5/6）。

**验证测试**：
- `test_refresh_token_expires_while_waiting_device_lock_401`（threads8 P1：可控时钟 + 注入等锁窗口，token 在窗口内跨过 expires_at → 401 unauthorized、行未轮换、设备未吊销）
- `test_invite_expires_while_loser_waiting_claim_403`（threads9 P1：可控时钟推进 + 赢家在独立会话提交抢占（跨会话提交对 rollback 后的输方可见，与真实等锁等价），邀请码在等待窗口内跨过 expires_at → 403 invalid_invite、无凭证签发）
- `test_login_mints_tokens_after_device_row_lock`（threads9 P1：钩子记录 device upsert 语句执行与 `_token_pair` 签发的先后，断言 upsert→mint；解码 access token 断言 exp−iat==TTL 且 refresh 可用）
- `test_invite_claim_revalidates_expiry_after_claim_success_403`（threads10 P1：可控时钟推过 expires_at 后用旧 now 执行真实抢占——rowcount=1 正是「谓词用等待前参数重评」的窗口 → 403 invalid_invite、claim 回滚（owner/used 复位）、无设备行无凭证）
- `test_claim_lease_issued_at_update_moment_not_loop_start`（threads10：置 running 前时钟前进，locked_at=前进后时刻而非 claim 循环起点——租约不被 FOR UPDATE 等锁时间缩短）
- 既有正面样例回归：`test_refresh_reuse_revoked_but_expired_401_no_revoke`（锁内 `utcnow()` 判过期的既有行为不变）

### INV-14 日志/数据库/错误响应不存异常原文与用户内容（threads8 P1，runner.py:71）

**陈述**：**异常的消息文本与用户内容不得进入任何持久层或跨用户可见面**——应用自身的日志（`logger.*`、traceback、`exc_info`）、数据库错误字段（`jobs.last_error` 等）、500 错误响应体，以及**框架层替应用打的日志与构建产物**（threads9 扩写，见下）。异常只以**类型名 + 粗粒度错误码**记录（`app/errors.py::safe_error_summary` / `safe_error_text`：`{"type": "StatementError", "code": "db_statement"}`）。

**为什么不记「脱敏截断摘要」（有意决定，不是疏漏）**：评审原始诉求是 SQLAlchemy `StatementError` 的 `str(exc)` 默认内嵌 SQL 绑定参数——参数就是笔记正文/标题/待办/提醒文本这类**自然语言用户内容**；数字串/邮箱/URL/token 式的脱敏挡不住自由文本，任何长度的「截断摘要」都会把用户内容带进日志与 `jobs.last_error`。类型名 + 错误码足以定位失败类别（`timeout`/`db_statement`/`db_integrity`/`json_parse`/`internal`…），具体细节由可复现的测试或本地复跑获取。同理 `worker` 的常驻循环不用 `logger.exception`/`exc_info=True`（traceback 会带上异常消息原文）。

**框架层（threads9 扩写）**——这些口子应用代码看不见，各自专门收口：

1. **uvicorn access log（P1 docker-compose.yml:44；threads10 P1 README.md:52）**：默认开启，请求行含完整 query——`GET /v1/search?q=<笔记正文>` 整行进 stdout（compose 日志收集面）。收口：compose 与 Dockerfile 的 uvicorn 命令一律 `--no-access-log`（`test_uvicorn_commands_disable_access_log` 机械锁定）；**文档里的启动示例同口径**——README 本地开发命令也必须带 `--no-access-log`（`test_readme_documented_uvicorn_command_disables_access_log` 锁定 fenced block），照文档起步的开发环境同样是终端/开发日志收集面；进程内保留请求级排障入口的是自有 `AccessLogMiddleware`（`app/middleware.py`）——只记 **method、路由模板（`scope["route"].path`，如 `/v1/notes/{note_id}`）、status、耗时**，不记 query、不记路径参数原值（未匹配路由记 `(unmatched)`）。
2. **ServerErrorMiddleware 必 re-raise（P1 main.py:56）**：`add_exception_handler(Exception, …)` 只消毒**响应体**；Starlette 的 `ServerErrorMiddleware` 调完 handler 后总是 re-raise，原始异常与 traceback 进 uvicorn.error 日志（SQLAlchemy StatementError 的 str 内嵌 SQL 绑定参数）。收口：最外层用户中间件 `SafeErrorFallbackMiddleware`（纯 ASGI）捕获一切 `Exception` → 记一行 `safe_error_text` → 响应未开始则回固定 500 JSON、**永不 re-raise**（uvicorn 收不到异常，traceback 无从落日志）；响应已开始（流式半途，本仓无流式端点）同样不 re-raise——re-raise 只会换回「uvicorn 打整段 traceback」这条被禁路径。**有意不给 `uvicorn.error` 配 filter**：兜底中间件永不 re-raise 后该分支不可达，无异常可滤；全局 filter 会把基础设施异常（连接错误等，不含用户内容）的 traceback 也抹掉，缩小诊断面。
3. **SQLAlchemy echo 的 URL query 向量**：全仓未传 `echo`，但 `create_async_engine` 会把 URL query 当引擎参数——`DATABASE_URL=…?echo=true` 静默开 echo，SQL+绑定参数整体进 sqlalchemy.engine 日志。收口：`app/config.py::_reject_sqlalchemy_echo`（任何环境）对 `make_url(database_url).query` 里真值的 `echo` 启动即 ValueError；排障需要 SQL 时本地临时改代码，不开配置口。
4. **第三方 HTTP 客户端 logger**：httpx INFO 级打每个请求行、未来版本可能打请求细节（LLM 请求体含笔记正文）。收口：`app/logs.py::harden_third_party_loggers` 把 `httpx`、`httpcore`、`openai` 钉 >= WARNING；`create_app`（api）与 worker `main()`（basicConfig 之后）统一调用。**gunicorn**：本仓无（requirements 只有 `uvicorn[standard]`，compose/Dockerfile 直接 uvicorn）；将来引入时必须同查其 access/error logger 配置。
5. **构建上下文也是泄漏面（P1 docker-compose.yml:21）**：compose 三个服务 `build: .` 而仓库曾无 `server/.dockerignore`——按 README 生产部署创建的 `server/.env`（JWT/LLM/PG 密钥）与本地 SQLite 笔记库会**整体发给 builder**（legacy/remote builder 都拿得到全量上下文，Dockerfile 不 COPY 也拦不住传输）。收口：`server/.dockerignore` 排除 `.env`/`.env.*`、`*.db`/`*.db-*`、`*.sqlite`/`*.sqlite-*`、**`*.sqlite3`/`*.sqlite3-*`（threads11 P1：README 记载的本地默认库是 `app.sqlite3`（含测试库 `tests/_test.sqlite3`）与 journal/WAL sidecar，`*.sqlite`/`*.sqlite-*` 都不匹配 `.sqlite3` 后缀——漏掉它，本地开发照样把笔记库发给 builder）**、`*.log`、`.venv/`/`venv/`、`__pycache__/`/`*.py[cod]`、各缓存目录、`tests/`、`.git/`；`!.env.example` 保留模板进上下文（真实 `.env` 仍被 `.env`/`.env.*` 排除）；镜像只 COPY `requirements-app.txt` 与 `app/`（tests 不进镜像，测试锁定）。
6. **启动校验失败的 traceback 不含密钥明文（threads10 P1，config.py:68）**：`jwt_secret`/`llm_api_key` 是 plain str、`database_url` 内嵌 PG 密码——任一 `mode="after"` 校验器抛错时，pydantic ValidationError 的 `input_value` 截断 repr 会露出**尾部**字段的值（实测：仅设 `DATABASE_URL`+`JWT_SECRET` 时消息末尾出现 `'jwt_secret': '…<密钥尾部>'`）。`get_settings`/`get_db_settings` 跑在一切中间件之前（api 的模块级 `create_app()`、worker 模块级 basicConfig、cli），traceback 整段落 uvicorn/进程日志。收口：两个入口边界捕获 ValidationError → 转成只含 `loc/type/msg` 的 `ConfigError`（strip `input`/`ctx`；msg 来自本仓校验器的固定文案或 pydantic 内置模板，均不含输入值），`from None` 抑制隐式 `__context__`（否则原 ValidationError 仍随「During handling of…」链式 traceback 打出）。**有意不用 SecretStr**：只护 repr、不覆盖 `database_url` 的 password 部分、且要改所有取值点与既有测试；边界收口一次覆盖全部入口（评估见第 2 节表）。
7. **`.gitignore` 与 `.dockerignore` 的 SQLite sidecar 模式对齐（threads12 P1，.gitignore:8）**：`*.sqlite3` 不匹配 `app.sqlite3-wal/-shm/-journal`——sidecar 在崩溃后残留或事务进行中存在，含完整或部分笔记页数据；`.dockerignore` threads11 已收口，`.gitignore` 漏同款模式时一次普通的 `git add` 照样能把旁路文件提交进仓库（本地库进 git 与进构建上下文是同一个泄漏类的两个面）。两边统一模式集：`*.db`/`*.db-*`/`*.sqlite`/`*.sqlite-*`/`*.sqlite3`/`*.sqlite3-*`，测试双断言。

**边界（允许的回显，写明以防扩大化）**：

- **422 校验错误响应**：pydantic 错误的 `type/loc/msg` 回给**同一客户端**——这是客户端修正请求所需的标准行为；但两道边界缺一不可（threads10 P1，schemas.py:156）：`input`/`ctx`（整段请求原值：正文、refresh_token、asr）**不回显**（`validation_exception_handler` 剔除，FastAPI 默认处理器会整段带回），且 **msg 一律固定文案、不得插值请求提交的原值**（旧 timezone 文案 `…时区名：{value}` 把用户输入带回 422 体——剥参数不等于脱敏消息，msg 本身就是回显面）。
- **启动校验消息可引用非密钥配置值**：非法 `DEFAULT_TIMEZONE` 等校验文案把配置值原样带回**操作者自己的控制台**（不是跨用户回显面、不是密钥），保留有助排障；密钥类值（JWT_SECRET/LLM_API_KEY/数据库密码）一律不进消息（框架层第 6 条）。
- **管理员 CLI**：`python -m app.cli create-invite` 把新生成的邀请码打到 stdout——stdout 即交付渠道，不进日志/数据库/错误响应。
- **500 响应体**：固定文案（`internal server error`），绝不回显异常文本。

**保证代码**：`app/errors.py::safe_error_summary/safe_error_text`（唯一摘要出口）、`app/pipeline/runner.py`（日志与 `last_error` 只走摘要）、`app/jobs/worker.py`（常驻循环 `logger.error` + 摘要，无 traceback）、`app/errors.py::unhandled_exception_handler`（500 固定文案）、`app/errors.py::validation_exception_handler`（剔除 input/ctx）、`app/schemas.py::_check_timezone`（422 msg 固定文案，threads10 P1）、`app/middleware.py::SafeErrorFallbackMiddleware`（最外层兜底、永不 re-raise）与 `AccessLogMiddleware`（模板级访问日志）、`app/logs.py::harden_third_party_loggers`（create_app 与 worker main 调用）、`app/config.py::_reject_sqlalchemy_echo` 与 `app/config.py::ConfigError/get_settings/get_db_settings`（启动校验脱敏边界，threads10 P1）、`server/docker-compose.yml`、`server/Dockerfile` 与 `server/README.md`（uvicorn 命令 `--no-access-log`，含文档示例，threads10 P1）、`server/.dockerignore` 与 `server/.gitignore`（sqlite/db 及 sidecar 模式对齐，threads12 P1）、`app/main.py::create_app`（中间件挂载序：兜底兜在 NulReject/CORS 之外）、机械检查 `scripts/check-invariants.py::INV-NO-RAW-EXC`。

**验证测试**：
- `test_safe_error_summary_type_and_code_only`（消息含伪造密钥/笔记正文时 summary 只有 type/code）
- `test_job_exception_not_logged_nor_persisted_raw`（threads8 P1：任务异常消息含笔记正文与伪造密钥 → caplog 与 `jobs.last_error` 均不含原文、只含类型/错误码；job 照常重试/判 failed）
- `test_worker_loop_exception_logged_sanitized_no_traceback`（常驻循环异常 → 日志无原文、无 exc_info、循环不退出）
- `test_500_response_body_contains_no_exception_text`（handler 抛含用户内容的异常 → 500 体为固定文案）
- `test_validation_error_response_does_not_echo_input`（422 体不含请求正文原值）
- `test_500_no_sentinel_in_any_logger_record`（threads9 P1：ASGITransport `raise_app_exceptions=False` 触发含哨兵的 500，caplog **所有** logger 的记录 message/args/exc_text 均不含哨兵，兜底日志只含类型名+错误码）
- `test_500_real_uvicorn_no_raw_traceback_in_logs`（threads9 P1：真实 `uvicorn.Server` 起本地端口 + 真 HTTP 请求触发 500，uvicorn/uvicorn.error/root 全部捕获记录无哨兵、无 traceback）
- `test_uvicorn_commands_disable_access_log`（threads9 P1：解析 compose command 与 Dockerfile CMD，每条 uvicorn 命令必须含 `--no-access-log`）
- `test_access_log_records_template_only_no_query_or_path_values`（threads9：含哨兵的 `/v1/search?q=`、带路径参数值的 GET、未匹配路由 → 访问日志只出现路由模板，不出现 query 值与路径原值）
- `test_dockerignore_excludes_secrets_and_local_data`（threads9 P1：.dockerignore 存在且含密钥/本地库/venv/缓存/tests 模式；threads11 补 `*.sqlite3`/`*.sqlite3-*` 断言；Dockerfile COPY 只带 requirements-app.txt 与 app/）
- `test_gitignore_excludes_sqlite_and_sidecar_files`（threads12 P1：.gitignore 含与 .dockerignore 对齐的全部 sqlite/db 模式（含 `*.sqlite3-*`/`*.db-*` 等 sidecar）——本地库不进 git 与不进构建上下文同口径）
- `test_database_url_echo_query_rejected`（threads9：`?echo=true` 启动即拒，任何环境；`?echo=false`/无参数放行）
- `test_third_party_http_loggers_pinned_to_warning`（threads9：先降级 DEBUG，`create_app()` 后 httpx/httpcore/openai 均 >= WARNING）
- 既有 `test_first_llm_failure_leaves_no_side_effects` 的 last_error 断言随本条改为「含类型名且不含原文」

### INV-15 改源字段必须让派生字段失效（threads8 P1，notes.py:215）

**陈述**：任何写**源字段**的路径必须同时让由它派生的字段失效（清空/作废）或重排队重算，绝不允许「源已变、派生还是旧值」的中间态被 API 观察到。源 → 派生映射：

| 源字段 | 派生字段 | 失效方式 |
| --- | --- | --- |
| `note.text` | `title`、`tags`、`todos`、`status`（organized/failed）、suggested reminders、记忆（经 memory_sync） | PATCH：同事务清空 title/tags/todos + `status='uploaded'` + 墓碑全部 suggested 提醒 + 入队 extract 与 memory_sync（新值由重抽取写回；extract 永久失败时旧派生也不残留） |
| `note.recorded_at` / `note.timezone` | suggested reminders 的 `fire_at`（相对时间参照与时区） | 当前 **PATCH 不支持改这两个字段**（`NotePatch` 只有 text+rev）——无可失效点；未来放开时必须与 text 同路径（清派生 + 重入队），本表即验收清单 |
| `note.deleted_at`（DELETE） | 全部派生 | 软删后 GET/list/sync 排除该行（sync 只回墓碑 id），提醒全量墓碑，memory_sync 清记忆——行内残留的 title/tags/todos 不可观察 |

各写路径的处理：

- `POST /v1/notes`：新行，title/tags/todos 天然 NULL、status=uploaded、入队 extract(rev=1)——无旧派生。
- `POST /v1/notes` 幂等命中：只读返回，无写。
- `PATCH` 完整路径：见上表第一行（**本轮修复**：此前只置 `status='uploaded'` 不清 title/tags/todos——PATCH 响应与 /v1/sync 把旧 rev 的抽取产物当当前元数据返回，新 extract 永久失败时旧产物永久残留）。
- `PATCH` 内容不变快路径：**不失效**（无源字段变更；无操作 PATCH 不得抹掉 organized 元数据）。
- `DELETE`：见上表第三行。
- `/v1/sync`：只读；**无 sync push / 批量写源字段的接口**（若有未来新增，逐路径对照本表）。
- extract apply：写全新派生（先软删旧 suggested 再写新产物）。
- 队列失败标记（`_mark_extract_failed`）：置 failed 不清派生——PATCH 已清空，失败落在一组空派生上，语义自洽。

**保证代码**：`app/api/notes.py::patch_note`（条件 UPDATE 一并 `title=None, tags=None, todos=None`）、`app/schemas.py::NotePatch`（源字段白名单：text+rev）、`app/pipeline/extract.py::_apply_extraction`（重算写回）。

**验证测试**：
- `test_patch_clears_stale_extraction_fields`（threads8 P1：organized 后 PATCH 正文 → 响应与库内 title/tags/todos 清空、status=uploaded、suggested 提醒在 /v1/sync 出现为墓碑、extract 与 memory_sync 均入队）
- `test_patch_unchanged_text_keeps_extraction_fields`（快路径不清派生；既有 `test_patch_unchanged_text_with_stale_rev_409` 锁定不入队）

### INV-16 密钥拼进 URL 前必须 percent-encode（threads12 P1，docker-compose.yml:24）

**陈述**：凡把密码/密钥拼进连接 URL（DATABASE_URL、REDIS_URL、DSN、connection string——含 compose 插值、脚本拼接、文档启动示例），userinfo 部分（用户名/密码）必须先 `urllib.parse.quote(x, safe="")`（或等价）编码。密码含 URI 分隔符（`@ : / # % ?` 空格等）时原样拼接会让 URL 解析器（SQLAlchemy `make_url` 及下游驱动）拆错组件：主机/库名被吃进密码、认证用错凭据——连接失败且报错不指向真实原因；compose 场景下 migrate 连不上库，api/worker（`depends_on: service_completed_successfully`）永不启动。注意**不用 `quote_plus`**：它把空格编成 `+`，而 `make_url` 只按百分号解码、不把 `+` 还原成空格——含空格的密码往返即错。

**本仓形态**：compose 的 migrate/api/worker **不再拼 DATABASE_URL**，改注入分项 `PGHOST/PGUSER/PGPASSWORD/PGPORT/PGDATABASE`（libpq 惯例名；postgres 服务仍收 `POSTGRES_PASSWORD` 原值、不进 URL）；应用侧 `app/config.py::postgres_url_from_env` 是唯一拼接点（`quote(safe="")` 编码用户名与密码）。`Settings`/`DbSettings` 的 `model_validator(mode="before")`：仅当 `database_url` 未提供且存在 PG* 变量（真实环境变量或构造 kwargs，大小写不敏感；kwargs 优先）时拼接——**显式 DATABASE_URL 永远优先**，本地 SQLite 开发与测试套件不受影响。密码不进任何日志/错误消息（INV-14）。

**为什么选「分项变量 + 应用拼接」而不是 compose entrypoint/脚本编码**：应用侧纯函数可用单元测试直连验证（密码含全部分隔符时 `make_url` 解析回原值），compose 只做变量透传、无 shell 拼接层；entrypoint 方案把编码逻辑放进不可单测的 YAML/shell 字符串里，三服务还要各复制一份。

**边界**：测试常量里的假凭据（`u:p@localhost/db` 这类无分隔符冲突的形态）不要求改造；新增任何「把密钥拼进 URL」的代码（脚本、文档示例、未来 REDIS_URL/DSN）一律走同一条规则——优先分项变量 + 应用侧拼接，必须拼时 `quote(safe="")`。有意不加机检规则：URL 拼接的 AST 启发式误报率高（分不清「已编码的值」与「原值」），达不到 check-invariants「精确优先于召回」的门槛；compose 形态由 `tests/test_compose.py` 机械锁定。

**保证代码**：`app/config.py::postgres_url_from_env`（唯一拼接点，`quote(safe="")`）、`app/config.py::Settings/DbSettings` 的 `_compose_pg_url_from_env`（before-validator，kwargs/环境大小写不敏感，DATABASE_URL 优先）、`server/docker-compose.yml`（migrate/api/worker 分项 PG* 注入）、`server/.env.example` 与 `server/README.md`（文档面说明）。

**验证测试**：
- `test_postgres_url_from_env_encodes_password_special_chars`（单元：密码含 `@ : / # %` 与空格时 `make_url` 解析回原值；用户名同理；无 PG* 变量返回 None）
- `test_settings_builds_pg_url_from_pg_env_vars`（PG* 进构造 kwargs / 环境变量 → `database_url` 为拼出的 PG URL；显式 DATABASE_URL 设置时 PG* 被忽略）
- `test_compose_pg_env_password_encoded_in_database_url`（PyYAML 解析 compose + 含分隔符的假密码走真实 `DbSettings`/`Settings` 构造，`make_url` 取回 userinfo 正确、host/port/db 符合 compose 拓扑；全程不打印密码）

### INV-17 应用工厂完整性：一切注册都在 create_app 内（threads13 P1，main.py:106）

**陈述**：所有路由（含 `/healthz` 这类探针）、中间件、exception handler、lifespan 钩子必须注册在 `create_app()` 工厂**内部**；模块级只保留 `app = create_app()` 这一行。挂在模块级 singleton 上的路由（`@app.get(...)` 写在模块顶层、位于 `app = create_app()` **之后**）只存在于那一个实例：`create_app()` 返回时装饰器尚未执行，`uvicorn app.main:create_app --factory`、测试或任何二次构建用工厂另建的 app 对该路由是 404——而其余路由齐全。探针缺失的失败形态尤其隐蔽：负载均衡器摘除一切「健康」实例，或滚动发布时新实例被误判死亡。

**保证代码**：`app/main.py::create_app`（/healthz 探针、全部 APIRouter、中间件、exception handler、lifespan 的唯一注册点；模块级仅 `app = create_app()`）。

**验证测试**：
- `test_healthz_registered_on_factory_created_app`（threads13：工厂另建 app GET /healthz → 200；此前 404）
- `test_factory_and_module_apps_expose_identical_routes`（类扫：模块级 app 与 `create_app()` 另建 app 的 (path, methods) 路由面完全一致，且模块级 app 含 /healthz——未来任何「挂在模块级 singleton」上的路由在这里掉出来）

---

## 2. 为什么选这个模型 / 替代方案

| 决策 | 选择 | 替代方案与不选的理由 |
| --- | --- | --- |
| job 拆分 | extract 与 memory_sync 两种，各自 attempts/失败语义 | 单 job 兼职（现状）：attempts 共享导致「LLM 成功、记忆失败」耗尽重试时要么重调 LLM 要么走 memory_only 特判；失败标记只能绑定入队 rev，apply 后 rev 已 +1 永不命中（C5）；补写路径与正常路径分叉多、难测 |
| memory 三态 | `memory_status: pending/synced/failed` 替换 bool `memory_synced` | bool 无法表达「同步失败」这一可观测、可恢复状态（失败后只能永远 False，与「尚未同步」不可区分，无法排障也无法写断言） |
| 记忆写删入口 | 仅 memory_sync job（+账户级 delete_all）；API 只入队 | API 提交后直删（现状）：best-effort 无重试（C4）；与进行中的 memory 写入交错产生「删除后复活/误删新 rev」（C9）。入口唯一化后，所有交错都落在 memory_sync 内部，用一套复查协议收敛 |
| 删除旧 rev 记忆的粒度 | 复查不一致时**按本次 add 返回的 memory id 删** | `delete_by_note` 一刀切（现状）：会误删并发新 rev 任务刚写入的记忆（C9）。按 id 删需要 add 返回 id——mem0 与 inmemory 都已返回 |
| 未 organized 时 memory_sync 的行为 | 只清旧记忆、不写新记忆 | 字面「完全不写」：PATCH 后 extract 永久失败时旧记忆永远留在 /search 里（C4 未修净）。清旧不写新与收敛目标一致：未 organized 的 rev 不该有记忆 |
| 同 note 串行化 | running 唯一部分索引（提交时硬保证）+ claim 快路径 NOT EXISTS | session 级 advisory lock 跨外部调用：钉死池连接、池回收丢锁、SQLite 无对应（详见 INV-5 评估）；note 行 FOR UPDATE 只能串行化读；owner 令牌列更强但多余（正确性不依赖串行化），记为升级路径 |
| extract 是否 bump rev | 保留现状：apply 时 rev+1 | 不 bump（rev 只表用户内容版本）：语义更纯、客户端更少 409，但改变对外行为、牵动 android 与现有测试；MVP 先保持一致，列为后续可选项 |
| API 路径对 jobs 表 | 只 INSERT（DO NOTHING），不 UPDATE 既存行（放弃「PATCH 时替换/取消旧 pending extract」） | upsert DO UPDATE 替换旧 pending：会引入 note→既存 job 行的锁序边，与队列内部 jobs→note 构成 AB-BA；代价只是过期 pending extract 积累（执行时一次短读即跳过为 done），量级可忽略 |
| create 路径顺序 | note INSERT + job INSERT 都 flush 后才 allocate_seq | 现状 job flush 在 sequences 锁之后（C2）：拉长全局临界区、违反「seq 是最后一把锁」 |
| 建表 | 一次性 init-db（PG 咨询锁）+ compose migrate one-shot | 启动时各进程并发 create_all（现状）：check-then-create 非原子，多 worker 下 duplicate DDL 崩溃（C6）；Alembic 迁移框架 MVP 暂不引入（无线上数据），init-db 即 create_all 的串行化包装，后续可平滑换 Alembic |
| NUL 拒绝 | 中间件（path/query 解码后查）+ schemas（body 与参数注解）双层 | 只靠 schemas：path/query 参数不经 pydantic body 校验，`/v1/notes/n%00x` 仍直达 PG（C8）；只靠中间件：body 校验散落各 handler 易漏 |
| refresh 凭据存储（threads5 P1-2） | 独立 `refresh_tokens` 表一行一枚，登录只追加 + 每设备 5 枚修剪 | 沿用 devices 单列 hash（后写者胜）：并发登录每个 200 各自返回的 token 只有最后一枚能用，其余天生无效；「登录失败重试」方案则把并发首登的原子性又变回多语句竞态 |
| 记忆全量枚举（threads5 P1-1） | metadata 过滤 + 边取边删循环推进；取不满一页才算删净；取满且无法推进即抛错；InMemoryStore 同语义可注入上限 | 固定大 top_k（如 1000）当「全量」：超限用户的老笔记记忆删不净且 job 报成功；只按 user 全量列举再过滤：同样受页上限截断 |
| aware 时间边界（threads5 P2-4） | 先转 UTC 再套与 naive 相同的 `[0001-01-02, 9998-12-31]` 界 | 只校验「能转成 UTC」：`9999-12-31T23:59:59Z` 合法入库，抽取/提醒解析在用户时区 `astimezone` 处 OverflowError，note 被标 failed 而不是 422 |
| 丢租约后的外部写（threads6 P1-1） | 复查丢租约 → 删己 id + 无条件补偿再入队（唯一索引去重） | 只删己 id 就返回：迟到的 delete_by_note 会删掉换主后写入的记忆且再无后继任务，note 永久无记忆而 memory_status=synced |
| PATCH 内容不变快路径（threads6 P1-2） | 快路径也先做自赋值条件 UPDATE 原子校验 rev | 无锁读后直接 200：过期 rev + 恰好等于旧内容的请求绕过乐观锁，客户端把过期视图当当前态 |
| 条件写 0 行的归因（threads6 P1-3） | 重读当前状态（或因果封闭论证豁免，注释载明）再分类 | 按猜测归因：refresh 轮换 0 行被当成重放 → 等锁期间被挤出/登出的 token 错吊销整台设备 |
| refresh 行清理（threads6 P2） | 登录/刷新时顺手删 `expires_at<=now` 的行；已吊销未过期的保留到过期 | 不清理：常刷新设备的已吊销/过期行无限堆积；只清活行则上限约束不住表体积 |
| 生产记忆后端（threads7 拆分） | mem0 适配层整体移至 feat/mem0-backend 分支（后续 PR）；本仓 `MEMORY_BACKEND ∈ {inmemory, disabled}`，MemoryStore 接口与 INV-3 后端契约保留 | 留在本仓：拆分目的 PR 会随 mem0 实现细节（版本钉死、驱动、embedding 凭据）继续膨胀，评审面无法收敛；接口与契约留在本仓保证 feat/mem0-backend 按同一契约验收 |
| disabled 空后端（threads7） | 显式配置值：全接口空实现、memory_sync 平凡收敛标 synced、任何环境允许 | null-but-pretend 假实现：无法区分「关闭」与「在跑」；测试标志放行 inmemory：生产语义绑在测试开关上且 inmemory 是真丢数据；拒绝启动：拆分后分进程部署无任何可用配置 |
| 锁内重读绕过 identity map（threads7 P1，auth.py:280） | 条件 UPDATE（synchronize_session=False）后的重读只查标量列或 `populate_existing=True` | 直接 `select(Model)`/`session.get`：命中 identity map 首读的旧对象，并发轮换的 `revoked_at` 读成 None，重放误判过期 401、设备保持活跃 |
| 重放分支的锁内复核（threads7 P1，auth.py:246） | 所有通往吊销的分支先 `_lock_device`，锁内重读按「不存在→401、已过期→401、已作废→重放吊销」判定 | 拿无锁快照直接吊销：行已被登出删除/已过期未修剪时，普通 401 语义的请求吊销整台设备并作废全部现行 token |
| 补偿入队的可靠性（threads7 P1，memory_sync.py:123） | 删己 id 与补偿入队构成 try/finally：清理抛异常也一定入队，异常照常上抛走重试 | 顺序执行：store.delete 抛异常直接退出，补偿被吞掉且本 job 租约已丢无法重试，损害无人对账 |
| init-db 校验分层（threads7 P1，docker-compose.yml:24） | init-db 只加载 DbSettings（数据库层）；migrate/api/worker 按 command 各自过启动校验，compose 有测试机械验证 | init-db 加载完整 Settings：migrate 沿用 .env 本地默认 inmemory + PG 覆盖，撞上生产拓扑校验退出，全新部署停在建表一步 |
| 锁内派生值（threads8 P1，auth.py:294） | `_lock_device` 之后重取 `now`，轮换条件 UPDATE 对行内**当前** `expires_at` 原子判定 | 沿用请求起始的 now：token 等锁期间跨过期仍被轮换成新凭证；「对比数据库时钟」不可移植（SQLite 无语句级时钟语义）且多余——条件 UPDATE 本身就是锁内判定 |
| 置位事务的租约复核（threads8 P2，memory_sync.py:131） | memory_status 条件 UPDATE 与租约复核同事务：事务内先 FOR UPDATE 锁 job 行、锁内复核租约（SQLite SQL datetime 等值不可靠 → 取行后 Python `as_utc` 比较），复核不过不置位 | note UPDATE 的 WHERE 里塞 EXISTS 子查询：双方言差异 + SQLite datetime 比较不可靠；不复核：丢租约的迟到 worker 照写 `synced`，掩盖后继任务未完成的真实状态 |
| 异常摘要形态（threads8 P1，runner.py:71） | 只记类型名 + 粗粒度错误码（`safe_error_summary`），**不记消息文本** | 脱敏截断摘要：StatementError 的 str 内嵌 SQL 绑定参数（笔记正文这类自由文本），regex 脱敏挡不住；引擎 `hide_parameters` 只治 DB 一类，治不了 LLM/网络异常消息里同样可能出现的用户内容，且改变全局诊断面 |
| PATCH 改正文的派生失效（threads8 P1，notes.py:215） | 条件 UPDATE 一并 `title=NULL, tags=NULL, todos=NULL` + `status='uploaded'` + 作废 suggested + 双入队 | 只置 status（现状）：PATCH 响应与 /v1/sync 把旧 rev 的抽取产物当当前元数据返回；新抽取重试耗尽时旧产物与 `failed` 永久并存 |
| LLM 字符串硬化（threads8 P2，extract.py:120） | `_clean_llm_str` 剥 NUL、C0（留 `\t\n`）、DEL、孤立代理对；剥后为空的条目整条丢弃 | 按 422 拒绝整次抽取：LLM 输出不是用户请求，无通道回显错误，剥字符即可救回；不处理：PG text/JSON 编码失败 → 确定性重试耗尽 → 有效笔记被标 failed |
| 数值配置为正（threads8 P2，config.py:99） | `_require_positive_numeric_settings`：启动时列出**全部**非正数值字段并报错 | 逐字段零散校验：报错只暴露第一个；运行时才暴露更糟——`WORKER_BATCH_SIZE=0` 时 worker 日志一切正常但循环体永不执行 |
| MockLLM 相对时间上界（threads8 P2，mock.py:78） | `_safe_relative`：timedelta 构造溢出或结果越出 `utc_safe_datetime` 界 → 该提醒不出（返回 None） | 直接构造 timedelta：`999999999999999999分钟后` OverflowError → 确定性失败 → 有效笔记被标 failed；上界与请求侧/抽取侧共用同一安全界 |
| MockLLM 十进制位数上界（threads9 P2，mock.py:49） | `_cn_to_int` 限 ≤6 位 + 捕获 ValueError/OverflowError（超长数量无提醒） | 正则限位 `[0-9]{1,6}`：超长数字串的末 6 位仍匹配「分钟后」，5000 位数被错当 999999 分钟；不处理：CPython int() 约 4300 位上限 → ValueError 上抛 → 确定性重试耗尽 → 有效笔记被标 failed |
| login 锁前签发（threads9 P1，auth.py:248） | `_token_pair` 移到 device upsert 之后，锁后重取 now 供签发与追加（与 refresh 同序） | 保持锁前签发：短 TTL + 长等锁 → 返回的 access token 到手即过期；两条 auth 路径签发时序不一致本身就是缺陷信号 |
| 邀请码输方的过期判定（threads9 P1，auth.py:240） | 重读之后重取 now 再判 expires_at（INV-13） | 沿用等锁前 now：等待期间跨过 expires_at 的邀请被输方接受并登录赢家账号——同一时刻起的全新登录反而 403 |
| 未处理异常的框架层去向（threads9 P1，main.py:56） | 最外层纯 ASGI 兜底中间件：记 `safe_error_text`、回固定 500、**永不 re-raise**（uvicorn 收不到异常） | 只依赖 `Exception` handler：ServerErrorMiddleware 调完 handler 总是 re-raise，原始 traceback 进 uvicorn.error；给 uvicorn.error 配 filter：该分支已不可达，且会连基础设施异常的诊断 traceback 一起抹掉 |
| access log 形态（threads9 P1，docker-compose.yml:44） | uvicorn `--no-access-log`（compose+Dockerfile）+ 自有 `AccessLogMiddleware` 只记 method/路由模板/status/耗时 | 保留 uvicorn access log 并 redact：请求行格式不受应用控制（query 之外还有路径参数原值）；完全不要访问日志：进程内再无请求级排障入口（哪个接口慢/多少 5xx） |
| 第三方 logger 级别（threads9） | `app/logs.py::harden_third_party_loggers` 钉 httpx/httpcore/openai >= WARNING，create_app 与 worker main 统一调用 | 不设置：级别随部署环境默认漂移，库升级把请求细节（LLM 请求体含笔记正文）打进日志时无防线 |
| 构建上下文（threads9 P1，docker-compose.yml:21） | `server/.dockerignore` 排除 .env/本地库/venv/缓存/tests；镜像只 COPY requirements-app.txt 与 app/ | 只依赖 Dockerfile COPY 白名单：上下文在 COPY 之前整体（含 remote builder）传输，.env 与本地 SQLite 笔记库照样离机 |
| 邀请码 claim 成功后复验（threads10 P1，auth.py:205） | 抢占成功（rowcount=1、仍持行锁）后标量列重读 expires_at + 重取 now 复验；过期 → rollback 本次 claim + 403 | 只信条件 UPDATE 的 rowcount：PG READ COMMITTED 下锁等待后用**等待前绑定**的 now 参数重评谓词，等待期间跨过 expires_at 的邀请码照样抢占成功，绕过输方归因直接创建账号；「对比数据库时钟」不可移植（SQLite 无语句级时钟语义） |
| 租约签发时刻（threads10，queue.py:236） | `locked_at` 在置 running 的语句内取写入时刻的 utcnow（`_mark_running`/`_reclaim_running` 函数内重取） | 沿用 claim 循环开头的 now：FOR UPDATE 等锁消耗的时间等量缩短租约，等待越久活 worker 越早被误判失灵、越早被换主；回收 cutoff 维持参数（谓词边界，方向保守） |
| 422 msg 固定文案（threads10 P1，schemas.py:156） | 校验器 ValueError 全部固定文案（`_check_timezone` 不再插值 value）；`validation_exception_handler` 剥 input/ctx 后返回 msg，msg 本身必须无原值 | 只剥 input/ctx：msg 里插值的用户输入（非法 timezone 名）原样回显，剥 input 白做；回显有排障价值不成立——合法值客户端本地就有，非法值回显只帮攻击者探测校验边界 |
| 启动校验脱敏形态（threads10 P1，config.py:68） | `get_settings`/`get_db_settings` 边界捕获 ValidationError → 转只含 loc/msg/type 的 `ConfigError`，`from None` 抑制隐式 `__context__` 链 | SecretStr：只护 repr，不覆盖 `database_url` 的 password 部分，且要改所有取值点与既有测试；异常消息白名单脱敏：regex 挡不住 pydantic `input_value` 截断 repr 里的尾部字段值（实测消息末尾原样出现尾部字段的密钥明文）。边界收口一次覆盖全部入口（api import / worker / cli） |
| 文档启动命令（threads10 P1，README.md:52） | README 的本地 uvicorn 示例同口径 `--no-access-log`，`test_readme_documented_uvicorn_command_disables_access_log` 解析锁定 | 只收口 compose/Dockerfile：照 README 起步的本地开发照样把 `GET /v1/search?q=<笔记正文>` 整行打进终端；文档示例与部署清单不一致本身就是漂移源 |
| 密钥进 URL（threads12 P1，docker-compose.yml:24） | compose 注入分项 PG* 环境变量，应用侧 `postgres_url_from_env` quote(safe="") 后拼 URL（唯一拼接点，可单测） | compose 里 `${POSTGRES_PASSWORD}` 原样插值进 DATABASE_URL：密码含 `@:/#` 时 SQLAlchemy 解析错位，migrate 连不上、部署停在建表一步；entrypoint/脚本编码：编码逻辑进不可单测的 YAML/shell，三服务各复制一份 |
| 复活设备的凭证 fence（threads12 P1，auth.py:82） | `credential_generation` 代际：吊销与真复活（revoked_at 非空时的 upsert）各 +1，access `gen` claim 与 refresh 行绑定签发时的代，`get_auth`/refresh 校验不一致一律普通 401（不吊销） | 复活只清 `revoked_at`：吊销前签发的 access 随复活重新放行；被吊销旧 refresh 的每次重放都把新登录的会话再吊销一遍（连环吊销到旧行过期） |
| LLM 出站清洗公共化（threads12 P2，ask.py:70） | `app/schemas.py::clean_llm_str` 公共函数：extract 抽取字段与 /v1/ask 的 answer 同一条规则（剥 NUL/C0/DEL/lone surrogate） | 各路径私有实现：ask 的 answer 无人清洗——供应商返回转义 lone surrogate 时 Starlette JSON 编码 UnicodeEncodeError → 500 |

## 3. 数据模型与迁移

- `jobs` 新列：`note_id: str|None`（加索引）、`note_rev: int|None`（extract 专用）；`kind` 值域变为 `{extract, memory_sync}`。
- `jobs` 新索引（`Index(..., unique=True, postgresql_where=..., sqlite_where=...)` 双方言）：
  - `uq_jobs_memsync_pending ON (note_id) WHERE kind='memory_sync' AND status='pending'`
  - `uq_jobs_memsync_running ON (note_id) WHERE kind='memory_sync' AND status='running'`
  - 保留 `ix_jobs_status_run_after`。
- `notes`：删除 `memory_synced`，新增 `memory_status: str(16) default 'pending'`（不进 API 响应、不占 change_seq，其更新也不触碰 `updated_at`/`change_seq`，见 INV-2.6）。
- `devices`：**删除 `refresh_token_hash` / `prev_refresh_token_hash`**（threads5 P1-2，凭据移出 device 行）；新增 `credential_generation: int NOT NULL DEFAULT 0`（threads12 P1 / INV-8 陈述 4：吊销/复活 +1 的凭证代际）。
- 新表 `refresh_tokens`（threads5 P1-2，INV-8）：`id: str(32) PK`、`device_id: str(64) FK devices.id`（索引）、`user_id: str(32) FK users.id`、`token_hash: str(128) UNIQUE`、`created_at`、`expires_at`、`revoked_at: nullable`、`credential_generation: int NOT NULL DEFAULT 0`（threads12：行创建后不可变，代际 fence 用）。init-db/create_all 经 `app.models` 注册自动建表。
- `MemoryStore.add(user_id, text, *, note_id, source_rev)`；`MemoryItem` 增 `source_rev`；新增 `MemoryEnumerationIncomplete`（INV-3 枚举完整性）。
- 迁移策略：MVP 未上线，无存量数据兼容义务——PG 用 init-db（create_all 只建缺失表；**已存在的旧 dev 库需丢弃卷重建**，或手动执行文档给出的 `ALTER TABLE notes DROP COLUMN memory_synced, ADD COLUMN memory_status ...`、`ALTER TABLE devices DROP COLUMN refresh_token_hash, DROP COLUMN prev_refresh_token_hash`、`CREATE TABLE refresh_tokens ...` 与 jobs 新列/索引 DDL）；SQLite dev 库删除文件重建。此为已知限制，写入自查清单。

## 4. 自查清单（推送前逐条核对）

1. 三套验证全绿：SQLite pytest、`TEST_DATABASE_URL=postgresql+asyncpg://...` pytest、根目录 `ruff check server`。
2. 每个新增/修改的写路径对照 INV-1 加锁序列表；`grep -rn "update(Job)" server/app/api server/app/pipeline` 为空（业务路径不 UPDATE 既存 job 行）。
3. 事务边界内无 LLM/记忆后端/网络调用：检查 extract/memory_sync/notes/auth 的每个 `session.begin()` 块。
4. 新增状态变更已同步更新本文档对应 INV 与测试；本文「保证代码」落点与实际文件/函数一致。
5. 并发测试在 PG 下用独立会话/连接真并发；SQLite 下无法复现的竞态 `skipif` 并注明原因（沿用 `requires_postgres` 标记）。
6. init-db 后核对三条部分唯一索引在 PG（`\d jobs`）与 SQLite（`sqlite_master`）都存在（或以测试断言）。
7. 旧 dev 库处理：PG 卷重建或手动 ALTER（见第 3 节）；`.env.example` 若涉及新配置项同步更新（`test_env_example_placeholder_matches_template`、`test_env_example_documents_memory_backend_choices`、`test_env_example_documents_new_runtime_flags` 系列）。
8. compose 校验：`migrate` one-shot 先于 api/worker（`service_completed_successfully`）；PG 下 api/worker 启动不再建表；各服务环境按其 command 通过对应启动校验（migrate → DbSettings；api → 完整 Settings；worker → 完整 Settings + worker 拓扑校验），`tests/test_compose.py` 机械验证（threads7 P1）。
9. 未来任何 header 值入库必须走 `NoNulStr`（INV-7 覆盖面表）。
10. 新增携带版本的写接口（或给既有接口加 rev）：对照 INV-11——快路径/幂等命中也必须先原子校验 rev，409 体用 `error_body("conflict", …)` 两段式。
11. 新增条件 UPDATE/DELETE 且消费 rowcount：对照 INV-12——0 行后重读归因，或注释里写因果封闭论证；禁止按单一猜测直接定性（重放/冲突/已删除）。**不回滚的重读必须绕过 identity map**（只查标量列或 `populate_existing=True`，synchronize_session=False 不更新已加载对象）；rollback 后重读天然安全（rollback 使 identity map 过期）。
12. 新增绕过数据库锁的外部写路径：对照 INV-5 总原则——外部写之后无条件再对账一次，丢租约必补偿再入队，不得静默返回；**补偿入队与它前面的清理外部写构成 try/finally**（清理抛异常也一定入队）。
13. AGENTS.md「写代码」含本文链接且路径有效。
14. 记忆后端改动：对照 INV-3 后端契约（枚举完整、不越权改动其他 note、add 不做 infer 类改写/合并）与本仓 `MEMORY_BACKEND ∈ {inmemory, disabled}` 枚举；生产后端（feat/mem0-backend）落地时契约不放宽。
15. 新增数值型配置（TTL / 超时 / 批量 / 重试 / 上限类）必须同时加进 `_require_positive_numeric_settings` 的字段清单（INV-10.7）与 `.env.example`，且「必须为正」的约束写进字段注释。
16. 新增/修改持锁写路径（FOR UPDATE / 条件 UPDATE / advisory lock）：对照 INV-13——等锁之前算好的派生值（now、状态、计数、rev、expires_at、活跃标记）不得在锁内沿用；锁内重取、由条件 UPDATE 原子判定或版本栅栏守护；条件更新/claim **成功之后**仍须用成功时刻的 now 复验过期（rowcount=1 不代表谓词里的旧时间参数仍然成立）；签发类时间戳（locked_at、iat/exp）取写入时刻而非循环/请求起点；方向保守的陈旧值必须注释论证（见 INV-13 边界）。
17. 新增任何异常出口（日志、DB 错误字段、HTTP 错误响应）：只走 `app/errors.py::safe_error_summary`（类型名 + 错误码），不 `str(exc)` / `repr(exc)` / f-string 插值 / `exc_info=True` / `logger.exception`；跑 `scripts/check-invariants.py` 确认 INV-NO-RAW-EXC 0 报错 0 误报（INV-14）。
18. 新增写源字段的接口（note text、recorded_at、timezone，含未来 sync push / 批量写）：对照 INV-15 源→派生映射表，同事务清派生 + 作废 suggested + 入队重算；改映射表本身时同步本表与验证测试。
19. 部署/日志面改动（INV-14 框架层）：uvicorn 命令一律 `--no-access-log`（compose、Dockerfile **与 README/docs 文档示例**同口径，`test_uvicorn_commands_disable_access_log` + `test_readme_documented_uvicorn_command_disables_access_log` 锁定）；自有访问日志只记 method/路由模板/status/耗时；新增第三方客户端库时把其 logger 钉进 `app/logs.py::harden_third_party_loggers`（create_app 与 worker main 双入口）；构建上下文新增敏感/本地文件类型时同步 `server/.dockerignore`；`DATABASE_URL` 不得经 URL query 开 echo（config 启动即拒）。
20. 新增等锁窗口的写路径（含把无锁路径改成条件 UPDATE/ON CONFLICT）：对照 INV-13 全仓锁点表补一行——锁后用于决策的每个派生值（now、计数、过期判定、iat/exp）标注来源（锁内重取/原子判定/版本栅栏/边界论证）。
21. 新增校验失败出口（INV-14）：schemas 校验器与 HTTP 错误的 msg 一律固定文案，不插值用户输入原值（`input`/`ctx` 之外 msg 本身也要干净）；启动校验异常只经 `app/config.py::ConfigError` 边界出口（只含字段位置与校验文案），不直接上抛 pydantic ValidationError——其 `input_value` repr 可含密钥明文，且发生在中间件挂载之前。
22. 新增把密钥/密码拼进 URL 的代码（compose 插值、脚本、文档启动示例、未来 REDIS_URL/DSN 类）：对照 INV-16——优先分项环境变量 + 应用侧 `postgres_url_from_env` 拼接；必须拼时 userinfo 先 `quote(x, safe="")`；密码不进日志/错误消息。
23. 改动设备吊销/复活/凭证签发路径：对照 INV-8 陈述 4——代只在吊销与真复活（revoked_at 非空的 upsert）时 +1；新签发的 access `gen` claim 与 refresh 行携带当前代；`get_auth`/refresh 的代校验不一致一律普通 401（不轮换、不吊销）。
24. 新增路由/中间件/exception handler/startup-shutdown 钩子（含 `/healthz` 这类探针）：一律注册进 `create_app()` 工厂内，模块级只留 `app = create_app()`（INV-17）——挂在模块级 singleton 上的路由对 `uvicorn --factory`/另建的 app 是 404；`test_factory_and_module_apps_expose_identical_routes` 机械比对两 app 的路由面。

## 5. 测试计划汇总（INV ↔ 计划测试名 ↔ 后端要求）

| INV | 测试（计划名，第二步实现） | 后端 |
| --- | --- | --- |
| INV-1 | test_create_note_sql_order_job_insert_before_seq_update；test_patch_sql_order_note_reminders_jobs_before_seq_update；test_extract_finalize_concurrent_patch_no_deadlock；test_business_paths_never_update_existing_job_rows；保留 test_patch_and_organize_writeback_overlap_no_deadlock、test_patch_delete_organize_three_way_overlap_no_deadlock | 前二双后端；并发项 PG 真并发 |
| INV-2 | test_extract_skips_when_rev_moved_marks_done；test_extract_attempts_exhausted_marks_note_failed_by_rev；test_extract_finalize_skips_note_patched_after_enqueue；test_memory_sync_single_pending_per_note；test_memory_sync_pending_allowed_while_running；test_memory_sync_single_running_per_note；test_extract_and_memory_sync_independent_attempts | 单 running 项 PG 真并发，SQLite 仅语义 |
| INV-3 | test_memory_metadata_contains_note_id_and_source_rev；test_no_stale_rev_memory_after_repatch；test_inmemory_list_page_cap_raises_when_full；test_memory_sync_fails_not_marks_synced_when_enumeration_incomplete | 双后端；枚举语义由 InMemoryStore 可注入上限承载，生产后端等价测试随 feat/mem0-backend |
| INV-4 | test_patch_and_delete_never_touch_memory_store_directly | 双后端 |
| INV-5 | test_memory_sync_writes_memory_with_source_rev_and_marks_synced；test_memory_sync_on_deleted_note_removes_memory；test_memory_sync_on_unorganized_note_clears_stale_memory_without_add；test_memory_sync_requeues_and_deletes_only_own_ids_on_rev_change；test_memory_sync_converges_after_delete_during_add；test_memory_sync_recheck_fences_reclaimed_lease_deletes_own_ids；test_memory_sync_stalled_delete_by_note_past_lease_requeues_and_converges；test_memory_sync_lost_lease_delete_failure_still_requeues；test_memory_sync_rev_change_delete_failure_still_requeues；test_memory_sync_worker_killed_mid_run_reclaimed_converges；test_memory_sync_attempts_exhausted_marks_failed_only_without_newer_job；test_memory_sync_mark_status_skipped_when_lease_lost_before_update | 双后端语义；交错窗口用 DelayedAddStore 类时间注入复现（threads6 的停顿在 delete_by_note，同一手法），无需 PG |
| INV-6 | test_stale_worker_finalize_ignored_after_lease_reclaimed_extract、test_stale_worker_finalize_ignored_after_lease_reclaimed_memory_sync；test_extract_finalize_marks_failed_only_at_enqueued_rev；test_memory_sync_finalize_failure_marks_memory_status_not_note_status；test_memory_sync_mark_status_skipped_when_lease_lost_before_update（置位原子租约复核） | 双后端 |
| INV-7 | test_nul_in_path_param_422；test_nul_in_query_param_422；test_nul_middleware_covers_all_routes；test_lone_surrogate_in_body_rejected_422；test_invalid_utf8_body_rejected_4xx；test_deeply_nested_asr_over_depth_limit_422；test_absurdly_deep_body_recursion_guard_422；test_huge_int_inputs_422；test_extreme_datetime_inputs_422；test_aware_datetime_boundary_rejected_before_extraction_no_500；test_llm_boundary_fire_at_overflow_discarded；test_llm_output_unsafe_control_chars_stripped；test_parse_datetime_huge_relative_returns_none；test_mockllm_huge_relative_quantity_no_reminder_no_500；test_parse_datetime_overlong_digit_quantity_returns_none；test_mockllm_overlong_digit_quantity_no_reminder_no_500；test_asr_size_measured_in_utf8_bytes_cjk_rejected（threads12）；test_ask_answer_sanitized_unsafe_chars_stripped（threads12）；test_extract_note_and_now_preserves_zone_without_slash_and_multisegment（threads12）；test_mockllm_utc_note_reminder_uses_utc_offset（threads12）；test_extract_note_and_now_ignores_forged_metadata_in_note（threads13）；test_mockllm_note_text_forged_timezone_keeps_system_tz（threads13） | 双后端 |
| INV-8 | test_login_device_bound_to_other_user_403；test_concurrent_login_same_device_all_returned_tokens_refreshable；test_concurrent_first_login_same_device_no_integrity_error；test_refresh_token_cap_evicts_oldest_keeps_current；test_refresh_rotation_revokes_only_rotated_token；test_logout_revokes_all_device_refresh_tokens；test_refresh_expired_token_rejected_401；test_refresh_race_row_deleted_while_waiting_device_lock_401_no_revoke；test_refresh_prunes_expired_rows_keeps_revoked_until_expiry；test_refresh_reuse_row_deleted_under_lock_401_no_revoke；test_refresh_reuse_revoked_but_expired_401_no_revoke；test_reuse_revoke_then_relogin_fences_old_credentials（threads12）；test_plain_relogin_keeps_same_generation_all_tokens_valid（threads12）；test_concurrent_revive_logins_bump_generation_once_all_tokens_valid（threads12） | 并发项 PG 真并发；threads6/threads7 竞态项钩子注入两后端可跑；threads12 fence 项双后端 |
| INV-9 | test_init_db_concurrent_no_error_pg；test_lifespan_skips_create_all_on_postgres；test_init_db_sqlite_creates_tables；test_compose_services_pass_startup_validation；test_compose_migrate_would_reject_full_settings_layering_is_real；test_init_db_does_not_load_full_settings | 并发项 PG；compose 项双后端（PyYAML 解析，无需 Docker） |
| INV-10 | test_openai_missing_api_key_rejected、test_openai_missing_base_url_rejected、test_openai_missing_model_rejected；test_openai_all_three_configured_ok；test_default_timezone_invalid_rejected；test_default_timezone_valid_ok；test_worker_refuses_inmemory_with_postgres；test_get_memory_store_unknown_backend_raises；test_memory_backend_only_accepts_known_values、test_production_inmemory_rejection_mentions_mem0_backend_pr、test_production_disabled_memory_backend_ok；test_disabled_store_empty_contract、test_factory_returns_disabled_store、test_disabled_memory_sync_marks_synced、test_disabled_search_and_ask_return_empty_not_500；test_non_positive_numeric_settings_rejected；test_openai_malformed_base_url_rejected、test_openai_absolute_http_base_url_ok、test_mock_provider_skips_base_url_shape_check（threads13） | 纯配置，无 DB（disabled 三条走 client 夹具但只断言空结果）；mem0 相关用例随生产后端移至 feat/mem0-backend |
| INV-11 | test_patch_unchanged_text_with_stale_rev_409 | 双后端 |
| INV-12 | test_refresh_race_row_deleted_while_waiting_device_lock_401_no_revoke；test_concurrent_sixth_login_eviction_vs_refresh_no_device_revoke_pg；正面样例回归 test_concurrent_patch_same_rev_exactly_one_success、test_login_device_bound_to_other_user_403 | 竞态项 PG 真并发；钩子注入项双后端 |
| INV-13 | test_refresh_token_expires_while_waiting_device_lock_401；test_invite_expires_while_loser_waiting_claim_403；test_invite_claim_revalidates_expiry_after_claim_success_403（threads10）；test_login_mints_tokens_after_device_row_lock；test_claim_lease_issued_at_update_moment_not_loop_start（threads10）；正面样例回归 test_refresh_reuse_revoked_but_expired_401_no_revoke | 可控时钟 + 钩子注入等锁窗口（`_lock_device`/抢占输方重读/claim 成功后复验/`_device_upsert`→`_token_pair` 顺序/租约签发时刻），双后端；PG 真并发版不做（定时窗口必抖动，钩子注入等价复现） |
| INV-14 | test_safe_error_summary_type_and_code_only；test_job_exception_not_logged_nor_persisted_raw；test_worker_loop_exception_logged_sanitized_no_traceback；test_validation_error_response_does_not_echo_input；test_422_fixed_message_does_not_echo_timezone_value（threads10）；test_500_response_body_contains_no_exception_text；test_500_no_sentinel_in_any_logger_record；test_500_real_uvicorn_no_raw_traceback_in_logs；test_get_settings_validation_failure_does_not_leak_secret_values（threads10）；test_uvicorn_commands_disable_access_log；test_readme_documented_uvicorn_command_disables_access_log（threads10）；test_access_log_records_template_only_no_query_or_path_values；test_dockerignore_excludes_secrets_and_local_data；test_gitignore_excludes_sqlite_and_sidecar_files（threads12）；test_database_url_echo_query_rejected；test_third_party_http_loggers_pinned_to_warning；既有 test_first_llm_failure_leaves_no_side_effects 的 last_error 断言改为「含类型名且不含原文」 | 双后端；500 体断言用 `raise_app_exceptions=False` 的 ASGITransport；真实 uvicorn 项按 uvicorn 默认日志配置起本地 server（127.0.0.1:0）；compose/Dockerfile/config 项纯解析无需 Docker |
| INV-15 | test_patch_clears_stale_extraction_fields；test_patch_unchanged_text_keeps_extraction_fields | 双后端 |
| INV-16 | test_postgres_url_from_env_encodes_password_special_chars；test_settings_builds_pg_url_from_pg_env_vars；test_compose_pg_env_password_encoded_in_database_url | 纯配置/解析，无 DB（compose 项 PyYAML 解析，无需 Docker） |
| INV-17 | test_healthz_registered_on_factory_created_app；test_factory_and_module_apps_expose_identical_routes | 双后端（探针 GET 走 _fresh_env 建好的库） |

既有测试的适配（第二步）：`organize_note`→`extract`、`cleanup_memory`→`memory_sync`、`memory_synced`→`memory_status` 断言改写；test_memory_failure_retries_backfill_only、test_cleanup_memory_job_skips_live_note、test_delete_during_memory_sync_does_not_resurrect 等旧测试按新模型语义迁移（收敛目标不变，断言落点改为 memory_sync）。
