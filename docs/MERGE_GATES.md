# 合入门禁 | Merge gates

与 dota2.ai / pinhaoyun / ai-devflow-weekly 对齐：CI、Codex、QA 都调用 `qianhaoq/devflow-gates` 的 reusable workflow（固定到 commit `1a6a4a8`，即 1a6a4a85728b0c1f3482a52020f94cb8208c73f5；升级要单独开 PR）。

## 必需检查（branch protection）

- `ci / Build & Test` — `scripts/ci-test.sh`：`server/` 有 Python 代码时跑 ruff + pytest，`android/` 有 Gradle 工程时跑 `assembleDebug testDebugUnitTest`，都没有时直接通过（骨架阶段）。
- `ci / Secret Scan` — gitleaks 全历史扫描。
- `codex / Codex Review Gate` — 等 Codex 评审完成；**显式 P0/P1 与未标注线程阻断，显式 P2 不阻断**。PR 评论 `@codex review` 触发；线程 Resolve 后由每 15 分钟的 Gate Refresh 重跑。
- `qa / QA Gate` — 需要标签 `qa-passed`，且只认白名单用户（仓库变量 `QA_LABELERS`，默认 `qianhaoq`）最后一次打的标签；head 有新提交后旧标签会被摘掉。

分支保护还要求：分支与 main 同步（strict）、对话全部 Resolve、管理员同样受约束、禁止 force-push 和删除。

## 非必需

- `PG Tests (PostgreSQL 16, non-required)` — `ci.yml` 里单独的 `pg-tests` job：起 `postgres:16` 服务容器（带 `pg_isready` 健康检查），导出 `TEST_DATABASE_URL=postgresql+asyncpg://…`，让 `server/tests` 在真实 PG 上再跑一遍（`test_concurrency.py` 的行锁/条件更新只有 PG 才有意义）。`server/` 没有测试时直接跳过。devflow-gates 的 reusable CI 不支持 `services`，所以放在调用方；稳定后再决定是否设为必需检查。

- `acceptance / Acceptance Gate` — M1 仅观察：在 PR 置顶评论里报告测试/覆盖率、改源码须改测试（`no-test-needed` + 理由豁免）、PR 规模、依赖、验收小节、Codex 结论，不阻断。配置 `.github/acceptance.yml`。

## 自动合入

`auto-merge.yml` 在上述检查全绿后 squash 合入，以下情况不合：draft、标签 `no-auto-merge`、`skip-codex-gate`。

**Copilot**：配额已用完，`require_copilot` 默认关闭（仓库变量 `DISABLE_COPILOT_REVIEW` 未设置或不为 `false` 时不要求 Copilot review）；配额恢复后把变量设为 `false` 即可重新要求。仓库没有任何必需检查依赖 Copilot。

## 评审严重度

| 级别 | 含义 | 合入前 |
| --- | --- | --- |
| **P0** | 正确性 / 安全 / 数据丢失 | 必须改代码后 Resolve |
| **P1** | 实质缺陷或明显回归 | 必须改代码后 Resolve |
| **P2** | 风格、命名、小优化 | 可不改；回复 `P2: defer — <理由>` 后 Resolve |

原生“对话必须 Resolve”没有严重度例外：纯 P2 也要回复理由后 Resolve 才能合并。

## 标签

| 标签 | 用途 |
| --- | --- |
| `qa-passed` | QA 通过（白名单用户打） |
| `no-auto-merge` | 禁止自动合入，由人合并 |
| `skip-codex-gate` | Codex 逃生口（仅记录用途，门禁仍失败，需管理员处理） |
| `no-test-needed` | Acceptance：改源码不改测试的豁免，正文须写理由 |
