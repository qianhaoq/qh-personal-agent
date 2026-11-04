# qh-personal-agent

个人语音记事与语音助手。说一句话，自动整理成笔记 / 待办 / 提醒，并能用语音问答自己的记忆。

当前 Android 里程碑：**S20**，提醒主列表与记事详情的 suggested/scheduled 提醒支持「10分钟后」，与「完成 / 取消」并列；成功后重设本地闹钟并刷新，失败保留闹钟并显示可读错误。复用 S19 data / Mock / API，保留通知快捷操作。详见 [Android README](android/README.md)。

S20 不做：rrule 执行、二次确认、FCM/Room/Hilt/Pipecat、ASR/TTS 核心改动、根路径 `GET /`；不部署、不改通知栏。

## 仓库结构（monorepo）

| 目录 | 内容 |
| --- | --- |
| `android/` | Android 客户端（Kotlin）：录音、语音识别、离线队列、提醒 |
| `server/` | 服务端（FastAPI）：鉴权、笔记整理（LLM）、记忆检索、问答 |
| `docs/` | 设计文档：调研、产品原型、技术架构、合并门禁 |
| `scripts/` | CI 脚本（`ci-test.sh`：按目录是否存在跑 server / android 检查） |
| `.github/` | CI 与门禁（复用 `qianhaoq/devflow-gates`）、验收配置、协作指引 |

## 设计文档

- [调研](docs/research-v1.md)
- [产品原型 v1](docs/product-prototype-v1.md) · [v2](docs/product-prototype-v2.md)
- [设计 v1](docs/design-v1.md) · [v2](docs/design-v2.md)
- [技术架构](docs/tech-arch-v1.md) · [AI-native 架构](docs/ai-native-arch.md)
- [Super-agent 概念映射](docs/superagent-concept-mapping.md) · [Super-agent 架构](docs/architecture-superagent.md)（持久化 Store / Engine / Workers / Channel；替代 #23/#25，notes 与 ask 已接入）
- [合并门禁](docs/MERGE_GATES.md)

## 开发与合并

- 协作规则见 [AGENTS.md](AGENTS.md)。
- 所有改动走 PR；`main` 受分支保护，必需检查：`ci / Build & Test`、`ci / Secret Scan`、`codex / Codex Review Gate`、`qa / QA Gate`。
- 密钥只放在本地 `.env` / 机器上的密钥目录，**不进仓库**。
