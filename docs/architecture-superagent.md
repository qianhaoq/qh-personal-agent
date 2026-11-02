# Super-agent 架构（S2：Ticket 驱动整理）

> 状态：S2 B 刀收双轨（分支 `feat/superagent-rewrite-s2`）。概念与术语以
> [superagent-concept-mapping.md](superagent-concept-mapping.md) 为准，本文只讲
> **落地形态**：数据流、模块边界、本地跑法、已知限制。

## 1. 数据流（Ticket → Worker → pipeline）

`/v1/notes` 契约不变；整理由领域 Ticket 驱动，不再经 jobs `extract`：

```text
POST /v1/notes 或 PATCH /v1/notes/{id}（正文变更）
 ├─ 写事务：note（+ PATCH 时作废 reminders）→ 可选 enqueue("memory_sync")
 │         → allocate_seq → commit
 └─ commit 之后（同 API 进程）：
      submit_note_uploaded()
        → HttpApiSource.note_uploaded：Trigger(NoteUploaded, dedupe_key=note_id:rev)
        → Engine.receive：
            幂等去重 → Thread(note:{id}) → PersonalPolicy.evaluate
            → accept ⇒ Mandate(v0) ⇒ WorkItem(organize_note, pending)
      schedule_organize(work_item)   # asyncio.create_task，不在事务内跑 LLM
        → OrganizeNoteWorker.run
            → engine.mark_running（CAS pending→running + worker_id）
            → app.pipeline.extract.extract_note(...)
            → engine.complete / fail
            （extract 成功写回时仍同事务 enqueue memory_sync）
```

**jobs 队列**：notes 路径不再 `enqueue("extract")`。生产上 jobs 主要剩
`memory_sync`（PATCH/DELETE 入口清旧记忆；extract 成功后再写新 rev）。
`app/jobs/*` 与 `pipeline/extract` 的 extract 入队能力**保留**（兼容/测试
可手动入队），但不是 notes API 的执行通道。

## 2. 模块边界

| 包 | 职责 | 禁止 |
| --- | --- | --- |
| `app/domain/` | 纯领域：模型 + `Engine.receive`（唯一写入口，内存态）+ Worker 协议 | 不碰 DB/LLM/网络；不 import api/pipeline |
| `app/policy/` | 唯一准入来源：v0 `PersonalPolicy`（`accept_mode=observe`、被动/主动动作面） | 不含执行逻辑 |
| `app/sources/` | 入口适配：`HttpApiSource` / `VoiceIngress` + registry；Trigger 构造与幂等键 | 不做业务执行 |
| `app/workers/` | WorkItem 执行方：`organize` facade + 进程内 `dispatch.schedule_organize` | 不直出信道；结果回 Engine |
| `app/api/` | 薄适配：HTTP → sources 钩子 / 调度 / 查询 | 不在事务内调 LLM（INV-1.4） |

边界规则（沿用映射 §3）：`api/*` 只 `engine.receive`/读模型 + 调度 Worker；
`policy/*` 是唯一准入与主动强度来源；`workers/*` 只 `report` 回 Engine；
`memory/*` 是 Worker 的工具端口。

## 3. 与现网契约/不变量的关系

- `/v1/notes` 的 202、幂等字段（`note_id/status/rev`）完全不变；幂等重放与
  IntegrityError 分支**不触发**领域钩子（只有成功创建/正文 PATCH 路径调用）。
- 钩子在 `session.commit()` **之后**调用：开单为纯内存；Worker 经
  `create_task` 异步跑，不进入 INV-1 的锁序故事；`submit_note_uploaded`
  吞掉一切异常（`safe_error_text`，INV-14），绝不把 202/200 变 500。
- `app/pipeline/*`、`app/jobs/*` 不删；`scripts/check-invariants.py` 仍适用
  （notes 路径不再产生 extract 行，SQL 锁序断言以 memory_sync 为准）。

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
  Ticket **不可见**，重启即空。可观测性目前只能进程内查询（测试与将来 ops 页）。
- 同进程 `schedule_organize` 依赖 API 进程事件循环；独立 jobs worker 进程
  **看不到**这些 Ticket（跨进程 Ticket / WorkItem PG 属后续刀）。
- `VoiceIngress` 是占位：S1 语音仍经端侧转写走 `POST /v1/notes`（即
  HttpApiSource 链路），端侧直连留后续刀。
- `accept_mode=observe`：只整理与「建议」提醒，不自动建系统闹钟、不外发。
