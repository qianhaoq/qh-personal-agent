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

改 `server/` 之前（含修 review 意见），先读完：[docs/server-invariants.md](docs/server-invariants.md)（服务端不变量，事实来源）和 [docs/agent-pitfalls.md](docs/agent-pitfalls.md)（反复踩的坑和正反例）。提交前逐条自检：

- [ ] 改动涉及并发（锁、条件更新、job、sync、记忆同步）时，配了能在 **PostgreSQL** 上跑的并发测试（`PG Tests` job），不能只在 SQLite 上过。
- [ ] **按类修，不按行修**：一个 review 发现修好后，用 `rg` 把同一类模式在整个 `server/` 里排查一遍，并在线程回复里写清排查了哪些地方。
- [ ] **同一类 P1 第二次出现时**，先更新 docs/server-invariants.md（必要时加上 `scripts/check-invariants.py` 的检查），再改代码。
- [ ] 本地跑 `python3 scripts/check-invariants.py` 通过；豁免必须写 `# invariant-ok: <理由>`。
- [ ] 单个 PR 改动超过 **约 800 行**时先拆分；评审轮次多，往往是因为一次改动的面太大。

## Review guidelines

Codex / 评审 bot 评审本仓 PR 时：

- 以 docs/server-invariants.md 为准检查 server 改动；发现违反时注明是哪一条不变量，并检查同一类模式在 diff 之外是否还有。
- 重点看 docs/agent-pitfalls.md 里列出的高发类别：先读后写竞态、加锁顺序 / allocate_seq 位置、job 收尾租约、提交后副作用、外部输入 NUL / 配置校验、静默回落假实现、多进程启动 DDL。
- 并发相关改动没有 PG 并发测试，按 P1 报。
- 豁免标记 `# invariant-ok:` 后面的理由站不住时，按违反不变量处理。

## 交接

- 工作交给下一个 bot 时，**单独私信**那个 bot，并标记**优先级**把它唤醒；不要只在群里 @ 或只留 PR 评论。消息里写清：仓库、PR 链接、head SHA、已完成什么、下一步要它做什么。
- 某个门禁变绿时（CI / Codex / QA / Acceptance），通知**需求方（个人助理开发）**和**专业测试**：PR 链接、head SHA、哪些门禁已绿。

## 密钥

- 密钥只放在 `/home/box/.coding-plans/`（以及服务端运行时的 `.env`），**绝不**写进仓库、PR、issue、日志或聊天。
- 仓库里只提交 `.env.example` 这类不含真值的模板；CI 的 Secret Scan（gitleaks）会拦截泄露。
