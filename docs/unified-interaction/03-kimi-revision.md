# Kimi 修订说明：对 Codex 设计评审的逐项处置

日期：2026-11-09  
评审输入：`02-codex-design-review.md`  
修订对象：`01-kimi-design.md`、`prototype.html`  
状态：仅设计契约与静态原型已修订；**生产实现尚未开始，没有把任何发现说成 App 代码已修复。**

## 范围决定

采纳 Codex 的完整首版收口建议，但保留统一交互的完整端到端体验：

- 保留：一个语音优先个人流、显式记下/提问、记事创建与整理、详情编辑删除、问答与来源、TTS、提醒确认/完成/取消/贪睡、全部提醒筛选、搜索、通知与 boot、无障碍。
- 网络：保留安全串行，不并行发送多个业务请求；等待时允许浏览和编辑下一输入，遇忙保留文字、不自动排队。
- 持久化：仅 ComposerDraft 与不可变 CreateRecovery；不做完整 Note/Reminder/Answer 业务缓存，不做持久问答历史。
- 通知点击保持打开 App，不做首版精确 reminder 路由。
- 服务端不变，仍无新聊天、任务进度或取消 endpoint。

这不是缩减成小功能补丁：首版仍必须完整交付统一 UI 及 note → organization → reminder confirmation → local scheduling 和 ask → sources 两条闭环。

---

## 1. P1 — 请求 owner / login epoch 必须在凭证使用前授权

**处置：采纳，重写为强制提交契约。**

### 设计修正

- 新增不可变 `RequestOwner = operationId + runtimeKey + userId + loginEpoch + processGeneration`。
- 所有 Repository 操作接收 owner；进入 session mutex 后，先比较不含 token 的 SessionDescriptor，匹配后才 `store.read()`。
- refresh token 使用前和使用刷新后的 access token 重试前都重新复核。
- 登出开始立即封闭新提交并 bump epoch/generation；同用户重新登录也不继承旧操作。
- 本地写入经统一 ProjectionCoordinator；登出清库和异步写有序，数据库事务带 owner+epoch+operationId+draftVersion 条件，不允许“检查后无条件写”。
- 首版最多一个 repository 网络操作在途，不删除 session mutex，不承诺零等待。遇忙只保留下一输入，不自动排队。

### 精确修改位置

- `01-kimi-design.md`：§0 第 5/7 项；§4.5；§7.2–7.4；§8 的全表前置约束；§9 App/Network；§10.1；§15.1 第 3/8 项；§17 A32–A35；§18 第 6/8 项；§19 第 9 项。
- `prototype.html`：状态页新增“会话归属”和“同步与网络串行”；asking 卡与输入坞明确不并行、不自动排队。

### 验收落地

A32–A35 覆盖旧 create/ask 等锁后换代、同用户再登录、refresh 中途换代、旧响应清库后到达和普通 refresh 不换登录。通过标准是旧请求**没有使用新代凭证发出 HTTP**，而不只是没有旧卡片。

---

## 2. P1 — 不确定创建必须保留不可变快照并安全对账

**处置：采纳，分离原始 A 与继续编辑 B。**

### 设计修正

- `local_only` 草稿与已发出的 `CreateRecovery` 不再是同一实体。
- 首次 POST 前持久化完整不可变 A：client_note_id、正文、recorded_at、timezone、source、asr；落盘失败不发送。
- sending/unknown/rejected 与“确定未发送”分开；网络中断、超时、5xx、进程死亡均是 unknown。
- 重启后持久 sending 一律转 unknown；手动对账/重试重新捕获当前 processGeneration，不直接沿用旧 RequestOwner。
- 恢复先 full sync/notes 按 client_note_id 找 live A；找到则不 POST。未找到时只能手动重试逐字段一致的 A。
- create 返回后必须 GET/sync 对账真实正文/status/rev；不按本地文字合成服务器成功卡，重放返回 organized/failed 也不降回 uploaded。
- A live + B 已编辑：保留 A 与 B，用户明确选择“用当前 rev 将 B PATCH 到 A”或“新 client_note_id 另存 B”。
- A tombstone/404：不复活、不清 B；只允许明确另存或移除恢复项。
- 移除 unknown 恢复项不表示服务器撤销，B 自动成为独立 local_only。
- Android Note DTO 增加服务端已有的 client_note_id；Mock 不作为唯一验收，因为它把 client_note_id 当 server id。

### 精确修改位置

- `01-kimi-design.md`：§1.2 创建契约；§4.3；§5.6；§7.4–7.5；§8 创建/对账/更新/另存各行；§9 Draft/Create 全组；§11.2–11.3；§15.1 第 4/8 项；§17 A07–A12、A40；§18 第 5/11 项。
- `prototype.html`：新增“创建结果未知”场景；初始同时展示不可变 A 与修改稿 B；“核对原记事”后展示权威 A 与仍保留的 B；提供更新/另存和“只移除本机恢复项”的独立确认层。

### 验收落地

A07–A12 覆盖 A 已保存丢 202、重启改 B、另一端修改/删除、sync 先找到、双击、落盘失败和本机移除。A40 要求 Real/MockWebServer 使用不同 server ID。

---

## 3. P1 — 服务端 confirmed 与本机实际排程必须分轴

**处置：采纳，删除所有把 scheduled 直接写成“已排程”的设计。**

### 设计修正

- 服务端轴保留 suggested/scheduled/done/cancelled；本机轴为 not_applicable/unknown/submitting/submitted_exact/submitted_inexact/failed/expired。
- `scheduled` 只显示“服务端已确认”。
- 只有实际 AlarmManager 调用返回且未抛错才显示“本机已提交系统排程（精确/非精确）”；不保证准点送达。
- prefs 写入、action 存在、scheduled 缓存均不是成功回执。
- action 缺失时保留响应 scheduled reminder fallback；不从 suggested 造 alarm。
- 本机失败只重试本地排程，不重复 confirm；refresh 失败不改写 mutation 结果。
- 到达时已过期标 expired，不设假成功；修改未来时间是新的明确 confirm 修改。
- POST 与 schedule 之间进程死亡，重启先 unknown，fresh sync 后本地恢复，不重新 POST。
- 通知运行时权限、通知频道、精确闹钟能力独立；通知权限改为首次本机排程提交成功后请求。

### 精确修改位置

- `01-kimi-design.md`：§0 第 8 项；§1.1 闹钟事实；§5.3–5.4；§6.4；§8 服务端确认/本机结果/本地重试行；§9 Reminder/Notification/Exact alarm 全组；§10.2；§11.1/11.3；§15.1 第 6 项；§17 A19–A28；§18 第 10 项；§19 第 3 项。
- `prototype.html`：确认 Sheet 改为两步说明；成功卡分别显示服务端、非精确本机提交和通知未开启；新增“本机排程异常”场景展示 unknown/failed/expired；状态页拆出服务端、排程、通知权限与频道。

### 验收落地

A20–A28 覆盖 scheduler/fallback 抛错、到达过期、refresh 失败、POST/schedule 间死亡、权限撤回竞态、通知/频道、boot 断网；禁止重复服务器 mutation 和假本机成功。

---

## 4. P2 — 固定听写与提交归属

**处置：采纳。**

- 每次 VoiceAttempt 固定 mode、owner epoch、attemptId、draftId/version。
- 产品决定：听写期间锁模式；任何模式切换取消当前尝试。Permission/Processing 同样适用。
- 每次提交快照化 draft version；A 回包不能消费后续 B。
- 修改位置：§4.2–4.4、§6.3、§10.1、§17 A05–A06、§18 第 4 项；原型听写层明确固定模式与切模式取消。

## 5. P2 — 异步答案的 TTS 仲裁

**处置：采纳。**

- 单一 AudioCoordinator；开始 ASR（含 Permission）停止 TTS/pending 并撤销在途 ask 自动播资格。
- Permission/Listening/Processing 时答案只保留文字；旧回答不抢占用户主动播报；旋转/恢复不自动播。
- 首版仅一个 ask 在途，仍处理 answer 与新 ASR、TTS 初始化、退后台的竞态。
- 修改位置：§4.2、§6.3、§10.1、§17 A17–A18；原型听写与状态页明确该行为。

## 6. P2 — 投影提交顺序与删除栅栏

**处置：采纳。**

- 所有 repository→projection 应用经单一 ProjectionCoordinator，refresh 保持完整串行和原子应用。
- refreshId 阻止旧全量响应覆盖新状态；rev 只负责旧对象，tombstone+refreshId 负责删除栅栏。
- 导航使用 navigationRequestId；同 alarm action.id 的 payload 更新必须重新消费，不永久按 ID 去重。
- 首版无持久业务缓存，但当前进程仍执行这些规则；离线失败不等于空列表。
- 修改位置：§5.2、§7.3/7.6、§17 A31/A36–A37、§18 第 7 项。

## 7. P2 — 提醒可达性与无障碍功能边界

**处置：采纳。**

- 个人流增加“提醒”筛选及待办/已结束/全部二级筛选；suggested/scheduled/done/cancelled 均可达，不新增任务中心。
- 无 note_id 提醒显示来源不可用，不渲染必然 409 的 mutation。
- 过去 suggested 必须改未来时间，409 不一律写“同步后重试”。
- divider 仅装饰；功能边界改用 control-border，焦点使用 focus。实际对比由 Compose/真机测试证明。
- 修改位置：§3.2–3.3、§5.3、§9、§12.1、§13、§17 A19/A38–A39；原型新增提醒筛选、无来源卡和功能边界 token。

---

## 修订后的阻塞结论与真实剩余风险

### 设计发现

- 本轮修订后，已知 **P0 = 0，P1 = 0，P2 全部采纳，无 defer**。
- 这是 Kimi 对修订稿的设计结论，不是 Codex 复审通过，也不是生产实现已完成。仍需 Codex 独立复核实际文件。

### 实现与产品风险（明确保留，不用文案掩盖）

1. **实现未开始**：RequestOwner、ProjectionCoordinator、CreateRecovery、LocalScheduleResult、AudioCoordinator 都是首版必须实现的客户端工作，当前代码没有这些完整能力。
2. **系统排程没有送达回执**：即使正确实现，也只能承诺“已提交系统排程”，不能承诺准点或通知已展示。
3. **ASR 依赖系统服务**：语音可用性、网络使用和识别质量不完全受 App 控制，文字路径必须始终保留。
4. **问答跨进程不可找回**：这是本次有意的首版限制；重启只能恢复问题草稿和“答案无法恢复”，不能提供持久聊天假象。
5. **无完整离线业务缓存**：冷启动离线无法阅读旧服务器记事/提醒，这是有意收口；当前进程已加载内容只能标为可能非最新。
6. **未知创建仍需谨慎实现**：client_note_id DTO 补充、完整快照落盘、live/tombstone 对账和 B 保留必须通过 Real API 契约测试，不能只靠 Mock。
7. **本地写与登出竞态**：Room 条件事务和统一写锁需要可控调度测试；单靠 UI epoch 检查不足。
8. **本阶段未运行构建、浏览器、设备或 TalkBack 测试**：原型只证明设计表达，不证明 Android 可实现性测试已通过。
