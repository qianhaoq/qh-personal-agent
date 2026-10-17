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
- 先读后写的状态变更（乐观锁、token 轮换、计数、状态机）必须锁行（SELECT … FOR UPDATE）或用条件更新（UPDATE … WHERE 旧值），并配并发测试。
- 数据库写事务的加锁顺序全局固定：**先锁业务行**（条件 UPDATE / FOR UPDATE，业务行之间按 note → reminders → device … 的顺序），全部写完后再调用 `allocate_seq`（server/app/common.py，须紧贴提交），change_seq 用其后的第二条 UPDATE 写入；绝不允许先 `allocate_seq` 再去锁业务行——PG 上会与其他路径构成 AB-BA 死锁（PATCH 与 organize 写回曾因此死锁）。不得依赖 ORM autoflush 隐式落序：业务行写完显式 flush 后才分配 seq。
- 事务边界里不得有 LLM / mem0 / 网络调用；鉴权等只读依赖用**独立短会话**完成即还连接（server/app/deps.py），不得把请求级会话/事务带进秒级外部调用。
- 字符串入参一律拒绝 NUL（\x00）：schemas 层统一用 `NoNulStr`/`reject_nul`（server/app/schemas.py）校验成 422，而不是等 PG 编码错误变 500。
- 异步 job 的收尾必须带租约条件（`WHERE status='running' AND locked_by=:me AND locked_at=:claimed`，见 server/app/jobs/queue.py 的 `finalize_claim`）：任务被回收换主后旧 worker 的收尾一律放弃。
- 生产（postgresql）默认值要安全：LLM/记忆后端配错启动即拒（不静默回落假实现）、`ENABLE_DOCS=false`、CORS 默认不启用、容器非 root、运行时镜像不装测试依赖（见 server/Dockerfile、requirements-app.txt）。

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

## 交接

- 工作交给下一个 bot 时，**单独私信**那个 bot，并标记**优先级**把它唤醒；不要只在群里 @ 或只留 PR 评论。消息里写清：仓库、PR 链接、head SHA、已完成什么、下一步要它做什么。
- 某个门禁变绿时（CI / Codex / QA / Acceptance），通知**需求方（个人助理开发）**和**专业测试**：PR 链接、head SHA、哪些门禁已绿。

## 密钥

- 密钥只放在 `/home/box/.coding-plans/`（以及服务端运行时的 `.env`），**绝不**写进仓库、PR、issue、日志或聊天。
- 仓库里只提交 `.env.example` 这类不含真值的模板；CI 的 Secret Scan（gitleaks）会拦截泄露。
