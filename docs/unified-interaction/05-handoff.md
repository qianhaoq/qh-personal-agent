# 第一阶段交接（协调者记录，非实现验收）

## 继续位置

- 仓库：qianhaoq/qh-personal-agent；main 基线 `3600c3ab4a1c499f099e39be6b8084b3351dd7b7`。
- 独立 worktree：`/workspace/qh-personal-agent-unified`；分支 `feat/voice-agent-unified-interaction`。
- 本阶段仅新增本目录研究/设计/原型/评审文件，尚未提交、push 或开 PR。没有生产代码修改、合并、部署、routine 或外发消息。
- 原 worktree `/workspace/qh-personal-agent` 仍干净，分支 `feat/s38-detail-confirm-alarm`，HEAD `5a4485e1a5f3f82f749b683f4adf452512762b6f`；旧 S39 worktree 亦未动。

## 一手依据与选定方案

来源见 00：DeepSeek 官方技术长文《Super-agent 设计探索：迈向更自主的 Agent 系统》及官方研究原型；没有核实 DOI/arXiv 正式论文身份。不可称已验证的学术论文，不照搬 coding bot。

选定 Kimi 设计：连续“个人流” + 统一底部语音/文字输入，明确“记下/提问”；记事、答案来源、提醒及待确认事项遵循同一视觉/状态语言。保留详情、编辑删除、搜索、完整提醒操作、通知/boot、无障碍；不迁移 iOS，不新增后台 agent/server 协议。

首个完整发布保持网络业务串行，忙时允许浏览/编辑下一份文字，不自动排队。仅持久化 ComposerDraft 和不可变 CreateRecovery，不做完整离线业务缓存或持久聊天记录。安全契约为 owner/epoch 认证前栅栏、A 请求/B 编辑稿隔离、服务端确认与本机 AlarmManager 提交分轴、投影栅栏及音频仲裁。

## 实际模型与证据

- Kimi 使用真实 `coding kimi --model k3.1-pro`（非默认 highspeed），探针、设计、修订均完成；init/modelUsage/canonicalModel 均报告 `k3.1-pro`。CLI 有 unrecognized_model 提示但执行成功；这是实际 CLI 元数据，不宣称独立证明供应商内部路由。
- Kimi 设计/修订会话：`c5428a31-aaef-4b2a-9ef1-ca832da18257`；01、03、prototype.html 是其实际工具写入。
- Codex 使用真实 box `codex exec --skip-git-repo-check`，模型 `gpt-6.1-sol`；02 与 04 是独立实际代码/设计评审，不是协调者代写。
- 全部调用日志：`/workspace/qh-unified-evidence/` 下 `kimi-k31-model-probe.json`、`kimi-design.stream.jsonl`、`kimi-revision.stream.jsonl`、`codex-design-review.log`、`codex-design-rereview.log`。
- 模型元数据摘要：同目录 `model-verification.json`；一手资料 URL/SHA256：`primary-sources/manifest.json`；初稿：`initial-design/`。

## 独立评审结论与未决 P2

04 结论：**没有剩余 DESIGN P0/P1，可进入实现；不等于实现安全、QA 或原型行为通过。** 原三项 P1 已在规范消除：旧操作等 session mutex 后不得使用新凭证；unknown 创建不可变 A/可编辑 B 的幂等与对账；scheduled 不等于真实本机排程。

原型仍有四类 P2，必须留痕，不得当作已验收：
1. 忙碌/离线/恢复 A/B 的 handler 是预设示例，不完整兑现输入保留或串行约束。
2. 提醒编辑值未进入动态摘要/结果，完成/取消/贪睡操作演示不足。
3. 焦点/Escape/快捷键/对话框背景隔离、音频仲裁和部分触达尺寸尚未实现/实测。
4. scenarios 数组索引 meta[4]/meta[5] 错位，可能显示 undefined。

这些不能直接移植到产品；若成为真实逻辑，将重新形成 P1。后续可由 Kimi完善原型或在实际实现中严格按规范修正，并在反向 review 验证。

## 已执行验证及限制

- `python3 scripts/check-invariants.py`：`0 error(s), 0 warning(s)`。
- `git diff --check`：通过（本阶段新文件未提交，不能将其解释为全部新增文档的功能测试）。
- 原型 HTML 静态解析、唯一静态 ID、内嵌脚本 `node --check`：通过；没有外部资源、Canvas、网络调用模式。记录 `static-validation/result.json`。按钮由 JS 生成，静态解析 0 个 button 不代表实际无按钮。
- Codex 04 的三文件 hash 已由协调者重算一致。
- **未渲染浏览器/截图、未跑 Android build/unit/Compose/TalkBack/设备测试；没有应用实现或可安装改版；尚无 PR。** 原型仅示例数据，不是真实后端/权限/持久化演练。

## 下一阶段执行顺序

1. 先读 AGENTS.md、00–04、真实当前代码；确认分支基线未漂移。用户本轮明确只到 PR，禁止合并/部署，即使历史文档允许自动合入。
2. 由 box Codex 实现完整统一 Android 发布单元；不能只交设计或换皮，也不能照搬原型不安全 handler。先列可验证内部单元，考虑 AGENTS 约 800 行生产改动的拆分要求；若整体实现与拆分要求冲突，应向主 bot 说明计划，不静默扩成巨型 PR。
3. 对 04 中 session mutex/条件落盘、unknown create 真 API 不同 ID、提醒故障注入、投影/通知/boot、ASR/TTS/草稿及功能完整性逐项写真实测试；既有回归必须保留。
4. Kimi 对实际代码和真实 UI 反向 review，不把本次静态原型 review 冒充代码/UI review。所有实际 P0/P1 必修。
5. 完整 build/unit/invariants，按环境能做的 Compose/可视/设备测试给证据，明确设备未测项。开一个明确等待用户检查的 PR 后停止；不恢复 routine、不外发通知、不部署。
