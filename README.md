# qh-personal-agent

个人语音记事与语音助手。说一句话，自动整理成笔记 / 待办 / 提醒，并能用语音问答自己的记忆。

当前里程碑：**S38**，记事详情「关联提醒」对 suggested 提醒补齐「确认并设闹钟」，与主列表共用严格状态判定和 `model.beginConfirm` 流程。应用标签「随记 S38」、顶部标题「随记 · S38」。详见 [Android README](android/README.md)。

选刀理由（已确认）：扫描 #57–#64，无开放 P0/P1；竞态、确认页、空态、确认标题、通知顺序与标题层级已由 S31–S37 闭环。本刀只补详情确认入口与主列表的一致性。

S38 不做：rrule、根路径 `GET /`、FCM/Room/Hilt/Pipecat、server/data/Mock/API、贪睡分钟调整、S31 竞态逻辑、通知栏、大重构、部署、合入、squash；不改确认对话框文案、筛选空态、通知标题或顺序。

## 仓库结构（monorepo）

| 目录 | 内容 |
| --- | --- |
| [`ios/`](ios/README.md) | SwiftUI 本机验证壳（登录、笔记列表、文本提问），非完整 Android 移植 |
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
