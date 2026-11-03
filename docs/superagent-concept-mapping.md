# Super-agent 概念映射与本刀落地

状态：干净 Runtime，`feat/superagent-clean-runtime`，2026-11-02。钱浩要求按干净理想态一次对齐；**替代 #23/#25 增量路线**，不在旧 S2/S3 分支补丁，不复用进程内 Engine。

## 思想来源与产品边界

参考 [deepseek-ai/dsh-superagent-prototype](https://github.com/deepseek-ai/dsh-superagent-prototype) 的 Store、Trigger/Thread/Ticket、Engine 唯一写入口与授权快照，以及微信文《Super-agent 设计探索：迈向更自主的 Agent 系统》的连续、并发、主动、持久和 Router/Worker 分工思想。本刀参考已有本地 Store 代码和概念调研，不整仓 fork，不引入 DeepSeek Harness 运行时依赖。

dsh 面向 GitHub/Feishu coding 协作；本产品面向钱浩个人语音随手记、建议提醒、记忆问答。借状态与执行分离、事件驱动、Policy 授权、统一信道，不搬 GitHub Source、coding Session 目录、团队 Panel 或电脑操控。

## 概念 → 当前实现

| 概念 | 当前落点 | 约束 |
| --- | --- | --- |
| Source | HttpApiSource（notes/ask）；VoiceIngress 占位 | HTTP 鉴权仍沿用原有 JWT/设备代际模型；不动 Android |
| Trigger | `sa_triggers` | 不可变事件/Decision；唯一 dedupe_key，笔记按 note_id+rev |
| Thread | `sa_threads` | 一条笔记修订链一个 Thread；ask 独立请求 Thread；intake 短事务串行 Eval |
| Eval | Engine.receive → PersonalPolicy.evaluate | 确定性准入，事务内无外部调用 |
| Mandate | `sa_work_items.mandate` JSON | 不可变 Policy v1 权限快照；默认 observe |
| Ticket / WorkItem | `sa_work_items` | pending → running → done/failed；Store 持久化，重启后可查询 |
| Assignment | Ticket 内 worker_id/locked_at/attempts | 每次执行唯一 token；claim 条件 UPDATE，report 带 running+owner CAS |
| Engine / Router | `domain/engine.py` | Runtime 状态唯一写入口；不含主路径 dict；report 后统一 Channel 出口 |
| Worker / Executor | OrganizeNoteWorker、AnswerAskWorker | 事务外 LLM/Memory 工具；只向 Engine report，不能直接通知 |
| Channel | SyncChannel + `SaChannelAction` | 写 note/建议提醒/订阅；确认后 sync 可见 `schedule_reminder`；ask 返回持久 result |
| Store | `domain/store.py` + SQLAlchemy models | SQLite/PG 同实现，create_all/init-db 自动注册 |
| Effects / 恢复调度 | `runtime.py` repair + `app/jobs/watchdog.py` | Watchdog Control Loop（独立进程）只做恢复：过期 Assignment 回收、缺 intake 补扫；Worker 进程先 repair 再认领 pending 整理 Ticket；整理失败重试随认领执行 |
| Memory | `memory/*` + 独立 memory_sync job | 仅工具端口；按 note_id/source_rev 收敛，mem0 infer=False |
| observe | PersonalPolicy 默认模式 | 只整理+建议；不静默建系统闹钟，不外发 |
| Subscription（提醒确认） | `SaSubscription` + `POST /v1/reminders/{id}/confirm` | 直通原 WorkItem，不经 Eval；Panel 仍未做（Watchdog 已落地） |

## 当前链路

1. POST note 提交 → Source → Engine 原子建持久 Ticket → Runtime 轮询 → OrganizeWorker → Engine CAS report → Channel 写整理产物、建议提醒与 memory_sync。
2. PATCH 正文同事务清旧 title/tags/todos、作废 suggested、memory_sync 清旧记忆；提交后开新 rev 的整理 Ticket，旧结果受 rev 栅栏约束。
3. ask → Trigger/Ticket → 同请求 await AnswerAskWorker → Memory 检索/LLM → Engine/Channel → answer/citations。HTTP 形状保持。
4. 重启后从 Store 读取 pending/done；多进程靠条件 UPDATE 竞争，同一 Ticket 仅一个认领成功。notes 提交后 intake 缺口由轮询补扫（`runtime.repair`：Worker 进程每轮自带，独立 Watchdog 进程专责），不靠进程内 task 保证可靠性。

## 本刀替换记录

| 区域 | 删除/替换/保留 |
| --- | --- |
| 内存领域引擎 | 删除 dict、events 列表与旁路模型，替换为 SQLAlchemy Store + async Engine |
| notes 编排 | 删除生产 enqueue extract；POST/PATCH 提交后建 Ticket，数据库轮询驱动 |
| 整理逻辑 | 从 pipeline/extract 迁入 workers/extract_impl；Worker 计算，Engine/Channel 写回 |
| 历史 extract | pipeline/extract 仅 import alias，允许排空旧 Job 与保留租约回归；生产 Organize 不调用该入口 |
| ask | 从 API 直调工具改为 Ticket/AnswerWorker；同请求返回，错误继续走既有安全出口 |
| Memory 队列 | 保留 enqueue memory_sync 与既有补偿/租约/归属协议；不是 Router |
| 文档/测试 | 重写现状架构与 domain 测试；迁移生产断言到 Ticket；旧队列测试显式造历史行，PG 并发回归保留并新增 |

完整模块地图、锁序、恢复与 ask 请求中断边界、本地命令见 [architecture-superagent.md](architecture-superagent.md)。新增状态规则纳入 [server-invariants.md](server-invariants.md)，不降低现有门禁。

## 保留与未做

保留 FastAPI + SQLAlchemy + Postgres/pgvector（SQLite 单测）、Memory/LLM 端口、原生 Kotlin 客户端、原有 API 安全与同步协议。不开 auto-merge、不部署、不改 Android；提醒确认 Subscription + Channel actions 已落地；多 agent、电脑操控、Panel、Android AlarmManager 实装仍未做。目标骨架就是当前实现；没有“之后再持久化”的阶段安排。
