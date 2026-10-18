# AGENTS.md — qh-personal-agent

个人语音记事与语音助手的 monorepo（`android/` Kotlin 客户端、`server/` FastAPI、`docs/` 设计文档）。本文件约束所有参与本仓的 bot 与人。

## 角色

| 角色 | 负责 |
| --- | --- |
| **个人助理开发** | 需求、优先级、PR 验收与**合并** |
| **专业研发** | CI、门禁（devflow-gates）、分支保护、仓库设置 |
| **专业测试** | QA 验收；通过后打 `qa-passed` |

## 写代码

- 代码改动默认交给本地 **`coding glm`**，模型用用户指定的 **GLM 5.3**。
- 前端 / UI（Android 界面、交互、样式）交给 **`coding kimi`**。
- 每个改动一个分支、一个 PR；不直接推 `main`（受保护）。
- 改服务端前先读 [docs/server-invariants.md](docs/server-invariants.md)，改动不得违反其中不变量；新增状态变更需同步更新该文档与对应测试。

## 评审与修复

- 评审和修复都在 PR 里完成：评审意见写成 PR review 线程，修复推到同一分支，在线程里回复后 Resolve。
- **P0 / P1 必须在合并前修复**（改代码后再 Resolve）。
- **P2 可以不改**，但必须在线程里写明理由后 Resolve：`P2: defer — <理由>`。
- 触发 Codex 评审：在 PR 评论 `@codex review`。

## 合并

- 必需检查：`ci / Build & Test`、`ci / Secret Scan`、`codex / Codex Review Gate`、`qa / QA Gate`；未解决的对话会挡住合并。
- `acceptance / Acceptance Gate` 在 M1 只报告（observe），不阻断。
- 由 **个人助理开发** 合并；需要人工把关的 PR 打 `no-auto-merge`。
- 不得绕过分支保护（不用管理员合并、不关保护再开）。
- 详见 [docs/MERGE_GATES.md](docs/MERGE_GATES.md)。

## 改 server 前必读 / 自检清单

改 `server/` 之前（含修 review 意见），先读完以下两份：
- [docs/server-invariants.md](docs/server-invariants.md)：服务端不变量 INV-1..INV-12，事实来源（随 PR #2 落地）；冲突时以它为准。
- [docs/agent-pitfalls.md](docs/agent-pitfalls.md)：反复踩的坑和正反例。

关键不变量速记（只是索引，细节与例外以 docs/server-invariants.md 对应条目为准）：

- **INV-1 锁序**：invite → user → device → refresh_tokens → note → reminders → jobs；业务行（含 job INSERT）显式 flush 后，`allocate_seq` 必须是提交前最后一把锁；事务内不调 LLM / mem0 / 外部网络。
- **INV-6 租约收尾**：收尾带租约条件（`status='running' AND locked_by AND locked_at`）；丢租约的 worker 只删自己写的 memory id 并补偿再入队 memory_sync，**不得静默 return**（INV-5 第 3 步）。
- **INV-7 输入硬化**：外部输入含 `\x00` 一律 422；病态输入（surrogate、超深 JSON、超大整数、越界时间）一律 4xx 不 500；时间（含 LLM 输出的 fire_at）统一用 `utc_safe_datetime` 的 ±14h 安全界。
- **INV-8 登录/refresh**：device 单条原子 upsert；refresh token 存 `refresh_tokens` 表、每设备 ≤5 枚；轮换只作废被轮换的那一枚，重放已作废的 token 才吊销整台设备。
- **INV-10 配置校验**：启动即校验，缺配置直接报错；数值配置（TTL/超时/批量/重试/上限）必须为正；生产（postgresql）不得隐式退化到 mock，`MEMORY_BACKEND ∈ {inmemory, disabled}`，生产后端 mem0 随 feat/mem0-backend（#6）落地。
- **INV-11 乐观锁 / rev**：rev 比较在任何提前返回之前、在锁或条件更新里原子完成；内容不变的快路径也要先校验 rev，过期 rev 一律 409。
- **INV-12 0 行归因**：条件 UPDATE/DELETE 匹配 0 行后必须重读当前状态再定原因（或在注释里写因果封闭论证），不得猜成重放 / 冲突 / 已删除。
- **INV-13 锁内重算**：等锁之前算好的派生值（now、状态、计数、rev、expires_at）不得在拿到锁之后沿用；锁内重取、条件 UPDATE 对行当前值原子判定、或版本栅栏守护。
- **INV-14 异常不落原文**：日志 / `jobs.last_error` / 错误响应只记异常类型名 + 错误码（`app/errors.py::safe_error_summary`），不记消息文本与 traceback；422 不回显 input/ctx；静态检查 INV-NO-RAW-EXC。
- **INV-15 源字段变更失效派生**：改 `note.text` 必须同事务清 title/tags/todos、置 `status='uploaded'`、作废 suggested reminders、入队 extract + memory_sync；源→派生映射表见文档。

提交前逐条自检：

- [ ] 并发敏感的改动（锁、条件更新、job、sync）配了能在 **PostgreSQL** 上跑的并发测试（`PG Tests` job），缺失按 P1。
- [ ] **按类修，不按行修**：一个 review 发现修好后，用 `rg` 把同一类模式在整个 `server/` 里排查一遍，并在线程回复里写清排查了哪些地方。
- [ ] **同一类 P1 第二次出现时**，先更新 docs/server-invariants.md，再改代码。
- [ ] 本地跑 `python3 scripts/check-invariants.py`。豁免只有一种写法：`# invariant-ok: <RULE-ID> <理由>`。
- [ ] 单个 PR 的代码改动超过 **约 800 行**（不计 docs、tests、生成文件）时先拆分。

## Review guidelines

Codex / 评审 bot 评审本仓 PR 时：

- 以 docs/server-invariants.md（INV-1..INV-12，随 #2 落地）为准检查 server 改动。发现违反时注明 INV 编号，并检查同一类模式在 diff 之外是否还有。
- 重点看 docs/agent-pitfalls.md 的高发类别。
- 分级口径与 .github/copilot-instructions.md 一致：
  - CI 红灯为 P1（造成生产事故时才算 P0）；
  - 并发敏感的改动缺 PG 并发测试为 P1。
- `scripts/check-invariants.py` 是 **best-effort 启发式，精确优先于召回**。
  - 漏检（控制流、别名、依赖注入参数等）不算 P0/P1，按 `P2: defer — heuristic checker; PG concurrency tests + human/Codex review are the backstop` Resolve。
  - 只有误报或检查器崩溃，才算针对检查器的 P1。
  - 不要为补全覆盖面继续扩 AST 规则。

## 交接

- 工作交给下一个 bot 时，**单独私信**那个 bot，并标记**优先级**把它唤醒；不要只在群里 @ 或只留 PR 评论。消息里写清：仓库、PR 链接、head SHA、已完成什么、下一步要它做什么。
- 某个门禁变绿时（CI / Codex / QA / Acceptance），通知**需求方（个人助理开发）**和**专业测试**：PR 链接、head SHA、哪些门禁已绿。

## 密钥

- 密钥只放在 `/home/box/.coding-plans/`（以及服务端运行时的 `.env`），**绝不**写进仓库、PR、issue、日志或聊天。
- 仓库里只提交 `.env.example` 这类不含真值的模板；CI 的 Secret Scan（gitleaks）会拦截泄露。
