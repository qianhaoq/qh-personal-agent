# Super-agent 架构（第一刀）

> 状态：落地第一刀（分支 `feat/superagent-rewrite-s1`）。概念与术语以
> [superagent-concept-mapping.md](superagent-concept-mapping.md) 为准，本文只讲
> **落地形态**：数据流、模块边界、本地跑法、已知限制。

## 1. 数据流（第一刀：双路径并行）

现有 S1 契约不动，领域骨架作为**旁路**并行走：

```text
POST /v1/notes（202，契约不变）
 ├─ 现网路径（保持唯一执行通道，不双跑）：
 │    enqueue("extract") → jobs 队列 → app.pipeline.extract（LLM 整理 + memory_sync）
 └─ 领域旁路（新增，纯内存同步，commit 之后调用）：
      submit_note_uploaded()
        → HttpApiSource.note_uploaded：构造 Trigger(NoteUploaded, dedupe_key=note_id:rev)
        → Engine.receive：
            幂等去重（dedupe_key）→ 记 Trigger/Thread(note:{id})
            → Eval 快路径 = PersonalPolicy.evaluate（规则，无 LLM）
            → accept ⇒ 签 Mandate(v0, 被动动作面) ⇒ 开 WorkItem(organize_note, pending)
```

WorkItem 的执行方（Assignment）是 `app/workers/organize.py` 的 facade：

```text
OrganizeNoteWorker.run(work_item)
  → engine.mark_running(...)
  → app.pipeline.extract.extract_note(note_id, enqueued_rev)   # 复用现网 pipeline
  → engine.complete / engine.fail（结果与安全摘要记进 WorkItem + DomainEvent）
```

**第一刀里生产 notes 路径不调用这个 facade**（extract 已由 jobs 队列驱动，
Ticket 不会触发第二次 enqueue）；facade 由单测驱动，验证「领域骨架能驱动
pipeline」。等 Engine 路径稳定（PR-6）再收口为唯一执行入口。

## 2. 模块边界

| 包 | 职责 | 禁止 |
| --- | --- | --- |
| `app/domain/` | 纯领域：模型 + `Engine.receive`（唯一写入口，内存态）+ Worker 协议 | 不碰 DB/LLM/网络；不 import api/pipeline |
| `app/policy/` | 唯一准入来源：v0 `PersonalPolicy`（`accept_mode=observe`、被动/主动动作面） | 不含执行逻辑 |
| `app/sources/` | 入口适配：`HttpApiSource`（notes API）/ `VoiceIngress`（Android 语音占位）+ registry；负责 Trigger 构造与幂等键 | 不做业务执行 |
| `app/workers/` | WorkItem 执行方：organize facade 调现有 pipeline | 不直出信道（不推通知/UI），结果回 Engine |
| `app/api/` | 薄适配：HTTP → sources 钩子 / 查询 | 不在事务内调 LLM（server-invariants INV-1.4） |

边界规则（沿用映射 §3）：`api/*` 只 `engine.receive`/读模型；`policy/*` 是唯一
准入与主动强度来源；`workers/*` 只 `report` 回 Engine；`memory/*` 是 Worker 的
工具端口。

## 3. 与现网契约/不变量的关系

- `/v1/notes` 的 202、幂等字段（`note_id/status/rev`）完全不变；幂等重放与
  IntegrityError 分支**不触发**领域钩子（只有成功创建路径调用）。
- 钩子在 `session.commit()` **之后**调用：纯内存、同步、无锁、无外部服务，
  不进入 INV-1 的锁序故事；`submit_note_uploaded` 吞掉一切异常（只记
  `safe_error_text` 安全摘要，INV-14），绝不把 202 变 500。
- 现有 `enqueue("extract")` 保留，engine 旁路**不再 enqueue**（不双跑）。
- `app/pipeline/*`、`app/jobs/*` 一行未删；`scripts/check-invariants.py` 全绿。

## 4. 本地跑法（零外部密钥）

```bash
cd server
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env          # 默认 LLM_PROVIDER=mock、MEMORY_BACKEND=inmemory、SQLite
.venv/bin/python -m pytest    # 全量；领域骨架：.venv/bin/python -m pytest tests/test_domain_engine.py
cd ..
python3 scripts/check-invariants.py
```

密钥只放本地 `.env`（模板 `server/.env.example`），不进仓。

## 5. 已知限制（不构成承诺）

- Engine 是**进程内内存态**：多进程（uvicorn 多 worker / 独立 job worker）间
  不可见，重启即空。可观测性目前只能进程内查询（测试与将来 ops 页用）。
- 生产上 Ticket 停在 `pending`：真正执行仍由 jobs 队列完成，WorkItem 状态与
  job 状态**暂不同步**（第一刀刻意不双跑的代价；PR-5/6 的 Effects + PG 持久化
  收口）。
- `VoiceIngress` 是占位：S1 语音仍经端侧转写走 `POST /v1/notes`（即
  HttpApiSource 链路），端侧直连留后续刀。
- `accept_mode=observe`：只整理与「建议」提醒，不自动建系统闹钟、不外发。
