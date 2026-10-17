# 评审指引（Copilot / Codex 等 AI 评审通用）

## 评审分级

- **P0 / P1 必须在合并前修复；P2 可选，不阻塞合并**（不改须回复 `P2: defer — <理由>` 后 Resolve）。
  - P0：安全漏洞（鉴权绕过、越权读取他人笔记）、密钥泄露、数据丢失 / 损坏、生产事故。
  - P1：CI 红灯；功能缺陷或明显回归；并发敏感改动（锁、条件更新、job、sync）缺 PostgreSQL 并发测试；隐私边界问题（录音 / 原文未加密、日志打出用户内容）；接口与 `docs/tech-arch-v1.md` 约定不一致且未说明。
  - P2：命名、样式、重构、性能与其他测试建议。

## 本项目红线

- 用户的录音、原文和记忆属于敏感数据：只在用户自己的命名空间读写；日志里不出现原文和 token。
- 客户端 token 存 EncryptedSharedPreferences，`allowBackup=false`。
- LLM / 第三方密钥只从环境变量读取，仓库只放 `.env.example`。

## 服务端不变量

- server 改动以 `docs/server-invariants.md`（INV-1..INV-10，随 PR #2 落地）为准评审，高发类别见 `docs/agent-pitfalls.md`。违反时注明 INV 编号，并检查同类模式在 diff 之外是否也存在。
- `scripts/check-invariants.py` 是 best-effort 启发式：漏检按 P2 defer，误报或崩溃才算 P1。

## 提交前自检

```bash
bash scripts/ci-test.sh
python3 scripts/check-invariants.py
```

## Merge gates

`main` 受保护：`ci / Build & Test`、`ci / Secret Scan`、`codex / Codex Review Gate`、`qa / QA Gate` 全绿，且对话全部 Resolve 后由需求方合并。见 docs/MERGE_GATES.md。
