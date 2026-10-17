# 服务端不变量与新模型设计（server-invariants.md）

> 状态：已实施（PR #2）。每条 INV 的「保证代码」与「验证测试」均为现存代码与测试（`test_invariants_doc_test_names_exist` 机械校验测试名存在）；机械检查器见 `scripts/check-invariants.py`。
> 范围：`server/`。本文回答两件事：**服务端必须永远成立的不变量**（INV-x，每条含精确陈述、保证代码、验证测试——测试名为第二步的计划名），以及**任务/记忆新模型**的设计与理由。
> 输入：threads4.txt 的 9 条 Codex 评审意见，下文以 C1–C9 指代：
> C1 auth 并发登录同设备竞态（P1）；C2 create 中 enqueue 晚于 allocate_seq（P1）；C3 openai 配置需三项齐备（P2）；C4 PATCH 后旧记忆无兜底清理（P2）；C5 整理后 rev 变化导致失败收尾永不命中（P1）；C6 create_all 并发建表（P2）；C7 DEFAULT_TIMEZONE 未校验（P2）；C8 path/query 参数 NUL 进 PG（P1）；C9 旧整理任务 delete_by_note 误删新 rev 记忆（P1）。
> 第五轮输入：threads5.txt 的 6 条（2 P1 + 4 P2），在对应 INV 内注明（INV-8 重写为多 refresh token 模型；INV-3 补枚举完整性；INV-5/6 补租约栅栏；INV-2 补内部字段不碰 updated_at；INV-7 补 aware 时间边界；INV-10 补后端可安装性/拓扑校验）。
> 第六轮输入：threads6.txt 的 6 条（3 P1 + 3 P2），在对应 INV 内注明：INV-5 补「丢租约必再入队」（迟到的 delete_by_note 会删掉换主后写入的记忆）；新增 INV-11（写接口乐观锁与 rev 语义，含幂等/无操作快路径）；新增 INV-12（条件更新/删除 0 行后的归因规则）；INV-8 补 refresh 行消失/过期的分类与过期行清理；INV-7 补 LLM 输出时间同套 ±14h 安全界；INV-10 补 MEM0_EMBEDDING_API_KEY。
> 第七轮输入：Codex 评审 1 条 P1（mem0 `add` 未显式 `infer=False`：默认 infer 路径会基于用户既有记忆做 UPDATE/DELETE/NOOP 决策，跨 note 改写破坏归属），在 INV-3 注明「add 必须 infer=False」。

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
3. **API/业务路径对既存 job 行只有 INSERT（`ON CONFLICT DO NOTHING`），永远不 UPDATE 既存 job 行**；既存 job 行的 UPDATE 只允许出现在队列内部（`app/jobs/queue.py` 的 claim / finalize / 回收判死路径），这些路径对业务行的加锁序列固定为 **jobs → note（仅失败标记）→ sequences → change_seq 写回**，不碰 reminders/device。因此「业务路径」与「队列路径」的锁序单向不相交，全库无环。
4. 事务边界内不得调用 LLM / mem0 / 任何外部网络服务（既有规则，保留）。

**各写路径加锁序列**（第二步实现后逐一对应）：

| 路径 | 加锁序列（左先右后） | seq 分配 |
| --- | --- | --- |
| POST /v1/notes（create） | note INSERT（新行）→ jobs INSERT extract（新行）→ flush → **allocate_seq** → note change_seq 写回 → commit。幂等冲突分支（client_note_id 撞唯一约束）整体回滚后只读复查，无二次加锁 | 1 |
| PATCH /v1/notes/:id | note 条件 UPDATE（锁行，乐观锁）→ reminders 条件 UPDATE（作废 suggested）→ jobs INSERT extract + INSERT memory_sync（均新行，DO NOTHING）→ flush → **allocate_seq** → note+reminders change_seq 写回 → commit | 1+N |
| DELETE /v1/notes/:id | note 条件 UPDATE（软删）→ reminders 条件 UPDATE（全量墓碑）→ jobs INSERT memory_sync → flush → **allocate_seq** → 写回 → commit | 1+N |
| extract 短写事务（apply） | note FOR UPDATE（PG）→ reminders（stale 条件 UPDATE + fresh INSERT）→ jobs INSERT memory_sync → flush → **allocate_seq** → 写回 → commit | 1+S+F |
| memory_sync 各短事务 | 读事务：note + job 行纯读无锁。外部调用：无任何会话/锁。置位事务：note 条件 UPDATE（memory_status，不分配 seq、不碰 updated_at/change_seq，见 INV-2.6）。再入队：jobs INSERT。**该路径永不碰 sequences** | 0 |
| 队列 claim | jobs 行（FOR UPDATE SKIP LOCKED / 条件 UPDATE） | 0 |
| 队列回收判死 _fail_expired | jobs 行 → note 条件 UPDATE → **allocate_seq** → 写回（jobs→note→sequences 单向） | 1 |
| 队列 finalize 成功 | jobs 行（FOR UPDATE + 租约条件） | 0 |
| 队列 finalize 失败 | jobs 行（FOR UPDATE + 租约条件）→ note 条件 UPDATE → **allocate_seq** → 写回 | 1 |
| POST /v1/auth/login | invite（读 + 首用条件 UPDATE）→ user INSERT（新行）→ device 原子 upsert（单条 INSERT…ON CONFLICT，见 INV-8）→ refresh_tokens INSERT（新行）+ 修剪 DELETE（本设备最老的超限行）。不碰 sequences | 0 |
| POST /v1/auth/refresh | device 行（PG FOR UPDATE 串行化轮换/吊销）→ refresh_tokens 条件 UPDATE（作废被轮换的那枚）+ 锁内重读归因（threads6 P1-3，纯 SELECT 无新锁）+ INSERT（新枚）+ 修剪 DELETE（超限活行 + 过期行）。不碰 sequences | 0 |
| POST /v1/auth/logout | device 行 → refresh_tokens DELETE（清除该设备全部 token 行） | 0 |
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

### INV-3 记忆归属：note_id + source_rev；枚举必须完整

**陈述**：MemoryStore 中每条由本服务写入的记忆，`metadata` 必含 `note_id` 与 `source_rev`；**记忆只属于 note 的当前 rev**——note 已删除或当前 rev 未 organized 时，store 中不应存在该 note 的记忆；organized 时存在的记忆其 source_rev 应等于 note 当前 rev。API 不向客户端暴露 source_rev（内部字段）。

**add 必须 `infer=False`，禁止跨 note 推理改写**（第七轮 Codex P1）：mem0 的 `add` 默认 `infer=True`——先检索**用户范围内**的既有记忆，再由 LLM 决定 ADD/UPDATE/DELETE/NOOP，相似内容会被判成「相关」而**改写/删除/复用其他 note 的记忆**（保留别人的 id/note_id、换掉文本），直接破坏本条的归属不变量。记忆的删旧写新完全由 memory_sync 的收敛逻辑管理（INV-5：先 `delete_by_note` 再 `add`），后端只做无主见的存储——适配层 `add` 一律显式 `infer=False`（原样插入，恰好只创建本 note 的记忆）。

在此之上（threads5 P1-1）：**对记忆后端做「全量」语义的枚举必须完整——一页取满又无法推进时必须报错，绝不当成已取全/已同步**。`list()` / `delete_by_note()` / `delete_all()` 的契约：

- `delete_by_note(user_id, note_id)`：按 metadata 过滤（`filters={"user_id", "note_id"}`）+ **边取边删**的循环（每轮删掉本轮取到的，下一轮自然前进，无需游标分页）；同一批 id 重复出现（删除未生效）即抛 `MemoryEnumerationIncomplete` 让 job 失败重试。
- `list(user_id)`：mem0 2.2.1 的 `get_all` 没有游标/offset 分页——一页取满即**无法证明已取全**，直接抛 `MemoryEnumerationIncomplete`（不静默截断；调用方让 job 失败重试）。页大小可注入（`Mem0Store(list_page_size=...)`），测试用极小值模拟超限。
- `delete_all(user_id)`：与 `delete_by_note` 同一个「过滤 + 边取边删」循环，返回确切删除数。
- `InMemoryStore` 同语义：可注入 `list_page_size` 上限，`list()` 取满即抛同款错误（进程内字典本身无截断，上限只为让两后端的「取不全即报错」行为都可测）。

**保证代码**：`app/memory/base.py`（`MemoryEnumerationIncomplete` + `add` 签名 `add(user_id, text, *, note_id, source_rev)`）、`app/memory/mem0_store.py`、`app/memory/inmemory.py`、唯一写入口 `app/pipeline/memory_sync.py`（INV-4/5 保证只有它写）。

mem0 版本钉死：`server/requirements-mem0.txt` 写 `mem0ai==2.2.1`（4c1c9be P2-3）。2.x 收回了 `search`/`get_all` 的顶层 `user_id` 参数（作用域只认 `filters={"user_id": ...}`，旧调用直接 ValueError）、`search` 条数参数是 `top_k`（无 `limit`）、`get_all` 默认 `top_k=20` 会截断、**`get_all` 的 `filters` 透传到 pgvector `list()` 且支持 metadata 键**（`payload->>'note_id' = ...`，实测 2.2.1 `mem0/vector_stores/pgvector.py::_build_filter_conditions`；这是 `delete_by_note` 能按 note 精确枚举的依据）、`add` 的 infer 主路径返回裸 list 且不回显 metadata（归属信息由适配层用自己的入参补齐）、**适配层 `add` 一律传 `infer=False`**（默认 `infer=True` 的 LLM 决策会跨 note 改写，见上；`infer=False` 原样插入、返回 `{"results": [...]}` 且不回显 metadata，也不发起任何 LLM 调用）、`delete` 对不存在的 id 抛 ValueError（按已删除吞掉，幂等）。适配集中在 `app/memory/mem0_store.py`。

**验证测试**：
- `test_memory_metadata_contains_note_id_and_source_rev`
- `test_no_stale_rev_memory_after_repatch`（PATCH 重整理后 /v1/search 不再命中旧文本；C9 的端到端回归）
- mem0 2.2.1 调用形状（mock 按 2.2.1 签名建模、传旧参数即抛）：`test_search_scopes_user_via_filters_and_top_k`、`test_get_all_scopes_user_via_filters_and_avoids_truncation`、`test_add_returns_all_written_items_with_note_id_and_rev`、`test_delete_by_ids_delegates_each_id_and_tolerates_missing`
- add 必须 infer=False（第七轮 Codex P1）：`test_add_infer_false_never_rewrites_other_notes_memory`（fake 的 infer=True 路径在文本相似时**原地改写**既有记忆——先 add note A 再 add 相似文本的 note B，A 的记忆原样保留、B 的记忆 note_id=B；真实 mem0ai+pgvector 的同款归属隔离见 `tests/test_mem0_integration.py::test_add_infer_false_never_rewrites_other_notes_memory`，可选环境）
- 枚举完整性（threads5 P1-1）：`test_delete_by_note_filters_by_note_metadata_and_paginates`（fake 的 get_all 按 2.2.1/pgvector 语义过滤 metadata 键 + top_k 截断；注入页大小 2 验证跨页删净）、`test_delete_by_note_raises_when_page_never_shrinks`（取满且同批重复 → 抛错）、`test_list_raises_when_page_full_cannot_advance`、`test_inmemory_list_page_cap_raises_when_full`、`test_memory_sync_fails_not_marks_synced_when_enumeration_incomplete`（枚举不完整 → job 失败重试、memory_status 不置 synced）

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
   - 租约仍在且 `(rev, deleted_at)` 与第 1 步相同 → 条件 UPDATE `memory_status='synced'`（条件 `id=:id AND rev=读取rev AND deleted_at IS NULL`，不占 seq、不碰 `updated_at`/`change_seq`，见 INV-2.6；若 note 已 failed 且记忆为空则置 `failed`——该状态下不可能再有记忆）；
   - 租约仍在但 `(rev, deleted_at)` 已变 → **只删本次 add 产生的 memory id**（按 id 删，绝不 delete_by_note，修 C9）+ `INSERT memory_sync … ON CONFLICT DO NOTHING` 再入队，job 仍算 done；
   - **租约已丢（被回收换主）→ 只删本次 add 产生的 memory id、不置 memory_status，然后无条件再入队一次 memory_sync（`ON CONFLICT DO NOTHING` 与已有 pending 去重），不得静默返回**（threads6 P1-1：「删己 id」只对账自己 add 的部分，**第 2 步的 delete_by_note 本身也可能耗时超过锁超时、在换主执行完成之后才落地**，把新持有者刚写的记忆删掉而队列里再无后继任务——note 永久无记忆且 memory_status=synced。迟到 worker 的全部外部写（delete_by_note + add）在复查前已顺序完成，补偿任务在其后执行，读到最新状态幂等收敛；补偿任务拿新租约，若再丢租约同样再入队——每轮至多一个 pending（部分唯一索引），收敛有界）。
4. **收尾**：只校验租约（INV-6）。`(rev, deleted_at)` 足够作为复查键：status 无法在不改 rev/deleted_at 的前提下变化（PATCH 改 rev，DELETE 改 deleted_at，apply 改 rev）。

**没有锁保护的外部写之后必须无条件再对账一次（threads6 P1-1 的总原则）**：任何绕过数据库锁的写（mem0 的 add / delete / delete_by_note）都无法被栅栏挡住迟到落地；执行体在结束前必须保证「存在一个后继任务会按 note 当前状态收敛」——正常路径靠置位/换主路径靠补偿再入队，唯一例外是复查确认自己仍持租约且状态未变（此时本次执行就是终态收敛）。丢 lease 一律重新入队 memory_sync（唯一索引去重），不得静默返回。

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
- `test_memory_sync_stalled_delete_by_note_past_lease_requeues_and_converges`（threads6 P1-1：旧 worker 停在 delete_by_note 里超过锁超时、新持有者完成、旧删除迟到落地删掉正主记忆 → 复查丢租约后补偿再入队，排空后该笔记仍有记忆、/v1/search 可搜到；PG 必跑，交错用事件钩子强制故两后端同语义）
- `test_memory_sync_worker_killed_mid_run_reclaimed_converges`（模拟租约过期回收后重跑收敛）
- `test_memory_sync_attempts_exhausted_marks_failed_only_without_newer_job`（有更新的 pending/running 时不标 failed）

### INV-6 任务收尾校验

**陈述**：收尾必须带租约条件 `WHERE status='running' AND locked_by=:me AND locked_at=:claimed`（租约失效则放弃一切写入，既有规则）。在此之上：**extract 收尾失败标记额外受入队 rev 条件约束**（`note.rev=入队rev AND deleted_at IS NULL AND status!='organized'`）；**memory_sync 收尾只校验租约**（不带 rev，其失败标记 memory_status 受 INV-2.5 的「无其他 pending/running」条件约束）。回收判死路径（`_fail_expired`）同理按 kind 分流。threads5 P2-1 起这同一租约条件**也约束 memory_sync 执行体的复查**（INV-5 第 3 步）：置位前必须仍持有租约，丢租约的执行不得置位、只删自己刚写的 memory id——threads6 P1-1 起唯一允许的数据库效果是**补偿再入队**（INSERT 新 job 行，不改本 job/note；见 INV-5 第 3 步第三结局）。

**保证代码**：`app/jobs/queue.py::finalize_claim`、`_fail_expired`（按 kind 选择 note 标记条件）、`app/pipeline/runner.py::_finalize`、`app/pipeline/memory_sync.py`（复查的租约栅栏）。

**验证测试**：
- `test_stale_worker_finalize_ignored_after_lease_reclaimed_extract` / `test_stale_worker_finalize_ignored_after_lease_reclaimed_memory_sync`（两种 kind 各一条）
- `test_extract_finalize_marks_failed_only_at_enqueued_rev`
- `test_memory_sync_finalize_failure_marks_memory_status_not_note_status`
- `test_memory_sync_recheck_fences_reclaimed_lease_deletes_own_ids`（threads5 P2-1：租约栅栏在执行体复查处生效）

### INV-7 外部输入硬化：NUL 拒绝 + 病态输入一律 4xx

**陈述**：任何可能进入数据库（或被拼进 LLM prompt / JSON 序列化）的外部输入，含 `\x00` 时必须得到 422，而不是 PG 编码错误 500。同类「编码/绑定/换算必炸」的病态输入也不允许 500（4c1c9be P2-4，全部拦成 400/422）：lone surrogate（UTF-8 编码失败）、非法 UTF-8 请求体（解码失败）、超深嵌套 JSON（解析器递归栈炸裂）、超大整数（SQL 绑定 OverflowError）、换算/本地化成 UTC 越界的极端日期（astimezone OverflowError）。threads5 P2-4 补：**aware 时间的边界与 naive 统一**——先转 UTC，再套用同一条 `[0001-01-02 00:00:00, 9998-12-31 23:59:59.999999]` 安全界（预留最坏 IANA 偏移 ±14h；此前 `9999-12-31T23:59:59Z` 配用户时区 Asia/Shanghai 能过 schema，却在抽取/提醒解析的 `astimezone` 处 OverflowError → note 被标 failed 而不是 422）。threads6 P2 补：**LLM 输出的时间同样套用这条 ±14h 安全界**——`extract` 的 `suggested_reminders[].fire_at` 解析（`_parse_fire_at`）把换算溢出（`astimezone` OverflowError/ValueError）或越界的值**当无效丢弃**（不生成该条 suggested reminder），绝不把异常上抛让 job 重试耗尽、把本来有效的笔记标 failed（`9999-12-31T23:59:59` 配 America/New_York 这类「语法合法、换算必炸」的值只能丢提醒，不能丢笔记）。界常量与判定函数放在 `app/schemas.py` 一处（`utc_safe_datetime`），请求体校验（SafeDatetime）与 LLM 输出解析共用，不允许各写一套边界。统一方案 = **中间件 + schemas + 异常兜底三层**：

1. **中间件**（`app/middleware.py`，注册于 `create_app`）：对每个请求的 URL path 与 query string **解码后**检查 `\x00`（`request.url.path` 与 `query_params` 各值），命中直接返回 422 `{"error":{"code":"validation_error",...}}`，不进入路由。覆盖所有路由的 path/query 参数。
2. **schemas**（既有 `NoNulStr`/`reject_nul` 保留）：所有请求体字符串字段；path/query 参数声明处再加 `Annotated[NoNulStr, ...]`（如 `note_id`）作第二道防线。在此之上：typed str 字段的 lone surrogate 由 pydantic 自带拒绝（string_unicode）；`NoteCreate.asr` 走 `_reject_invalid_unicode_everywhere` 递归（NUL + surrogate + 嵌套深度 ≤ `_MAX_ASR_DEPTH=16`）；`SafeDatetime`（`NoteCreate.recorded_at`/`AskRequest.client_time`）对 **aware 与 naive 一视同仁**：换算/视为 UTC 后的值必须落在 `[0001-01-02 00:00:00, 9998-12-31 23:59:59.999999]`（任何 ±14h 内的 `astimezone` 都不再越界）；`NotePatch.rev` 约束在 int4 范围。
3. **异常兜底**（`app/errors.py::recursion_error_handler`，注册于 `create_app`）：超深嵌套在 json 解析阶段就 RecursionError（schema 校验根本看不到），拦成 422；非法 UTF-8 请求体由 Starlette 解码失败路径返回 400。

**覆盖面清单**：

| 入口 | 机制 |
| --- | --- |
| body：LoginRequest/RefreshRequest/NoteCreate/NotePatch/AskRequest 全部字符串字段 | schemas `NoNulStr`（既有；surrogate 由 pydantic str 校验拒绝） |
| body：NoteCreate.asr（dict 任意层级键/值） | `_reject_invalid_unicode_everywhere` 递归：NUL + lone surrogate + 深度 ≤16 |
| body：recorded_at / client_time | `SafeDatetime`：换算/视为 UTC 后的值必须落在 `[0001-01-02, 9998-12-31 23:59:59.999999]`（aware/naive 统一；预留 ±14h） |
| body：NotePatch.rev | `Field(ge=1, le=int4 上界)` |
| body：超深嵌套 JSON / 非法 UTF-8 字节 | RecursionError 处理器 422 / Starlette 解码失败 400 |
| LLM 输出：suggested_reminders[].fire_at（threads6 P2） | `_parse_fire_at` 复用 `utc_safe_datetime`：换算溢出/越界 → 丢弃该提醒（笔记照常 organized），不上抛异常 |
| path：`/v1/notes/{note_id}` 等 | 中间件 + 参数 `NoNulStr` 注解（修 C8） |
| query：`/v1/search?q=`、`/v1/sync?since=&limit=`、`/v1/notes?cursor=&limit=` | 中间件（q 已有 `NoNulStr`）；since/cursor 补 `le=int8 上界`（超大整数绑定 OverflowError） |
| header | 当前**无任何 header 值入库**（authorization 只做 JWT 解析，不落库）；规则：未来任何从 header 取值入库的字段必须走 `NoNulStr`（自查清单项） |

**保证代码**：`app/middleware.py`、`app/main.py::create_app`（注册）、`app/schemas.py`（含 `utc_safe_datetime` 界判定）、`app/errors.py`、`app/api/notes.py`（path 参数注解、cursor 上界）、`app/api/sync.py`（since 上界）、`app/pipeline/extract.py::_parse_fire_at`（LLM 输出同界）。

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

### INV-8 登录与 refresh：device 原子 upsert + 每设备多枚 refresh token

**陈述 1（核心承诺，threads5 P1-2）**：**只要登录（或 refresh）返回成功，返回的凭证在过期（`expires_at`，30 天 TTL 落库强制）或被显式作废（轮换消耗/设备吊销/登出/被每设备上限挤出）之前必须有效**——不存在「200 返回的 token 因为另一个并发 200 而天生无效」。

**陈述 2（存储模型）**：refresh 凭据不再挤在 `devices` 行上（旧模型单列 `refresh_token_hash` 后写者胜，正是并发登录互相覆盖的根因），改存 **`refresh_tokens` 表**（`id, device_id, user_id, token_hash UNIQUE, created_at, expires_at, revoked_at`；DDL 见第 3 节）：

- **登录只追加**：每次成功登录 INSERT 一行新 token（不覆盖任何既有行），同一事务内修剪：每设备至多保留 **5 枚**未过期未作废的 token，修剪条件**排除本行**（按 `created_at DESC, id DESC` 保留最新的 4 枚其他行）——本次返回的 token 一定幸存；同设备并发登录在 device 行 upsert 的行锁上串行化（锁序 device → refresh_tokens，见 INV-1）。修剪同时顺手 DELETE 该设备**已过期**（`expires_at <= now`，无论是否作废）的行（threads6 P2：见下方清理策略）。
- **refresh 轮换只作废被轮换的那一枚**：条件 `UPDATE refresh_tokens SET revoked_at=now WHERE id=:row AND revoked_at IS NULL AND expires_at>now`，**rowcount==1 才**发新 token（追加一行新 TTL）并修剪；**rowcount==0 时不得直接断定为重放（threads6 P1-3 / INV-12）**——首次读行（无锁）与拿到 device 行锁之间存在窗口，期间行可能已被第 6 次登录的上限修剪或 logout 删除，或恰好跨过 `expires_at`。必须在**已持有 device 锁的会话里重读该行**归因：
  - 行不存在（被挤出/登出删除）→ 401 `unauthorized`，**不吊销设备**（挤出/登出的既定语义就是普通 401）；
  - `revoked_at` 非空 → 确系被轮换/吊销后的重放 → 按复用吊销整台设备（原语义）；
  - `revoked_at` 为空却没轮换成功 → 只可能是等待锁期间跨过过期时刻（UPDATE 里的 `now` 是请求起始时刻）→ 401 `unauthorized`（过期），不吊销设备。
- **重放已作废（被轮换/吊销）的 token** → 401 `refresh_reused` + 吊销**整台设备**（`devices.revoked_at` + 作废其全部 token；既有语义保留）。被 5 枚上限挤出（行已删除）的 token 重放得到普通 401——包括「读行时还在、等锁期间被挤掉」的竞态窗口（上条的重读归因保证它不会被误判成重放）。上限本身是显式作废策略的一种，写入已知取舍。
- **logout** → 清除（DELETE）该设备全部 token 行（不吊销设备行，access 到期自然失效；登出后的重放按既有语义得普通 401，不触发复用吊销——包括等锁期间被登出删除的竞态，同上由重读归因兜住）。
- 过期 token（`expires_at<=now`，未作废）→ 401 unauthorized（TTL 首次真正强制；不吊销设备）。
- **过期行清理策略（threads6 P2）**：登录/刷新追加新 token 的同一事务顺手 DELETE 该设备 `expires_at <= now` 的行——**已吊销（revoked_at 非空）但未过期的行保留到过期为止**（重放识别窗口：被轮换后 30 天内重放仍能关联到设备并触发吊销），过期后即无任何价值、随下一次该设备的登录/刷新清掉。活跃设备的表体积由此有界（≤5 枚活行 + 未过期的已吊销行）；完全失联设备的历史行不主动清理（可选的全局定期 GC 记为后续项， MVP 量级接受）。

**陈述 3（device 原子 upsert，修 C1，保留）**：登录对 device 的写入仍是**单条** `INSERT … ON CONFLICT (id) DO UPDATE … WHERE devices.user_id = EXCLUDED.user_id`（原子完成「存在性检查 + 归属检查 + 复活/刷新」，不再写任何 hash 列）：

- 无冲突 → 插入新设备行；
- 冲突且 user_id 相同 → DO UPDATE 清 `revoked_at`、刷新 `last_seen`（name/platform 仅在请求非空时覆盖，`COALESCE(NULLIF(EXCLUDED.x,''), devices.x)`）；
- 冲突且 user_id 不同 → WHERE 不成立，rowcount=0 → 复读该行确认后返回 **403 `device_conflict`**，不触碰原账号的任何 token。

并发语义：同设备同账号并发登录全部 200、**每个返回的 refresh token 都能成功 refresh**（各自一行，互不覆盖）；并发首登同设备（均无行）由 PK 冲突 upsert 原子解决；并发同枚 token 双刷新只有一个轮换成功，另一个按复用吊销设备。

**保证代码**：`app/api/auth.py`（login 的 upsert+追加+修剪、refresh 的条件轮换、logout 全作废）、`app/models.py::RefreshToken`（init-db/create_all 建表）、`app/auth/tokens.py::refresh_expiry`。

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
- 既有 `test_refresh_rotation_and_reuse_revokes_device`（重放语义不变）保留

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
3. **`MEMORY_BACKEND=mem0` ⇒ mem0ai 必须已安装**（threads5 P2-3：`importlib.util.find_spec("mem0")` 探测，失败即启动报错并提示 `pip install -r requirements-mem0.txt`——记忆 store 是懒构造，不查的话 api 照常起、首个 `/search` 才 500，`memory_sync` job 全部 ModuleNotFoundError）。任何环境生效。
4. **inmemory 只属于单进程**：进程内状态跨 api/worker 两进程互不可见。生产（postgresql）+ inmemory 默认拒绝启动；仅单进程跑 PG 测试套件可设 `ALLOW_INMEMORY_IN_TESTS=true` 放行（原 `ALLOW_INMEMORY_IN_PROD` 改名：名字带 IN_PROD 误导部署者以为生产可开；**worker 进程无论该标志都拒绝 inmemory+PG**，`app/jobs/worker.py::check_memory_backend_shareable`）；SQLite 本地开发允许 inmemory，但 `.env.example` 与 README 写明其单进程局限（worker 写的记忆对 api 进程的 /search 不可见，完整链路用 compose/mem0）。`.env.example` 的本地默认值因此是 `inmemory`，mem0 行注释给出（未安装时启动即报第 3 条的错）。
5. **`MEM0_EMBEDDING_API_KEY`（未设回退 `LLM_API_KEY`）**（threads6 P2）：mem0 的 embedding 可以与聊天 LLM 不同源（如 LLM 用 DeepSeek、embedding 用 OpenAI）——`MEM0_EMBEDDING_BASE_URL` 指向别的供应商时，embedding 端点的凭证不能想当然复用 `LLM_API_KEY`。取值顺序：`MEM0_EMBEDDING_API_KEY` → 未设则 `LLM_API_KEY`（同源部署零配置）。**启动校验**：设置了 `MEM0_EMBEDDING_BASE_URL` 或 `MEM0_EMBEDDING_API_KEY`（即显式自定义了 embedding 配置）而按上述顺序解析出的 key 为空时，启动即 ValueError——否则应用能正常启动，但 mem0 的每次 add/search 都在 embedding 端点上鉴权失败，memory_sync job 全部耗尽重试（问题被掩盖到队列里）。
6. **mem0 2.2.1 适配层的两个实测事实**（threads6 集成验证，fake client 测不出——`from_config` 收任意 dict、不碰真实工厂）：
   - llm/embedder 的 OpenAI 兼容配置键是 **`openai_base_url`**：`BaseLlmConfig`/`BaseEmbedderConfig` 都不收 `base_url`，传错键第一次构造 store 就 TypeError（`test_mem0_config_keys_match_real_backend` 把键名钉住）。
   - mem0ai **不依赖任何 PostgreSQL 驱动**：pgvector 后端运行期才 `import psycopg`（缺了回落 psycopg2，再缺 ImportError）——`requirements-mem0.txt` 显式带 `psycopg[binary,pool]`，否则只装 mem0ai 的生产镜像第一次构造 store 就 ImportError。

**保证代码**：`app/config.py`（`_require_complete_openai_config`、`_validate_default_timezone`、`_require_memory_backend_installed`、`_reject_inmemory_memory_backend_in_production`、`_require_embedding_key_when_customized`）、`app/memory/mem0_store.py`（embedder `api_key` 用回退链；llm/embedder 配置键 `openai_base_url`）、`requirements-mem0.txt`（psycopg 驱动）、`app/jobs/worker.py::check_memory_backend_shareable`、`server/.env.example`、`server/README.md`。

**验证测试**：
- `test_openai_missing_base_url_rejected` / `test_openai_missing_model_rejected` / `test_openai_missing_api_key_rejected`（已有 key 用例扩展）
- `test_openai_all_three_configured_ok`（另有 `test_openai_missing_two_fields_lists_both` 断言多项缺失一并报出）
- `test_default_timezone_invalid_rejected`（如 `Asia/Bejing`）/ `test_default_timezone_valid_ok`
- `test_mem0_backend_missing_mem0ai_rejected_at_startup`（threads5 P2-3：错误信息含安装命令）
- `test_mem0_embedding_api_key_falls_back_to_llm_key`（threads6 P2 / INV-10.5：设了 `MEM0_EMBEDDING_API_KEY` 用它，未设回退 `LLM_API_KEY`——用捕获 config 的假 mem0 断言传给 embedder 的 key）
- `test_mem0_config_keys_match_real_backend`（threads6 集成实测：mem0 2.2.1 的配置键是 `openai_base_url`，传 `base_url` 构造即 TypeError——fake 测不出，键名钉死防回退）
- `test_mem0_embedding_custom_config_without_key_rejected`（threads6 P2：自定义了 embedding 配置而无任何可用 key → 启动 ValueError）
- `test_worker_refuses_inmemory_with_postgres`（worker 启动检查，无视放行标志）
- `test_env_example_local_default_inmemory_documents_mem0`（模板本地默认 inmemory、mem0 行与安装说明齐全）
- `test_mem0_constructs_and_scopes_user_and_note` / `test_mem0_delete_by_note_paginates_past_page_cap`（threads6 真实集成：真 mem0ai 2.2.1 + pgvector + 确定性本地 embedder——user/note 过滤下推、小页上限下的穷尽删除；常规环境按原因跳过，运行方式见 `tests/test_mem0_integration.py` 模块 docstring）

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

- **回滚后重读**：`rollback` → 重新 SELECT 该行 → 按当前值分类。正面样例：PATCH 的 404/409 复查（`_raise_conflict_or_not_found`）、login 邀请码抢占后重读（owner 已有 → 登录该 owner）、login device upsert rowcount==0 后重读（异 user → 403）、refresh 轮换 rowcount==0 后**锁内**重读 token 行（不存在 → 401 不吊销 / revoked → 重放吊销 / 未 revoked → 过期 401，INV-8）。
- **因果封闭论证豁免**：若能证明「0 行的所有可达成因都导向同一个正确动作」，可免重读，但必须在代码注释里写明论证。现仓豁免点：DELETE note 的 0 行 → 404（进 UPDATE 前刚确认过行存在，条件只有 `deleted_at IS NULL`，无 un-delete 路径 ⟹ 唯一成因是并发软删）；队列 claim/reclaim/finalize 的各 0 行 → 跳过该候选（成因 ∈ {被抢/已收尾/已回收}，跳过对一切成因都正确）；`_mark_memory_status`/`_mark_extract_failed`/`_mark_memory_sync_failed` 的 0 行 → 不标（条件里显式携带归因前提，前提不满足即不该标；且 (rev, deleted_at) 变化必伴随后继任务，INV-5 P1）。

**保证代码**：`app/api/notes.py`、`app/api/auth.py`、`app/jobs/queue.py`、`app/pipeline/memory_sync.py`（上述各点，含注释论证）。

**验证测试**：
- `test_refresh_race_row_deleted_while_waiting_device_lock_401_no_revoke`（0 行的成因是「行被删除」而非「重放」：错误归因会吊销整台设备）
- `test_concurrent_sixth_login_eviction_vs_refresh_no_device_revoke_pg`（同上，PG 真并发窗口）
- 既有正面样例回归：`test_concurrent_patch_same_rev_exactly_one_success`（PATCH 0 行 → 重读归因 409/404）、`test_login_device_bound_to_other_user_403`（upsert 0 行 → 重读归因 403）

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
| 记忆全量枚举（threads5 P1-1） | metadata 过滤（pgvector `payload->>'note_id'`）+ 边取边删循环推进；取不满一页才算删净；InMemoryStore 同语义可注入上限 | 固定大 top_k（如 1000）当「全量」：超限用户的老笔记记忆删不净且 job 报成功；只按 user 全量列举再过滤：同样受页上限截断 |
| aware 时间边界（threads5 P2-4） | 先转 UTC 再套与 naive 相同的 `[0001-01-02, 9998-12-31]` 界 | 只校验「能转成 UTC」：`9999-12-31T23:59:59Z` 合法入库，抽取/提醒解析在用户时区 `astimezone` 处 OverflowError，note 被标 failed 而不是 422 |
| 丢租约后的外部写（threads6 P1-1） | 复查丢租约 → 删己 id + 无条件补偿再入队（唯一索引去重） | 只删己 id 就返回：迟到的 delete_by_note 会删掉换主后写入的记忆且再无后继任务，note 永久无记忆而 memory_status=synced |
| PATCH 内容不变快路径（threads6 P1-2） | 快路径也先做自赋值条件 UPDATE 原子校验 rev | 无锁读后直接 200：过期 rev + 恰好等于旧内容的请求绕过乐观锁，客户端把过期视图当当前态 |
| 条件写 0 行的归因（threads6 P1-3） | 重读当前状态（或因果封闭论证豁免，注释载明）再分类 | 按猜测归因：refresh 轮换 0 行被当成重放 → 等锁期间被挤出/登出的 token 错吊销整台设备 |
| refresh 行清理（threads6 P2） | 登录/刷新时顺手删 `expires_at<=now` 的行；已吊销未过期的保留到过期 | 不清理：常刷新设备的已吊销/过期行无限堆积；只清活行则上限约束不住表体积 |
| embedding 凭证（threads6 P2） | `MEM0_EMBEDDING_API_KEY` 未设回退 `LLM_API_KEY` + 自定义配置的启动校验 | 固定用 `LLM_API_KEY`：embedding 与聊天不同源时应用能启动但每次 add/search 都 401，问题被掩盖到队列耗尽 |

## 3. 数据模型与迁移

- `jobs` 新列：`note_id: str|None`（加索引）、`note_rev: int|None`（extract 专用）；`kind` 值域变为 `{extract, memory_sync}`。
- `jobs` 新索引（`Index(..., unique=True, postgresql_where=..., sqlite_where=...)` 双方言）：
  - `uq_jobs_memsync_pending ON (note_id) WHERE kind='memory_sync' AND status='pending'`
  - `uq_jobs_memsync_running ON (note_id) WHERE kind='memory_sync' AND status='running'`
  - 保留 `ix_jobs_status_run_after`。
- `notes`：删除 `memory_synced`，新增 `memory_status: str(16) default 'pending'`（不进 API 响应、不占 change_seq，其更新也不触碰 `updated_at`/`change_seq`，见 INV-2.6）。
- `devices`：**删除 `refresh_token_hash` / `prev_refresh_token_hash`**（threads5 P1-2，凭据移出 device 行）。
- 新表 `refresh_tokens`（threads5 P1-2，INV-8）：`id: str(32) PK`、`device_id: str(64) FK devices.id`（索引）、`user_id: str(32) FK users.id`、`token_hash: str(128) UNIQUE`、`created_at`、`expires_at`、`revoked_at: nullable`。init-db/create_all 经 `app.models` 注册自动建表。
- `MemoryStore.add(user_id, text, *, note_id, source_rev)`；`MemoryItem` 增 `source_rev`；新增 `MemoryEnumerationIncomplete`（INV-3 枚举完整性）。
- 迁移策略：MVP 未上线，无存量数据兼容义务——PG 用 init-db（create_all 只建缺失表；**已存在的旧 dev 库需丢弃卷重建**，或手动执行文档给出的 `ALTER TABLE notes DROP COLUMN memory_synced, ADD COLUMN memory_status ...`、`ALTER TABLE devices DROP COLUMN refresh_token_hash, DROP COLUMN prev_refresh_token_hash`、`CREATE TABLE refresh_tokens ...` 与 jobs 新列/索引 DDL）；SQLite dev 库删除文件重建。此为已知限制，写入自查清单。

## 4. 自查清单（推送前逐条核对）

1. 三套验证全绿：SQLite pytest、`TEST_DATABASE_URL=postgresql+asyncpg://...` pytest、根目录 `ruff check server`。
2. 每个新增/修改的写路径对照 INV-1 加锁序列表；`grep -rn "update(Job)" server/app/api server/app/pipeline` 为空（业务路径不 UPDATE 既存 job 行）。
3. 事务边界内无 LLM/mem0/网络调用：检查 extract/memory_sync/notes/auth 的每个 `session.begin()` 块。
4. 新增状态变更已同步更新本文档对应 INV 与测试；本文「保证代码」落点与实际文件/函数一致。
5. 并发测试在 PG 下用独立会话/连接真并发；SQLite 下无法复现的竞态 `skipif` 并注明原因（沿用 `requires_postgres` 标记）。
6. init-db 后核对三条部分唯一索引在 PG（`\d jobs`）与 SQLite（`sqlite_master`）都存在（或以测试断言）。
7. 旧 dev 库处理：PG 卷重建或手动 ALTER（见第 3 节）；`.env.example` 若涉及新配置项同步更新（`test_env_example_placeholder_matches_template`、`test_env_example_local_default_inmemory_documents_mem0`、`test_env_example_documents_new_runtime_flags` 系列）。
8. compose 校验：`migrate` one-shot 先于 api/worker（`service_completed_successfully`）；PG 下 api/worker 启动不再建表。
9. 未来任何 header 值入库必须走 `NoNulStr`（INV-7 覆盖面表）。
10. 新增携带版本的写接口（或给既有接口加 rev）：对照 INV-11——快路径/幂等命中也必须先原子校验 rev，409 体用 `error_body("conflict", …)` 两段式。
11. 新增条件 UPDATE/DELETE 且消费 rowcount：对照 INV-12——0 行后重读归因，或注释里写因果封闭论证；禁止按单一猜测直接定性（重放/冲突/已删除）。
12. 新增绕过数据库锁的外部写路径：对照 INV-5 总原则——外部写之后无条件再对账一次，丢租约必补偿再入队，不得静默返回。
13. AGENTS.md「写代码」含本文链接且路径有效。

## 5. 测试计划汇总（INV ↔ 计划测试名 ↔ 后端要求）

| INV | 测试（计划名，第二步实现） | 后端 |
| --- | --- | --- |
| INV-1 | test_create_note_sql_order_job_insert_before_seq_update；test_patch_sql_order_note_reminders_jobs_before_seq_update；test_extract_finalize_concurrent_patch_no_deadlock；test_business_paths_never_update_existing_job_rows；保留 test_patch_and_organize_writeback_overlap_no_deadlock、test_patch_delete_organize_three_way_overlap_no_deadlock | 前二双后端；并发项 PG 真并发 |
| INV-2 | test_extract_skips_when_rev_moved_marks_done；test_extract_attempts_exhausted_marks_note_failed_by_rev；test_extract_finalize_skips_note_patched_after_enqueue；test_memory_sync_single_pending_per_note；test_memory_sync_pending_allowed_while_running；test_memory_sync_single_running_per_note；test_extract_and_memory_sync_independent_attempts | 单 running 项 PG 真并发，SQLite 仅语义 |
| INV-3 | test_memory_metadata_contains_note_id_and_source_rev；test_no_stale_rev_memory_after_repatch；test_add_infer_false_never_rewrites_other_notes_memory；test_delete_by_note_filters_by_note_metadata_and_paginates；test_delete_by_note_raises_when_page_never_shrinks；test_list_raises_when_page_full_cannot_advance；test_inmemory_list_page_cap_raises_when_full；test_memory_sync_fails_not_marks_synced_when_enumeration_incomplete | 双后端（mem0 形状用 fake client） |
| INV-4 | test_patch_and_delete_never_touch_memory_store_directly | 双后端 |
| INV-5 | test_memory_sync_writes_memory_with_source_rev_and_marks_synced；test_memory_sync_on_deleted_note_removes_memory；test_memory_sync_on_unorganized_note_clears_stale_memory_without_add；test_memory_sync_requeues_and_deletes_only_own_ids_on_rev_change；test_memory_sync_converges_after_delete_during_add；test_memory_sync_recheck_fences_reclaimed_lease_deletes_own_ids；test_memory_sync_stalled_delete_by_note_past_lease_requeues_and_converges；test_memory_sync_worker_killed_mid_run_reclaimed_converges；test_memory_sync_attempts_exhausted_marks_failed_only_without_newer_job | 双后端语义；交错窗口用 DelayedAddStore 类时间注入复现（threads6 的停顿在 delete_by_note，同一手法），无需 PG |
| INV-6 | test_stale_worker_finalize_ignored_after_lease_reclaimed_extract、test_stale_worker_finalize_ignored_after_lease_reclaimed_memory_sync；test_extract_finalize_marks_failed_only_at_enqueued_rev；test_memory_sync_finalize_failure_marks_memory_status_not_note_status | 双后端 |
| INV-7 | test_nul_in_path_param_422；test_nul_in_query_param_422；test_nul_middleware_covers_all_routes；test_lone_surrogate_in_body_rejected_422；test_invalid_utf8_body_rejected_4xx；test_deeply_nested_asr_over_depth_limit_422；test_absurdly_deep_body_recursion_guard_422；test_huge_int_inputs_422；test_extreme_datetime_inputs_422；test_aware_datetime_boundary_rejected_before_extraction_no_500；test_llm_boundary_fire_at_overflow_discarded | 双后端 |
| INV-8 | test_login_device_bound_to_other_user_403；test_concurrent_login_same_device_all_returned_tokens_refreshable；test_concurrent_first_login_same_device_no_integrity_error；test_refresh_token_cap_evicts_oldest_keeps_current；test_refresh_rotation_revokes_only_rotated_token；test_logout_revokes_all_device_refresh_tokens；test_refresh_expired_token_rejected_401；test_refresh_race_row_deleted_while_waiting_device_lock_401_no_revoke；test_refresh_prunes_expired_rows_keeps_revoked_until_expiry | 并发项 PG 真并发；threads6 竞态项钩子注入两后端可跑 |
| INV-9 | test_init_db_concurrent_no_error_pg；test_lifespan_skips_create_all_on_postgres；test_init_db_sqlite_creates_tables | 并发项 PG |
| INV-10 | test_openai_missing_api_key_rejected、test_openai_missing_base_url_rejected、test_openai_missing_model_rejected；test_openai_all_three_configured_ok；test_default_timezone_invalid_rejected；test_default_timezone_valid_ok；test_mem0_backend_missing_mem0ai_rejected_at_startup；test_mem0_embedding_api_key_falls_back_to_llm_key；test_mem0_embedding_custom_config_without_key_rejected；test_worker_refuses_inmemory_with_postgres；test_env_example_local_default_inmemory_documents_mem0 | 纯配置，无 DB |
| INV-11 | test_patch_unchanged_text_with_stale_rev_409 | 双后端 |
| INV-12 | test_refresh_race_row_deleted_while_waiting_device_lock_401_no_revoke；test_concurrent_sixth_login_eviction_vs_refresh_no_device_revoke_pg；正面样例回归 test_concurrent_patch_same_rev_exactly_one_success、test_login_device_bound_to_other_user_403 | 竞态项 PG 真并发；钩子注入项双后端 |

既有测试的适配（第二步）：`organize_note`→`extract`、`cleanup_memory`→`memory_sync`、`memory_synced`→`memory_status` 断言改写；test_memory_failure_retries_backfill_only、test_cleanup_memory_job_skips_live_note、test_delete_during_memory_sync_does_not_resurrect 等旧测试按新模型语义迁移（收敛目标不变，断言落点改为 memory_sync）。
