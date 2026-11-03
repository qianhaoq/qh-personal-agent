# Super-agent Runtime（当前实现）

钱浩要求的干净理想态重写，分支 `feat/superagent-subscription-channel`（基线 #26 clean runtime）。本刀**替代 #23/#25 的增量路线**；不延续内存 Engine + pipeline facade，不 cherry-pick S2/S3。产品仍是个人语音随手记、建议提醒、记忆问答，Android 与 `/v1/auth|notes|sync|ask` 的 HTTP 形状保留。

## 当前数据流

```text
POST /v1/notes / PATCH 正文
  → note 事务：清旧派生物、分配 change_seq、commit
    （PATCH 同事务 enqueue memory_sync 清旧记忆）
  → HttpApiSource → Trigger → Engine.receive
  → Store 事务：Thread 串行化 → dedupe → Policy Eval → Mandate → pending Ticket

python -m app.jobs.worker 每轮
  → Runtime.repair：补扫缺 Trigger 的 uploaded note → 回收过期 Assignment
  → pending Ticket → OrganizeNoteWorker
    → CAS claim → 短读 note → 事务外 LLM → report Engine.complete
    → 同事务 CAS running+worker_id → Channel → note/reminders/memory_sync → seq → commit
  → 独立 memory_sync 队列：事务外 MemoryStore 同步 → 租约与 rev 复查

python -m app.jobs.watchdog（Control Loop，独立进程，可与 Worker 并存）
  → 只跑 Runtime.repair：回收过期 Assignment + 补扫缺 Trigger 的 uploaded note
  → 不认领 Ticket、不碰 memory 队列；与 Worker 扫同一 Store，CAS 保证同一
    Ticket 至多一个执行者（单进程部署时 Worker 自带 repair，不必另起）

POST /v1/ask
  → HttpApiSource → Trigger → Engine → pending answer_ask Ticket
  → await AnswerAskWorker：CAS claim → 事务外 Memory.search / LLM
  → Engine.complete → Channel → 持久 result → 同请求返回 answer/citations
```

LLM 与 Memory 都是 Worker 工具端口。Engine 是 Runtime 状态的唯一写入口，Channel 是 Engine 调用的统一结果出口（含 sync 可见 `actions[]`）。Worker 不直接发通知、不设置系统闹钟。默认 `PersonalPolicy v1 / observe` 只授权整理与建议提醒；用户确认后 Channel 才产出 `schedule_reminder` action。显式问答只授权检索和回答。

## 模块地图

| 模块 | 当前职责 |
| --- | --- |
| `app/models.py::SaThread/SaTrigger/SaWorkItem` | `sa_threads`、`sa_triggers`、`sa_work_items` SQLAlchemy 表；PG/SQLite，create_all/init-db 自动注册 |
| `app/domain/store.py` | 数据库会话与事务外快照读取，无主路径内存字典 |
| `app/domain/engine.py` | 原子 intake、版本化 Mandate、Assignment claim/complete/fail、过期恢复 |
| `app/policy/personal_policy.py` | off/observe/auto 准入；显式问答与被动整理权限隔离 |
| `app/sources/base.py, voice_http.py` | HTTP 事件构造；VoiceIngress 接口占位；提交后 note 钩子安全失败 |
| `app/runtime.py` | `repair`（缺失 intake 补扫 + 过期 Assignment 恢复，可复用）+ pending Ticket 认领执行；多个进程竞争同一 Store |
| `app/workers/organize.py` | 认领整理 Ticket、调用工具、向 Engine report |
| `app/workers/extract_impl.py` | 整理快照、prompt、解析/清洗、Channel 使用的写适配器 |
| `app/workers/answer.py` | 问答 Ticket 同请求执行、Memory/LLM 调用、Engine report |
| `app/domain/channel.py` | SyncChannel：note/建议提醒写回、Subscription 激活、确认后 `schedule_reminder` action；Worker 禁止直推端侧副作用 |
| `app/models.py::SaSubscription/SaChannelAction` | 订阅与 sync 可见 Channel actions；CAS/幂等 dedupe |
| `app/api/reminders.py` | `POST /v1/reminders/{id}/confirm` 薄适配 → Trigger → Engine 订阅直通 |
| `app/pipeline/memory_sync.py` | Memory 端口同步执行体，原有收敛/租约补偿协议保留 |
| `app/jobs/watchdog.py` | Control Loop 进程入口：只轮询 `runtime.repair`，不认领 Ticket、不碰 memory 队列 |
| `app/jobs/worker.py` | 进程入口：先 repair，再 pending Ticket，再轮询独立 Memory 队列 |

## 持久化与事务语义

- Thread 有持久 key，Trigger 有唯一 `dedupe_key`，每个 Trigger 至多一个 Ticket。Thread upsert 在短事务内串行同 Thread 的规则 Eval。重复 Trigger 返回原回执，不再次 Eval 或改变 Mandate。
- Mandate 与 Decision 是 Ticket 上的 JSON 快照。Assignment 嵌在 Ticket 的 `worker_id/locked_at/attempts/status` 列；每次 Worker 执行生成新的 execution id，不能把进程 id 当作可重复使用的 Assignment token。
- claim 严格 `UPDATE ... WHERE status='pending'` 且 `rowcount=1` 才成功；claim 等锁后重新取 locked_at。complete/fail 严格要求 `status='running' AND worker_id=:execution`。未命中只返回 false，不猜测不存在/状态冲突。
- complete 的 Ticket CAS 与 Channel 的业务写回在同一事务：先 Ticket 行，再 note → reminders → memory_sync INSERT → flush → sequences 最后。Channel 出错则整个完成事务回滚，Worker 再 report fail。done 不被迟到 fail 覆盖。
- 整理还有 note rev/删除态栅栏，PATCH/DELETE 期间的旧 LLM 输出被丢弃；旧 Ticket 可 done，但不覆盖当前笔记。长 LLM 调用不持会话/锁。
- Organize 工具异常保存 `safe_error_text`，按现有 `job_max_attempts` 与延迟重试，耗尽才在当前 rev 上标 note failed。Memory 同步的 attempts 和失败态独立。
- 轮询会将超时 running Assignment 回收为 pending；耗尽则 failed。过期旧执行因 execution token 不同无法写回。每次判死独立事务，不持 sequences 锁继续领下一个 Ticket。ask 由当前请求领取，后台不重放问答；请求中断可能留下可查询的 pending Ticket，超时 running 记录可回收，但不自动向不存在的请求投递。
- 上传 note 提交与 intake 是两个事务。提交后 intake 异常只记安全摘要、保留 202；轮询用稳定的 note_id+rev 幂等键补扫没有 Trigger 的 uploaded note，因此进程在两者之间中断也不会丢整理。已被 Policy ignore 的 Trigger 不补开 Ticket。
- 重启 Engine、Store、连接池不会清除 Ticket；没有内存 authoritative 状态。生产 PG 先 `init-db`，服务启动不并发建表（INV-9）。

## 旧代码去向

生产 POST/PATCH **不 enqueue extract**。整理实现已迁入 `workers/extract_impl.py`，生产 OrganizeWorker 只调用计算工具，结果经 Engine/Channel 落库。`pipeline/extract.py` 仅是历史 import alias，供旧数据库中 extract 行的排空与既有 Job 租约回归；它不是生产 Worker 的 facade，也不是编排入口。`pipeline/runner.py` 只处理 Memory 同步及历史行，不能创建新整理 Ticket。

旧队列回归显式插入历史 extract 行，继续验证原有 lease/判死/并发保护；生产集成测试读取 `SaWorkItem` 并验证无 extract jobs。`tests/runtime_driver.py` 是有限轮询的集成测试驱动，与生产进程入口的 Runtime + Memory 两个轮询面一致。

## 本地验证

```bash
cd server
.venv/bin/python -m app.cli init-db
.venv/bin/uvicorn app.main:app --no-access-log
# 另一个终端（SQLite 的 inmemory Memory 仅供开发；生产 PG 用 mem0/disabled）
.venv/bin/python -m app.jobs.worker
# 可选第三个终端：Watchdog 只做修复（过期 Assignment 回收 + intake 补扫），
# 与 Worker 并存安全；单进程跑 Worker 时不比另起（Worker 自带 repair）
.venv/bin/python -m app.jobs.watchdog
.venv/bin/python -m pytest
.venv/bin/ruff check app tests
cd ..
python3 scripts/check-invariants.py
```

设 `TEST_DATABASE_URL` 后同一测试套件在 PG 上运行。`test_concurrent_intake_and_claim_single_assignment` 真并发独立 Engine/会话验证幂等与 CAS，`test_late_organize_output_fenced_after_expiry` 验证迟到有效结果不能复活已判死的笔记；原有 PG PATCH/DELETE 锁序与单连接池问答回归保留。

## 提醒确认契约（本刀）

```text
整理完成（observe）
  → Channel.apply_output：suggested reminders + SaSubscription(thread=note:{id}, kind=reminder_confirm)
  → sync：reminders status=suggested；**无** actions/schedule_reminder

POST /v1/reminders/{id}/confirm   body?: { fire_at?, title? }
  → Trigger user_confirmed_reminder（dedupe_key 幂等）
  → Engine：active Subscription 直通原 organize WorkItem（不 Eval、不开新 Ticket）
  → Channel.confirm_reminder：reminder suggested→scheduled + SaChannelAction(type=schedule_reminder)
  → sync：reminders status=scheduled + actions[]（端侧日后可消费为 AlarmManager；本刀不实装）
```

响应：`{ reminder, action, work_item_id, duplicate }`。重复同 body 确认 `duplicate=true` 且 action id 不变。

## 本刀未做

不部署、不自动合并；Android AlarmManager / UI、Panel、多 agent、电脑操控、外部推送、删尽 pipeline alias 均不在本刀。`auto` 仍无主动执行器。
