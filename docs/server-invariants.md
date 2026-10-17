# 服务端不变量与新模型设计（server-invariants.md）

> 状态：设计稿（第一步，只写文档不改代码）。第二步按本文实施代码与测试。
> 范围：`server/`。本文回答两件事：**服务端必须永远成立的不变量**（INV-x，每条含精确陈述、保证代码、验证测试——测试名为第二步的计划名），以及**任务/记忆新模型**的设计与理由。
> 输入：threads4.txt 的 9 条 Codex 评审意见，下文以 C1–C9 指代：
> C1 auth 并发登录同设备竞态（P1）；C2 create 中 enqueue 晚于 allocate_seq（P1）；C3 openai 配置需三项齐备（P2）；C4 PATCH 后旧记忆无兜底清理（P2）；C5 整理后 rev 变化导致失败收尾永不命中（P1）；C6 create_all 并发建表（P2）；C7 DEFAULT_TIMEZONE 未校验（P2）；C8 path/query 参数 NUL 进 PG（P1）；C9 旧整理任务 delete_by_note 误删新 rev 记忆（P1）。

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

1. 同一写事务内，业务行锁按固定顺序获取：**invite → user → device → note → reminders → jobs**。
2. 所有业务行的 INSERT/UPDATE（**含 job 行的 INSERT**，修 C2）显式 flush 落定之后，`allocate_seq`（sequences 行锁）必须是提交前获取的**最后一把锁**；change_seq 由其后的第二条 UPDATE 写回（行锁已持有，不新获取）。不得依赖 ORM autoflush 隐式落序。
3. **API/业务路径对既存 job 行只有 INSERT（`ON CONFLICT DO NOTHING`），永远不 UPDATE 既存 job 行**；既存 job 行的 UPDATE 只允许出现在队列内部（`app/jobs/queue.py` 的 claim / finalize / 回收判死路径），这些路径对业务行的加锁序列固定为 **jobs → note（仅失败标记）→ sequences → change_seq 写回**，不碰 reminders/device。因此「业务路径」与「队列路径」的锁序单向不相交，全库无环。
4. 事务边界内不得调用 LLM / mem0 / 任何外部网络服务（既有规则，保留）。

**各写路径加锁序列**（第二步实现后逐一对应）：

| 路径 | 加锁序列（左先右后） | seq 分配 |
| --- | --- | --- |
| POST /v1/notes（create） | note INSERT（新行）→ jobs INSERT extract（新行）→ flush → **allocate_seq** → note change_seq 写回 → commit。幂等冲突分支（client_note_id 撞唯一约束）整体回滚后只读复查，无二次加锁 | 1 |
| PATCH /v1/notes/:id | note 条件 UPDATE（锁行，乐观锁）→ reminders 条件 UPDATE（作废 suggested）→ jobs INSERT extract + INSERT memory_sync（均新行，DO NOTHING）→ flush → **allocate_seq** → note+reminders change_seq 写回 → commit | 1+N |
| DELETE /v1/notes/:id | note 条件 UPDATE（软删）→ reminders 条件 UPDATE（全量墓碑）→ jobs INSERT memory_sync → flush → **allocate_seq** → 写回 → commit | 1+N |
| extract 短写事务（apply） | note FOR UPDATE（PG）→ reminders（stale 条件 UPDATE + fresh INSERT）→ jobs INSERT memory_sync → flush → **allocate_seq** → 写回 → commit | 1+S+F |
| memory_sync 各短事务 | 读事务：note 纯读无锁。外部调用：无任何会话/锁。置位事务：note 条件 UPDATE（memory_status，不分配 seq）。再入队：jobs INSERT。**该路径永不碰 sequences** | 0 |
| 队列 claim | jobs 行（FOR UPDATE SKIP LOCKED / 条件 UPDATE） | 0 |
| 队列回收判死 _fail_expired | jobs 行 → note 条件 UPDATE → **allocate_seq** → 写回（jobs→note→sequences 单向） | 1 |
| 队列 finalize 成功 | jobs 行（FOR UPDATE + 租约条件） | 0 |
| 队列 finalize 失败 | jobs 行（FOR UPDATE + 租约条件）→ note 条件 UPDATE → **allocate_seq** → 写回 | 1 |
| POST /v1/auth/login | invite（读 + 首用条件 UPDATE）→ user INSERT（新行）→ device 原子 upsert（单条 INSERT…ON CONFLICT，见 INV-8）。不碰 sequences | 0 |
| POST /v1/auth/refresh | device 行（PG FOR UPDATE 串行化轮换 + 条件 UPDATE） | 0 |
| POST /v1/auth/logout | device 行 | 0 |
| GET /v1/sync | 只读。PG 下 REPEATABLE READ 单快照，无行锁（既有设计，见 app/api/sync.py docstring，不变） | 0 |

**保证代码**：`app/common.py::allocate_seq`（docstring 已载规则，保留）、`app/api/notes.py`（create 重排：job INSERT 先 flush 再 allocate_seq，修 C2；PATCH/DELETE 移除提交后直删记忆）、`app/pipeline/extract.py`（原 organize 的 apply 事务）、`app/pipeline/memory_sync.py`（新）、`app/jobs/queue.py`（队列路径）、`app/api/auth.py`（INV-8）。全局规则同步写进 AGENTS.md 的既有条目。

**验证测试**（计划名）：
- `test_create_note_sql_order_job_insert_before_seq_update`（engine 事件监听 SQL 流，断言 jobs INSERT 先于 sequences UPDATE；SQLite+PG 双后端语义一致）
- `test_patch_sql_order_note_reminders_jobs_before_seq_update`（同上）
- 已有 PG 并发回归保留：`test_patch_and_organize_writeback_overlap_no_deadlock`、`test_patch_delete_organize_three_way_overlap_no_deadlock`
- 新增 `test_extract_finalize_concurrent_patch_no_deadlock`（PG，真并发：extract 耗尽_attempts 收尾 与 PATCH 同 note 并行，独立会话）
- `test_business_paths_never_update_existing_job_rows`（静态断言：grep/AST 检查 app/api 与 app/pipeline/extract.py 中对 Job 的写仅为 INSERT）

### INV-2 任务模型：extract 与 memory_sync 两种 job

**陈述**：

1. `jobs.kind ∈ {extract, memory_sync}`（`organize_note`/`cleanup_memory` 移除；MVP 未上线，dev 库随 init-db 重建）。
2. extract 入队时携带 `note_rev`（新列）；执行时读 note：`deleted_at` 非空 → done；`note.rev != note_rev` → **跳过 = done**（过期任务无副作用）；否则 LLM → 短写事务 apply（rev+1、status=organized、memory_status=pending，同事务 enqueue memory_sync）。
3. memory_sync 不带 rev；**每个 note 至多一个 pending 的 memory_sync**：部分唯一索引 `UNIQUE(note_id) WHERE kind='memory_sync' AND status='pending'`，入队用 `INSERT … ON CONFLICT DO NOTHING`；running 期间再入队允许新建一个 pending（pending 唯一性只约束 pending 之间）。extract **不**做 pending 去重（同 note 多个不同 rev 的 pending extract 合法，旧rev 执行时自跳过）。
4. **同一 note 同时至多一个 running 的 memory_sync**（串行化，见 INV-5）：部分唯一索引 `UNIQUE(note_id) WHERE kind='memory_sync' AND status='running'` 数据库层硬保证。
5. 两类 job 各自独立的 attempts/max_attempts/失败语义：extract 耗尽 → `note.status='failed'`（条件：`rev=入队rev AND deleted_at IS NULL AND status!='organized'`）；memory_sync 耗尽 → `note.memory_status='failed'`（条件：note 未删除且**无其他** pending/running 的 memory_sync，`NOT EXISTS(... AND id != :job_id)`）。互不污染（修 C5：memory 失败不再触碰 note.status，extract 失败不触碰 memory_status）。

**保证代码**：`app/models.py`（Job 增 `note_id`/`note_rev` 列与三条部分唯一索引，`postgresql_where`/`sqlite_where` 双方言）、`app/jobs/queue.py::enqueue`（带 note_id/note_rev 与 DO NOTHING）、`app/pipeline/extract.py`、`app/pipeline/memory_sync.py`、`app/pipeline/runner.py::execute_job`（分发两种 kind）。

**验证测试**：
- `test_extract_skips_when_rev_moved_marks_done`
- `test_extract_attempts_exhausted_marks_note_failed_by_rev`（改造自旧的 test_failure_marks_note_failed）
- `test_extract_finalize_skips_note_patched_after_enqueue`（改造自现有同名测试）
- `test_memory_sync_single_pending_per_note`（并发/重复入队只剩一个 pending，双后端）
- `test_memory_sync_pending_allowed_while_running`
- `test_memory_sync_single_running_per_note`（PG，两 worker 独立连接并发 claim，断言任一时刻只有一个 running；SQLite 单写者天然满足，仅验证语义）
- `test_extract_and_memory_sync_independent_attempts`（extract 耗尽置 status=failed 不影响 memory_sync 运行；memory_sync 耗尽置 memory_status=failed 不影响 note.status=organized）

### INV-3 记忆归属：note_id + source_rev

**陈述**：MemoryStore 中每条由本服务写入的记忆，`metadata` 必含 `note_id` 与 `source_rev`；**记忆只属于 note 的当前 rev**——note 已删除或当前 rev 未 organized 时，store 中不应存在该 note 的记忆；organized 时存在的记忆其 source_rev 应等于 note 当前 rev。API 不向客户端暴露 source_rev（内部字段）。

**保证代码**：`app/memory/base.py::MemoryStore.add`（签名扩为 `add(user_id, text, *, note_id, source_rev)`，实现写 metadata）、`app/memory/mem0_store.py`、`app/memory/inmemory.py`（MemoryItem 增 source_rev）、唯一写入口 `app/pipeline/memory_sync.py`（INV-4/5 保证只有它写）。

**验证测试**：
- `test_memory_metadata_contains_note_id_and_source_rev`
- `test_no_stale_rev_memory_after_repatch`（PATCH 重整理后 /v1/search 不再命中旧文本；C9 的端到端回归）

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
3. **复查**：新短读事务重读 `(rev, deleted_at)`。与第 1 步相同 → 条件 UPDATE `memory_status='synced'`（条件 `id=:id AND rev=读取rev AND deleted_at IS NULL`，不占 seq；若 note 已 failed 且记忆为空则置 `failed`——该状态下不可能再有记忆）；不同 → **只删本次 add 产生的 memory id**（按 id 删，绝不 delete_by_note，修 C9）+ `INSERT memory_sync … ON CONFLICT DO NOTHING` 再入队，job 仍算 done。
4. **收尾**：只校验租约（INV-6）。`(rev, deleted_at)` 足够作为复查键：status 无法在不改 rev/deleted_at 的前提下变化（PATCH 改 rev，DELETE 改 deleted_at，apply 改 rev）。

**串行化方案的选择**（题目要求评估）：

- ❌ **A. PG session 级 advisory lock 覆盖外部调用**：把池化连接钉在秒级 mem0/LLM 调用上，违反本仓「外部调用不占连接」原则（ask/search 专门修过，见 `app/deps.py`）；池回收/断连会静默丢锁；SQLite 无对应物，两后端行为分叉；崩溃安全依赖连接关闭时机。
- ❌ **B. note 行 FOR UPDATE 短事务**：只能串行化「读」，覆盖不了外部调用窗口，无执行串行化效果（题目亦指出不能持锁调 mem0）。
- ✅ **C（选定）. job 行约束串行化**：claim 快路径加 `NOT EXISTS (running 同 note 的 memory_sync)` 过滤 + 部分唯一索引 `UNIQUE(note_id) WHERE kind='memory_sync' AND status='running'` 在**提交时硬保证**（两个 worker 并发把同 note 的两个 pending 都置 running，必有一个在提交时违反索引 → IntegrityError → savepoint 回滚该候选、继续找下一个）。不占连接、PG/SQLite 都支持、崩溃安全（worker 死 → 行仍 running → 既有租约回收语义照常）。
- ⚠️ **D（升级备选）. notes.memory_sync_owner 执行令牌**（条件 UPDATE 抢 note 行所有权）：更强的互斥，但多一列与「回收时偷令牌」逻辑；由于 INV-5 的复查/删己/再入队已保证正确性不依赖串行化（串行化只是减少重复外部调用与交错），当前量级不需要，记录为升级路径。

**正确性论证（收敛到「记忆 == f(note 终态)」）**：

关键前提 P1：**任何改变 note `(rev, deleted_at, status)` 的已提交事务都伴随 memory_sync 入队**——create/apply 入队（apply 事务内），PATCH/DELETE 入队（API 事务内），memory_sync 复查不一致再入队。

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
| 租约过期被回收后原 worker 复活 | 串行化索引约束的是 job 行状态，复活 worker 的执行不再被索引挡住——由复查/删己/再入队兜底（正确性不依赖串行化）；其收尾因租约校验失效而放弃（INV-6），不会写 note |

**保证代码**：`app/pipeline/memory_sync.py`（新文件，含四步与论证对应实现）、`app/jobs/queue.py`（claim 的 NOT EXISTS 快路径 + running 唯一索引冲突的 savepoint 处理）。

**验证测试**：
- `test_memory_sync_writes_memory_with_source_rev_and_marks_synced`
- `test_memory_sync_on_deleted_note_removes_memory`（替代原 cleanup_memory 系列语义）
- `test_memory_sync_deleted_path_always_calls_delete_by_note`（4c1c9be P2-1：已删除路径无条件 delete_by_note，哪怕 store 里本无记忆）
- `test_two_worker_delete_vs_add_converges_no_orphan`（4c1c9be P2-1 两 worker 交错：旧 cleanup 先跑完、慢 add 落地、worker 复查前崩溃 → 租约回收重跑清掉孤儿，最终搜不到）
- `test_memory_sync_on_unorganized_note_clears_stale_memory_without_add`（C4 回归：PATCH 后 extract 永久失败，旧记忆被清、无新增）
- `test_memory_sync_requeues_and_deletes_only_own_ids_on_rev_change`（C9 回归 / 4c1c9be P2-2：用 DelayedAddStore 模拟慢 add，PATCH 插入后旧任务只删自己写的 id，并发新写入立刻且最终都可检索；双后端语义验证）
- `test_memory_sync_converges_after_delete_during_add`（删除后不复活）
- `test_memory_sync_worker_killed_mid_run_reclaimed_converges`（模拟租约过期回收后重跑收敛）
- `test_memory_sync_attempts_exhausted_marks_failed_only_without_newer_job`（有更新的 pending/running 时不标 failed）

### INV-6 任务收尾校验

**陈述**：收尾必须带租约条件 `WHERE status='running' AND locked_by=:me AND locked_at=:claimed`（租约失效则放弃一切写入，既有规则）。在此之上：**extract 收尾失败标记额外受入队 rev 条件约束**（`note.rev=入队rev AND deleted_at IS NULL AND status!='organized'`）；**memory_sync 收尾只校验租约**（不带 rev，其失败标记 memory_status 受 INV-2.5 的「无其他 pending/running」条件约束）。回收判死路径（`_fail_expired`）同理按 kind 分流。

**保证代码**：`app/jobs/queue.py::finalize_claim`、`_fail_expired`（按 kind 选择 note 标记条件）、`app/pipeline/runner.py::_finalize`。

**验证测试**：
- `test_stale_worker_finalize_ignored_after_lease_reclaimed_extract` / `test_stale_worker_finalize_ignored_after_lease_reclaimed_memory_sync`（两种 kind 各一条）
- `test_extract_finalize_marks_failed_only_at_enqueued_rev`
- `test_memory_sync_finalize_failure_marks_memory_status_not_note_status`

### INV-7 所有外部输入拒绝 NUL

**陈述**：任何可能进入数据库（或被拼进 LLM prompt / JSON 序列化）的外部字符串，含 `\x00` 时必须得到 422，而不是 PG 编码错误 500。统一方案 = **中间件 + schemas 双层**：

1. **中间件**（新 `app/middleware.py`，注册于 `create_app`）：对每个请求的 URL path 与 query string **解码后**检查 `\x00`（`request.url.path` 与 `query_params` 各值），命中直接返回 422 `{"error":{"code":"validation_error",...}}`，不进入路由。覆盖所有路由的 path/query 参数。
2. **schemas**（既有 `NoNulStr`/`reject_nul` 保留）：所有请求体字符串字段；path/query 参数声明处再加 `Annotated[NoNulStr, ...]`（如 `note_id`）作第二道防线。

**覆盖面清单**：

| 入口 | 机制 |
| --- | --- |
| body：LoginRequest/RefreshRequest/NoteCreate/NotePatch/AskRequest 全部字符串字段 | schemas `NoNulStr`（既有） |
| body：NoteCreate.asr（dict 任意层级键/值） | `_reject_nul_everywhere` 递归（既有） |
| path：`/v1/notes/{note_id}` 等 | 中间件 + 参数 `NoNulStr` 注解（修 C8） |
| query：`/v1/search?q=`、`/v1/sync?since=&limit=`、`/v1/notes?cursor=&limit=` | 中间件（q 已有 `NoNulStr`，since/limit 为 int） |
| header | 当前**无任何 header 值入库**（authorization 只做 JWT 解析，不落库）；规则：未来任何从 header 取值入库的字段必须走 `NoNulStr`（自查清单项） |

**保证代码**：`app/middleware.py`（新）、`app/main.py::create_app`（注册）、`app/schemas.py`、`app/api/notes.py`（path 参数注解）。

**验证测试**：
- `test_nul_in_path_param_422`（`/v1/notes/n%00x` 的 GET/PATCH/DELETE）
- `test_nul_in_query_param_422`（`/v1/search?q=a%00b` 等）
- `test_nul_middleware_covers_all_routes`（遍历 app.routes，把每个含参路径注入 `%00`，断言 422 而非 500）
- 既有 `test_nul_rejected_in_note_strings_422` / `test_nul_rejected_in_auth_strings_422` / `test_nul_rejected_in_ask_and_search_422` 系列保留

### INV-8 登录：device 原子 upsert

**陈述**：登录对 device 的写入是**单条** `INSERT … ON CONFLICT (id) DO UPDATE … WHERE devices.user_id = EXCLUDED.user_id`（原子完成「存在性检查 + 归属检查 + refresh hash 覆写」，修 C1）：

- 无冲突 → 插入新设备行（含 refresh_token_hash）；
- 冲突且 user_id 相同 → DO UPDATE 原子覆写 refresh_token_hash、清 prev/revoked、刷新 last_seen（name/platform 仅在请求非空时覆盖，`COALESCE(NULLIF(EXCLUDED.x,''), devices.x)`）；
- 冲突且 user_id 不同 → WHERE 不成立，rowcount=0 → 复读该行确认后返回 **403 `device_conflict`**。

并发语义：同设备同账号并发登录在行/PK 上串行化，两请求都 200，refresh hash 后写者胜（每响应的 token 在响应时刻都有效；先返回者的 refresh 会被后写者覆盖，客户端 refresh 收 401 时重新 login 即可——单设备个人应用下这是期望语义，且**不再出现 IntegrityError 500 或丢更新异常**）。并发首登同设备（均无行）由 PK 冲突 upsert 原子解决。

**保证代码**：`app/api/auth.py::login`（删除「先 get 再分支改写」的读-写窗口，改为 upsert + rowcount 分流）。锁序：invite → user → device（INV-1 表）。

**验证测试**：
- `test_login_device_bound_to_other_user_403`
- `test_concurrent_login_same_device_linearizable_pg`（PG，真并发独立会话：N 个并发登录同 device 同 invite，全部 200 且无 500，库中恰一行、hash 与**最后完成者**一致）
- `test_concurrent_first_login_same_device_no_integrity_error`（PG，真并发）

### INV-9 建表/迁移：一次性 init-db

**陈述**：`create_all` 不得在 api/worker 启动时并发执行（修 C6）：

- 新命令 `python -m app.cli init-db`：PG 下在**单个事务**内先 `SELECT pg_advisory_xact_lock(<固定key>)` 再 `Base.metadata.create_all`（DDL 事务性 + 咨询锁，并发执行安全且串行化）；SQLite 下直接 create_all。
- `main.py` lifespan 与 `app/jobs/worker.py`：`DATABASE_URL` 为 postgresql 时**不**建表（表缺失时让首个查询快速失败，暴露部署错误）；SQLite（本地开发/测试）保留启动建表。
- `server/docker-compose.yml`：新增 one-shot `migrate` 服务（`command: python -m app.cli init-db`），`api`/`worker` 改为 `depends_on: migrate: condition: service_completed_successfully`（postgres 健康后先跑 migrate）。
- 测试不受影响：conftest 自行 drop/create（测试基建，非应用启动路径）。

**保证代码**：`app/cli.py`（init-db 子命令）、`app/db.py`（create_all 增加 PG 咨询锁包装或新函数）、`app/main.py`、`app/jobs/worker.py`、`server/docker-compose.yml`。

**验证测试**：
- `test_init_db_concurrent_no_error_pg`（PG，真并发两个独立连接同时跑 init-db，均成功、无 duplicate-object 错误）
- `test_lifespan_skips_create_all_on_postgres`（PG 测试环境下 monkeypatch 计数 create_all 调用为 0）
- `test_init_db_sqlite_creates_tables`（SQLite 路径）

### INV-10 配置完备性校验

**陈述**（Settings 加载即校验，任何环境）：

1. `LLM_PROVIDER=openai` ⇒ `LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL` 三者必须齐备（修 C3：缺任一项启动即 ValueError，而不是运行期拼出相对 URL / 空 model 让每次 organize/ask 失败）。保留既有生产（postgresql）下 mock 拒绝等规则。
2. `DEFAULT_TIMEZONE` 必须是合法 IANA 时区名（`ZoneInfo(...)` 可构造，修 C7；请求级 timezone 校验复用 `app/schemas.py::_check_timezone` 的逻辑）。

**保证代码**：`app/config.py`（`_reject_misconfigured_llm_in_production` 扩展为三字段 + 新增 `_validate_default_timezone`）。

**验证测试**：
- `test_openai_missing_base_url_rejected` / `test_openai_missing_model_rejected` / `test_openai_missing_api_key_rejected`（已有 key 用例扩展）
- `test_openai_all_three_configured_ok`（另有 `test_openai_missing_two_fields_lists_both` 断言多项缺失一并报出）
- `test_default_timezone_invalid_rejected`（如 `Asia/Bejing`）/ `test_default_timezone_valid_ok`

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

## 3. 数据模型与迁移

- `jobs` 新列：`note_id: str|None`（加索引）、`note_rev: int|None`（extract 专用）；`kind` 值域变为 `{extract, memory_sync}`。
- `jobs` 新索引（`Index(..., unique=True, postgresql_where=..., sqlite_where=...)` 双方言）：
  - `uq_jobs_memsync_pending ON (note_id) WHERE kind='memory_sync' AND status='pending'`
  - `uq_jobs_memsync_running ON (note_id) WHERE kind='memory_sync' AND status='running'`
  - 保留 `ix_jobs_status_run_after`。
- `notes`：删除 `memory_synced`，新增 `memory_status: str(16) default 'pending'`（不进 API 响应、不占 change_seq）。
- `MemoryStore.add(user_id, text, *, note_id, source_rev)`；`MemoryItem` 增 `source_rev`。
- 迁移策略：MVP 未上线，无存量数据兼容义务——PG 用 init-db（create_all 只建缺失表；**已存在的旧 dev 库需丢弃卷重建**，或手动执行文档给出的 `ALTER TABLE notes DROP COLUMN memory_synced, ADD COLUMN memory_status ...` 与 jobs 新列/索引 DDL）；SQLite dev 库删除文件重建。此为已知限制，写入自查清单。

## 4. 自查清单（推送前逐条核对）

1. 三套验证全绿：SQLite pytest、`TEST_DATABASE_URL=postgresql+asyncpg://...` pytest、根目录 `ruff check server`。
2. 每个新增/修改的写路径对照 INV-1 加锁序列表；`grep -rn "update(Job)" server/app/api server/app/pipeline` 为空（业务路径不 UPDATE 既存 job 行）。
3. 事务边界内无 LLM/mem0/网络调用：检查 extract/memory_sync/notes/auth 的每个 `session.begin()` 块。
4. 新增状态变更已同步更新本文档对应 INV 与测试；本文「保证代码」落点与实际文件/函数一致。
5. 并发测试在 PG 下用独立会话/连接真并发；SQLite 下无法复现的竞态 `skipif` 并注明原因（沿用 `requires_postgres` 标记）。
6. init-db 后核对三条部分唯一索引在 PG（`\d jobs`）与 SQLite（`sqlite_master`）都存在（或以测试断言）。
7. 旧 dev 库处理：PG 卷重建或手动 ALTER（见第 3 节）；`.env.example` 若涉及新配置项同步更新（`test_env_example_placeholder_matches_template`、`test_env_example_defaults_to_mem0_backend`、`test_env_example_documents_new_runtime_flags` 系列）。
8. compose 校验：`migrate` one-shot 先于 api/worker（`service_completed_successfully`）；PG 下 api/worker 启动不再建表。
9. 未来任何 header 值入库必须走 `NoNulStr`（INV-7 覆盖面表）。
10. AGENTS.md「写代码」含本文链接且路径有效。

## 5. 测试计划汇总（INV ↔ 计划测试名 ↔ 后端要求）

| INV | 测试（计划名，第二步实现） | 后端 |
| --- | --- | --- |
| INV-1 | test_create_note_sql_order_job_insert_before_seq_update；test_patch_sql_order_note_reminders_jobs_before_seq_update；test_extract_finalize_concurrent_patch_no_deadlock；test_business_paths_never_update_existing_job_rows；保留 test_patch_and_organize_writeback_overlap_no_deadlock、test_patch_delete_organize_three_way_overlap_no_deadlock | 前二双后端；并发项 PG 真并发 |
| INV-2 | test_extract_skips_when_rev_moved_marks_done；test_extract_attempts_exhausted_marks_note_failed_by_rev；test_extract_finalize_skips_note_patched_after_enqueue；test_memory_sync_single_pending_per_note；test_memory_sync_pending_allowed_while_running；test_memory_sync_single_running_per_note；test_extract_and_memory_sync_independent_attempts | 单 running 项 PG 真并发，SQLite 仅语义 |
| INV-3 | test_memory_metadata_contains_note_id_and_source_rev；test_no_stale_rev_memory_after_repatch | 双后端 |
| INV-4 | test_patch_and_delete_never_touch_memory_store_directly | 双后端 |
| INV-5 | test_memory_sync_writes_memory_with_source_rev_and_marks_synced；test_memory_sync_on_deleted_note_removes_memory；test_memory_sync_on_unorganized_note_clears_stale_memory_without_add；test_memory_sync_requeues_and_deletes_only_own_ids_on_rev_change；test_memory_sync_converges_after_delete_during_add；test_memory_sync_worker_killed_mid_run_reclaimed_converges；test_memory_sync_attempts_exhausted_marks_failed_only_without_newer_job | 双后端语义；交错窗口用 DelayedAddStore 类时间注入复现，无需 PG |
| INV-6 | test_stale_worker_finalize_ignored_after_lease_reclaimed_extract、test_stale_worker_finalize_ignored_after_lease_reclaimed_memory_sync；test_extract_finalize_marks_failed_only_at_enqueued_rev；test_memory_sync_finalize_failure_marks_memory_status_not_note_status | 双后端 |
| INV-7 | test_nul_in_path_param_422；test_nul_in_query_param_422；test_nul_middleware_covers_all_routes | 双后端 |
| INV-8 | test_login_device_bound_to_other_user_403；test_concurrent_login_same_device_linearizable_pg；test_concurrent_first_login_same_device_no_integrity_error | 并发项 PG 真并发 |
| INV-9 | test_init_db_concurrent_no_error_pg；test_lifespan_skips_create_all_on_postgres；test_init_db_sqlite_creates_tables | 并发项 PG |
| INV-10 | test_openai_missing_api_key_rejected、test_openai_missing_base_url_rejected、test_openai_missing_model_rejected；test_openai_all_three_configured_ok；test_default_timezone_invalid_rejected；test_default_timezone_valid_ok | 纯配置，无 DB |

既有测试的适配（第二步）：`organize_note`→`extract`、`cleanup_memory`→`memory_sync`、`memory_synced`→`memory_status` 断言改写；test_memory_failure_retries_backfill_only、test_cleanup_memory_job_skips_live_note、test_delete_during_memory_sync_does_not_resurrect 等旧测试按新模型语义迁移（收敛目标不变，断言落点改为 memory_sync）。
