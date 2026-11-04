# qh-personal-agent

个人语音记事与语音助手。说一句话，自动整理成笔记 / 待办 / 提醒，并能用语音问答自己的记忆。

当前 Android 里程碑：**S23**，提醒主列表在隐藏已结束提醒后为空时显示「暂无进行中提醒。可开启「显示已结束」查看。」；没有任何提醒时仍显示「暂无提醒。」。仅调整空状态文案与纯函数，保留 S22 筛选规则和开关行为。详见 [Android README](android/README.md)。

S23 不做：FCM/Room/Hilt/Pipecat、rrule、根路径 `GET /`、二次确认、data / Mock / API / server 改动、ASR/TTS 改动、routine；不改筛选规则、FilterChip、通知按钮集合或 API 语义，不部署、不合入。

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
