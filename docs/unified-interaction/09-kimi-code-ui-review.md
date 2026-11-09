# Kimi 对真实 Android 实现的独立反向评审

日期：2026-11-08  
评审对象：`feat/voice-agent-unified-interaction` 工作树中的**未提交真实实现**，不是仅评审 HTML 原型。  
基线快照：`3600c3ab4a1c499f099e39be6b8084b3351dd7b7`。  
模型调用：显式 `--model k3.1-pro`；本报告不推断供应商内部路由或实现细节。

## 0. 结论与证据边界

**结论：当前实现不通过最终发布评审。P0 = 0，P1 = 5，P2 = 8。**

- 其中 3 项 P1 是真实 correctness 失败：登出封闭顺序、`scheduled` 仍显示“已排程”、恢复 A 的 404/409 后状态未闭环。
- 其中 2 项 P1 是已批准设计的硬要求缺口：B 无法在保留已核对 A 时另存新记事；大量真实 Compose 操作目标未落实 48 dp。
- 这些 P0/P1 必须由 Codex 修复并补充对应真实测试后，才能发布最终 PR；不能用 07 的辅助编译或 JVM 测试结论抵消。
- 统一流、显式“记下/提问”、串行 repository、不可变 A/独立 B、当前进程答案、手动-only TTS、服务端/本机排程分轴的主体实现方向可以保留，不需要重做架构或扩大服务端范围。

**这是代码级 UI / Compose 布局、状态与语义审查，不是渲染或设备验收。** 本次没有连接 emulator/device，没有看到任何应用 UI 截图或浏览器渲染，没有点击真实 App，没有听到 ASR/TTS，也没有完成硬件、权限、TalkBack 或无障碍 QA。下文的“复现”是从真实源码可推导的操作步骤，不表示我已在设备执行。

协调者正在另行运行完整 Gradle build；本报告不预判其结果。07 中 `.verification` 的辅助编译 stub 了 Room，不是 Android build；07 自述的测试数字也不是本次独立重跑结果。本报告没有执行 build、测试脚本或外部命令。

## 1. 审查快照与范围

按允许工具，本次以完整基线 SHA + 当前未提交文件的逐行源码快照定位，不伪称取得了当前工作文件 SHA-256。行号对应本次 `Read` 返回；后续改动后必须重新审查，不能沿用行号或结论。

### 设计与原型

完整读取：`AGENTS.md`、`docs/unified-interaction/00-primary-sources.md`、`01-kimi-design.md`、`02-codex-design-review.md`、`03-kimi-revision.md`、`04-codex-design-rereview.md`、`05-handoff.md`、`06-codex-implementation-plan.md`、`07-codex-implementation-report.md`、`prototype.html`。

04 中记录的是当时设计文件 hash，不是本次 Android 生产文件 hash；没有拿它冒充代码快照。

### 实际生产代码

- app：`MainActivity.kt`、`UnifiedComposer.kt`、`FlowReminderCard.kt`、`SystemStatus.kt`、`ComposerDatabase.kt`、`NoteProjection.kt`、`VoiceAttempt.kt`、`LocalScheduleResult.kt`、`AnswerItem.kt`、`AppRepositories.kt`、`ReminderAlarms.kt`、`ReminderNotificationAction.kt`、`ReminderNotificationConfirmActivity.kt`。
- 相关既有集成代码：`VoiceAsr.kt`、`AnswerSpeaker.kt`、`AnswerTts.kt`、`ConfirmOverride.kt`、`DisplayTime.kt`、`ReminderStatus.kt`。
- data/security：`Api.kt`、`Repository.kt`、`RequestOwner.kt`、`CreateRecovery.kt`、`SecureTokens.kt`。
- 配置与迁移边界：`android/app/build.gradle.kts`、`AndroidManifest.xml`、`.gitignore`、Compose 依赖目录。

### 实际测试源码

重点读取：`RequestOwnerTest.kt`、`CreateRecoveryTest.kt`、`RepositoryTest.kt`、`LocalScheduleResultTest.kt`、`NoteProjectionTest.kt`、`VoiceAttemptTest.kt`、`ReminderNotificationActionTest.kt`、`AppRepositoriesTest.kt`、`ReminderStatusTest.kt`、`ReminderListFilterTest.kt`、`AsrStateTest.kt`、`AnswerReplayTest.kt`、`AnswerTtsTest.kt`、`ConfirmOverrideTest.kt`。

未找到本次实现对应的 Compose semantics/UI instrumentation 测试，也未找到 `AgentModel` 的恢复 404/409、B 另存或登出进程死亡顺序测试。

---

## 2. P1 — 真实 correctness 失败

### P1-1：登出先清本地数据，后持久封闭会话；中间进程死亡会重新开放旧会话

**代码：**

- `android/app/src/main/kotlin/dev/qh/agent/MainActivity.kt:489-499`
- `android/data/src/main/kotlin/dev/qh/data/Repository.kt:152-168`
- `android/core-security/src/main/kotlin/dev/qh/security/SecureTokens.kt:16-24`

`AgentModel.logout()` 的顺序是：

1. `composerStore.clear()`；
2. `signedIn = false`、取消整理、`alarms.clear()`；
3. 到 `:497` 才调用 `repo.logout(owner)`。

而持久 `bumpLoginEpoch()` 与 `closeSession()` 只在进入 `RealRepository.logout()` 后的 `Repository.kt:153-158` 发生。因此 07 所说“logout closing marker survives restart”只覆盖**已经进入 repository logout 后、等待 mutex 前**的窗口，没有覆盖 model 的前置清理窗口。

**静态复现：**

1. 已登录并存在旧 token、草稿和 pending alarm。
2. 点击“退出”。
3. 在 `MainActivity.kt:492-496` 的本地清理之后、`repo.logout()` 首次执行前终止进程。
4. 下一次创建 `RealRepository` 时，旧 token 尚未清除，`closed` 尚未置位，`signedIn` 可再次为 true。旧会话被重新接受，闹钟 `apply()` 还存在异步落盘窗口。

这违反 01 §7.3/§10.1 的“登出开始立即封闭”和 07 自己的进程死亡承诺，是会话边界问题，不只是文案。

**必须修复：** 将“同步、持久地 bump epoch + closed + process fence”拆成登出的第一步，在任何 Room/闹钟清理及可中断工作之前完成；之后才能清本地状态并执行有界网络 logout。保留“迟到 logout 不得清新代 token”的现有 fence。增加 model 级故障注入测试，分别覆盖封闭前/后、网络等待中、清理中和进程重建；不能只测直接调用 `RealRepository.logout()`。

### P1-2：真实提醒卡仍把服务端 `scheduled` 翻译成“已排程”

**代码：**

- `android/app/src/main/kotlin/dev/qh/agent/ReminderStatus.kt:38-45`，尤其 `:40`：`"scheduled" -> "已排程"`。
- `android/app/src/main/kotlin/dev/qh/agent/FlowReminderCard.kt:16-22` 总是先显示这个标签，再显示本机结果。
- `android/app/src/test/kotlin/dev/qh/agent/ReminderStatusTest.kt:25-33` 还把这个错误文案固化成断言。

**静态复现：** fresh sync 返回 `status=scheduled`，随后精确与非精确 AlarmManager 调用都失败，或 fire_at 已过期。`localSchedules` 虽为 `FAILED/EXPIRED`，同一卡仍在 `FlowReminderCard.kt:17` 显示“已排程”。`UNKNOWN` 也同样先显示“已排程”。

后面的“服务端已确认 · 本机排程失败/待核对”不能消除前一个独立成功标签的误导。这直接违反 01 §5.3–5.4、§6.4、§18.10，也是原型 P2 绝不能进入真实逻辑的一类问题。

**必须修复：** `scheduled` 的中性服务端标签改为“服务端已确认”或同义文案；只有 `LocalScheduleResult.submitted` 才显示本机“已提交精确/非精确系统排程”。全仓同类文案已检查：生产命中集中在 `ReminderStatus.kt`、`FlowReminderCard.kt`、`LocalScheduleResult.kt` 与状态说明，没有发现另一个需要保留的 scheduled→本机成功映射。

按 AGENTS 的测试规则，修改 `ReminderStatusTest` 时应在 PR 写明：**原断言为何错（把服务端确认当本机回执），新断言覆盖什么（所有 local 轴均不产生本机成功文案）**，而不是为凑绿删断言。

### P1-3：恢复 A 的 404/tombstone 与 PATCH 409 都没有完成权威投影闭环

**代码：**

- `android/data/src/main/kotlin/dev/qh/data/CreateRecovery.kt:70-78`：GET 404 只把 recovery 写成 `deleted`，然后继续抛错。
- `android/app/src/main/kotlin/dev/qh/agent/MainActivity.kt:332-338`：`reconcileCreate` 没有处理上述删除后的本地 note/recoveredNote。
- `MainActivity.kt:210-217`：外层 `run` 只重新加载 recovery，不清 `recoveredNote`，也不做 projection delete。
- `MainActivity.kt:238-249`：sync tombstone 会 `markDeleted` 并删除 notes，但仍不清同 ID 的 `recoveredNote`。
- `MainActivity.kt:594-595`：只要旧 `recoveredNote != null` 且 B 非空，仍显示“用当前草稿更新此记事”。
- `MainActivity.kt:343-353`：`updateRecovered` 使用捕获的 `original.rev`，没有 409 分支；与 `saveEdit` 在 `:387-394` 的冲突刷新处理不同。

**404 静态复现：**

1. A 已对账为 live，`recoveredNote=A`，另有草稿 B。
2. 另一端删除 A。
3. 点击“核对原记事”，GET 返回 404；或直接同步收到 tombstone。
4. GET 404 路径中旧 A 仍可留在 `notes`；两条路径都可留下旧 `recoveredNote` 和“更新此记事”按钮。再次操作只会 PATCH 已删除 ID。

**409 静态复现：**

1. A 已对账，保存其 rev；另有 B。
2. 另一端修改 A，使 rev 前进。
3. 点击“用当前草稿更新此记事”得到 409。B 没有丢，这是正确的；但实现不刷新 A/rev，也不把新权威对象写回 `recoveredNote`。
4. 再点仍使用旧 rev，用户被卡在重复冲突中，不能按设计重新决定。

这违反 01 §7.5 第 7–9 步与 A09/A10/A14；不是“未知结果必须保守”所能解释的刻意收口。

**必须修复：**

- 404/tombstone 经同一 projection/tombstone 顺序应用：移除服务器 A 卡、清掉对应 `recoveredNote` 和更新入口、保留 B，不重新 POST 复活 A；关联提醒/闹钟通过 fresh sync 或既有本地删除栅栏核对。
- 409 保留 B，先刷新并展示权威 A 与新 rev，让用户再次明确选择；不得自动把 B PATCH 到刷新后的对象。
- 增加 Real/MockWebServer 不同 server ID 的 404、tombstone、409 测试，以及 model/projection 测试；只测 `CreateRecoveryFlow` 内部字符串状态不足以通过。

---

## 3. P1 — 已批准设计的功能/可达性硬缺口

### P1-4：A live + B 时没有“将 B 另存为新记事”；现有 UI 反而禁止所有新创建

**代码：**

- `android/app/src/main/kotlin/dev/qh/agent/MainActivity.kt:588-596` 只有核对、原样重试、用 B 更新 A、移除恢复项。
- `android/app/src/main/kotlin/dev/qh/agent/UnifiedComposer.kt:59-62` 在 note 模式且 `recovery != null` 时禁用“保存记事”。
- `android/data/src/main/kotlin/dev/qh/data/CreateRecovery.kt:28-35` 的 `prepare` 也要求当前 recovery 为 null。

**静态复现：** A 已保存但曾丢响应，重启后对账到 live A，继续编辑的 B 仍在。用户想保留服务器 A，同时把 B 另存为独立记事。当前没有这个操作；唯一变通是移除 A 的本机恢复项再保存，不能兑现设计中并列的“更新 A / 另存 B”选择。

这是 01 §7.5 第 7 步和 A08/A09 的端到端闭环缺失，不是要求新增泛化存储或服务器 API。

**最小可实现修复：** 只对已权威对账的 live A 提供“将 B 另存新记事”。新用户动作捕获当前 owner/processGeneration、生成新的 operationId 与 client_note_id，并先持久化 B 的完整新快照再 POST。现有单 recovery 记录无需扩成通用数据库：可以在同一 Room 事务中用 B 的新 recovery 替换**已不再需要恢复的 live A 本地项**，同时保留服务器 A 投影；不得对 unknown/deleted A 静默这么做。覆盖新 ID 不同、B 响应丢失、A/B 均不丢和双击/重启测试。

### P1-5：大量真实 Compose 操作没有 48 dp 最小目标，设计中的可达性要求尚未落地

**代码实例：**

- `android/app/src/main/kotlin/dev/qh/agent/FlowReminderCard.kt:22-30`：本地重试、来源、确认、贪睡、完成、取消全是未加最小尺寸的 `TextButton`。
- `android/app/src/main/kotlin/dev/qh/agent/UnifiedComposer.kt:72-73`：“结束并识别”“取消本次听写”未加最小尺寸；只有语音/保存按钮在 `:56,62` 显式设置了 48 dp。
- `android/app/src/main/kotlin/dev/qh/agent/MainActivity.kt:566-603,614-626,653-655,722-752` 等大量主操作同样未设置。
- `MainActivity.kt:631-640` 的提醒 `FilterChip` 也没有 48 dp 约束。

Material3 的默认 TextButton/Button 与 FilterChip 高度不能据此视为已满足 48 dp；源码中只有少数显式 `heightIn(min = 48.dp)`。这不需要伪装成设备测量即可判定实现没有兑现 01 §12.2“所有目标至少 48×48dp”。

**必须修复：** 给所有交互控件（包括文字按钮、日期时间对话框按钮、筛选 chip、顶部导航）统一落实至少 48 dp 触控边界，不能只放大语音主按钮。新增 Compose semantics/UI 测试读取实际 bounds，覆盖提醒卡所有操作、ASR、确认/删除对话框、搜索和详情；不能用 HTML CSS 或代码审查替代该测试。

---

## 4. P2 — 不扩大范围的真实 UX / 一致性问题

### P2-1：实际语音记事把 `source` 写回 `app`

`MainActivity.kt:90-94` 的 `acceptVoice` 直接调用 `updateDraft(text)`；`updateDraft` 在 `:302-307` 总是执行 `draftSource = "app"`。真正能设置 `voice_asr` 的 `createFromVoice`（`:309-314`）没有被当前统一入口调用。因此语音创建的不可变 A 虽完整持久化，来源元数据却错误。

建议让 `acceptVoice` 在 note 模式设置并持久化 `voice_asr` 后再 prepare，增加从统一入口到最终 `NoteCreate.source` 的测试；不要只测 `noteDraft(..., source=...)` 辅助函数。

### P2-2：`rejected/deleted` 恢复状态仍共用“结果未知、可能已保存”文案

`CreateRecovery.kt:54` 可写 `rejected`，`:73` 可写 `deleted`；但 `MainActivity.kt:588-596` 除 live 外都显示“保存结果待核对；服务器可能已保存原请求”。422 已确定未接受时这不够诚实；deleted 时还显示“核对原记事”，失败后 `:337` 又提示可原样重试，而按钮实际已隐藏。

应分别显示“本次创建未被接受”和“原记事不存在或已删除”，动作与状态一致；B 始终保留。这是局部状态呈现修复，不需要新恢复能力。

### P2-3：通知权限不是“首次本机提交成功后”一次性解释/请求

`MainActivity.kt:128-131` 的每次 submitted `localResult` 都置 `requestNotifications=true`；确认、贪睡和本地重试均可再次触发 `:67-73` 的系统请求。它不是启动即请求，也没有在排程失败时请求，这部分正确；但用户拒绝后，下一次成功重排仍可再次请求，且没有前置解释。

可用本进程/持久“已解释请求过”标记收窄为首次，已授权不重复；拒绝后的状态页保留手动设置入口。无需新增权限流程页面。

### P2-4：答案重播的实际 UI 没有使用已有仲裁 helper

`AnswerTts.kt:8-9` 和 `AnswerReplayTest.kt` 定义了 busy、ASR、TTS 状态规则；真实卡片却在 `MainActivity.kt:615` 直接 `speaker.speak(result.answer)`，只检查 `audioInputActive`。因此 helper 的 busy/TTS speaking 测试不能证明真实按钮行为；按钮在播报中仍叫“播报答案”，点击会重新开始。

开始 ASR 停止 TTS/pending、退后台停止和迟到答案不自动播的主路径是正确的。这里只需把现有 helper/状态接到按钮与“正在播报/停止”语义，不必恢复自动播放。

### P2-5：注意力区与几个来源/空结果入口未完整兑现

- `MainActivity.kt:557-675` 没有设计中独立的 suggested 注意力区；提醒嵌卡与筛选仍可达，因此不是功能丢失，但“只问一个需要决定的问题”的首页层级较弱。
- 无记忆答案没有“去搜记忆”入口；`MainActivity.kt:611-623` 只遍历 citations。
- 搜索空结果只有“没有找到相关记忆。”（`:687`），没有更短关键词提示；null note_id 搜索命中（`:692-695`）也没有明确“来源记事不可用”文字。

这些可以局部补齐，不需要新搜索或问答契约。

### P2-6：成功提问后没有消费对应版本的问题草稿

`MainActivity.kt:468-483` 创建不可变 `AnswerItem` 并正确保留 A/B 文字，但成功时只把 `askOutcomeUnknown=false`，从不清空已提交的 draft。重启后已成功问题仍作为可再次发送的普通草稿出现；当前页也容易让用户误以为尚未提问。

应按设计只在 `draftId/version` 仍等于提交版本时消费该草稿；若用户已继续输入下一版则保留。失败/结果未知时继续保留问题，不自动重试。

### P2-7：少量普通 UI 仍暴露开发概念或未控制预览密度

- `MainActivity.kt:749` 在记事详情显示 `rev`，违反 01 §5.2“不显示 rev”。
- 流卡正文 `:657` 没有 4 行预览边界，长记事会压过其他对象；答案 8 行折叠已经实现，但 `:614` 对短答案也始终显示展开按钮。
- 根 `Surface`（`:503`）使用默认 surface，而设计 canvas 只设置到 theme background，视觉底色需要实际截图校准；本报告不声称已看到色差。

这些属于视觉与概念密度修正，不影响 rev 请求本身。

### P2-8：大字体重排仍有明确结构性风险，键盘/焦点只有部分实现

提醒二级筛选 `MainActivity.kt:631-640` 与顶部操作 `:529-533` 都使用不换行 `Row`；没有针对 fontScale 2.0 的纵排/滚动策略。根页面及输入坞也主要靠固定 `Column`，只有列表局部滚动。不能在没有渲染证据时宣称 200% 已可用。

`UnifiedComposer.kt:43-46` 只显式实现 Ctrl/Cmd+Enter；未看到显式 Escape、Sheet/Dialog 焦点回还或状态 live-region 语义。Compose 默认按钮和对话框语义是可复用基础，不能据此断言全部键盘/TalkBack 失败，也不能据此宣称通过。需用第 6 节的实际证据收口。

---

## 5. 独立核对后成立、应保留的实现

以下不是“测试已通过”声明，而是对真实源码路径的核对结果。

### 5.1 会话 owner / mutex / refresh 主体顺序正确

- 所有已认证 Repository 公开操作都有 `RequestOwner`；默认参数在挂起前捕获（`Repository.kt:26-45`）。
- `RealRepository.authorized` 在取得 session mutex 后，先 `requireCurrent(owner)`，再 `store.read()`（`:125-129`）；没有先读 token 再检查。
- 401 后使用旧 refresh token 前复核（`:131-136`）；写 fresh token 在锁内复核且检查 user（`:137-141`）；新 access retry 前再次复核（`:142-147`）；响应返回前也复核（`:149-150`）。
- 分页每页复核；普通 refresh 不改 loginEpoch；同用户重新登录会通过 epoch/processGeneration 隔离旧 owner。
- `SecureTokens.identity()` 只读元数据与 `contains`，没有通过该 descriptor 读取 access/refresh 内容。
- `RequestOwnerTest` 源码确实包含 stale create/ask/read/logout 等锁后零 credential read/HTTP、晚到 refresh/logout 不得覆盖新代等断言；其实际网络/完整套件结果仍受 07 所述环境限制。

本报告没有发现需要推翻这套 owner 设计的 P1；P1-1 是 model 在调用该安全 logout 之前的顺序漏洞。

### 5.2 不可变 A、独立 B 与创建恢复主体正确

- `CreateRecoveryFlow.prepare` 在 HTTP 前保存完整 `NoteCreate`；落盘异常阻止发送（`CreateRecovery.kt:28-36`）。
- A 快照与后续 draft 分离；B 可在 A suspend 时独立编辑，A 回包不会清 B。
- reconcile 先 live client ID 查找再 GET，不 POST；未找到时只有显式 retry 才逐字段重放 A。
- authoritative GET 检查 client identity 并采用服务器正文/status/rev；MockWebServer 测试使用不同 server ID/client ID。
- 本地移除 recovery 不发服务器删除且保留 B；sending 恢复为 unknown。
- P1-3/P1-4 是必须补齐的尾部闭环，不要求改为自动重试或完整业务缓存。

### 5.3 服务端确认与本机 AlarmManager 分轴主体正确

- `LocalScheduleResult` 只有实际 exact/inexact 调用返回才标 submitted；SecurityException 可 fallback，双方失败为 failed，过期不提交（`LocalScheduleResult.kt:11-19`）。
- `ReminderAlarms.kt:96-115` 的 prefs 是恢复意图，不作为 submitted 回执；prefs commit 失败会 failed。
- `AgentModel.confirmReminder` 分服务器 POST、本地 schedule、apply、后续 refresh；refresh 失败有独立文案，不把服务器成功改成失败（`MainActivity.kt:256-270`）。
- `retryLocal` 先 fresh sync，再只调用 `alarms.retryLocal`，没有重复 confirm（`:137-145`）。
- 通知运行时权限、频道、exact capability 在 `SystemStatus.kt` 分开显示；失败/过期不会因 confirm 成功直接走 submitted 请求分支。

需要修的是 P1-2 的旧“已排程”标签及 P2 权限/入口细节，不是重新引入一个合成“成功”。

### 5.4 投影、ASR 与手动-only TTS 方向合理

- `NoteProjection` 同时使用 refresh/version、rev 和 tombstone；旧全量不能清掉新 create 或复活 delete。
- 前台 refresh/mutation/organize 共用 `refreshMutex`；alarm consume 另有 session/alarm generation fences；同 action ID 没有永久去重。
- `VoiceAttempt` 固定 owner/mode/draftVersion；模式切换取消 recognizer；既有 recognizer generation 防晚到回调；busy 时识别文字只留草稿，不自动排队。
- 开始 ASR 会停止 TTS/pending；退后台取消 Listening/Processing 并停止播报；答案迟到达不会自动播放。
- 07 选择 manual-only 回答播报，并在状态页诚实说明，这是可接受的首版收窄，不构成必须补自动播放的 P1。
- 当前进程多答案卡、来源回原记事、手动重播/停止、搜索、详情编辑删除、全部提醒状态、完成/取消二次确认、10 分钟授权、通知与 boot 入口均仍存在。

---

## 6. 仍然缺失的构建、UI 与设备证据

这些不是用源码推断可以消除的项目，也不应全部伪装成代码 P1。

### 6.1 构建与真实存储/网络

1. 等协调者完整 Gradle `assembleDebug/build/test` 的实际结果；本报告不假设成功或失败。
2. 真实 Room kapt 生成、DAO/schema、事务与升级测试；辅助 stub 不覆盖。
3. 允许 loopback 后运行 Real/MockWebServer 不同 ID、response loss、404/tombstone/409 测试。
4. Room 写锁与 logout/clear 的可控竞态及进程重建，不只内存 Disk fake。

### 6.2 必须取得的截图/Compose 证据

至少对真实安装 APK 取得以下截图或等效 Compose UI 证据，而不是 prototype HTML：

- 390 dp 常规字体：统一流、空态、离线/已加载非最新、asking/failed、unknown A+B、live A+B、提醒确认、详情/编辑/删除、搜索、状态页。
- `scheduled + UNKNOWN/FAILED/EXPIRED/SUBMITTED_EXACT/SUBMITTED_INEXACT`，证明没有“已排程”假成功。
- 320/390 dp、fontScale 1.0/1.3/1.6/2.0：顶部导航、四种流筛选、提醒二级筛选、长卡、输入坞、日期时间对话框；所有关键操作可见可滚动。
- Compose semantics bounds 证明所有目标至少 48 dp；实际功能边界、文字和焦点对比度测量；减少动画设置下状态仍可读。

### 6.3 TalkBack/键盘与真实音频/权限

- TalkBack 遍历与公告：模式 selected、unknown“服务器可能已保存”、双轴提醒、完整时间、来源编号、错误只公告一次；装饰元素不干扰。
- 硬件键盘 Tab 顺序、Enter/Space、Ctrl/Cmd+Enter、Escape、Dialog 首焦点和关闭后焦点回还。
- 麦克风允许/拒绝/永久拒绝，Permission/Listening/Processing 切模式、旋转、后台；真实识别失败后的文字路径。
- TTS 初始化延迟、中文不可用、长文本、新 ASR 停止 pending、后台停止、手动重播；需要设备结果，不能声称“已听到”。

### 6.4 AlarmManager/通知/升级

- exact 允许、拒绝、提交前撤回、fallback 成功/失败、过期；confirm 成功后 scheduler 失败与 refresh 失败；POST 与 schedule 之间进程死亡。
- 通知运行时权限和频道独立开关、真实提醒展示及完成/取消确认、贪睡；boot 断网先恢复 pending 且不同步误删。
- 旧版本升级保留 SecureTokens、`pending_reminder_alarms` 与通知 PendingIntent 行为。

---

## 7. 最终判定与 Codex 必修清单

**Verdict：BLOCKED，不得把当前快照作为最终统一体验 PR 发布。没有发现 P0；5 项 P1 全部必须修复。**

Codex 的最小必修顺序：

1. **先封闭登出**：持久 epoch/closed/process fence 先于任何本地清理；补进程死亡与迟到写测试。
2. **移除 scheduled→“已排程”**：服务端与本机文案彻底分离，并按规则解释修正旧错误断言。
3. **补齐恢复 404/tombstone/409**：权威投影删除、清 stale recoveredNote、保留 B、409 刷新后重新确认；用不同 server ID 测。
4. **提供 live A 下 B 新 ID 另存**：先落新快照再 POST，不清服务器 A，不复用 A 身份，不扩成通用任务系统。
5. **所有真实 Compose 操作落实 48 dp**，并用实际 semantics/UI 测试证明。

P2 可以按仓库规则说明理由后延期，但 P2-1 的错误语音来源、P2-2 的 rejected/deleted 不诚实文案和 P2-4 的“测试 helper 与真实按钮脱节”建议随本轮一并修复，成本低且直接影响统一体验的一致性。

在完整构建、真实 Room/网络测试及上述截图/设备证据出现之前，只能说“主体设计已落入真实源码，仍有明确阻塞”，不能说 UI 已验收、功能已端到端通过或可以发布。
