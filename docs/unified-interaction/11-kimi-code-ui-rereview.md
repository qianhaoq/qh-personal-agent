# Kimi 对 Codex 修复后真实实现的独立复审

日期：2026-11-08  
对象：`feat/voice-agent-unified-interaction` 当前未提交工作文件；基线仍为 `3600c3ab4a1c499f099e39be6b8084b3351dd7b7`。  
输入：完整阅读 `10-codex-review-fixes.md`，保留并重新对照 `09-kimi-code-ui-review.md`，逐项检查当前生产代码、新测试及协调者完整 Gradle 日志。  
边界：只读审查；没有修改应用、测试、09 原报告或其他设计文件。

## 0. 最终结论

### 源码处置结论

**09 的五项 P1 在当前源码中均有实质修复：5 项 fixed，0 项原样 remaining，0 项整体 false-positive。**

其中 P1-5 的原论证需要自我修正：**“没有显式 modifier 就证明 Material3 触控目标不足 48 dp”是过度推断**；Material3 默认 minimum interactive semantics 可能已扩大触控区域。但原设计要求同时覆盖实际布局与触控边界，当前显式 `touchTarget()` 和 host bounds 用例仍是合理加固。因此它不是整项误报，而是原论证中关于默认触控行为的部分误判。

### 发布/门禁结论

**最终仍为 BLOCKED：P0 = 0，P1 = 1（完整构建与测试红灯），未决 P2 = 3 组，另有明确设备/渲染证据缺口。**

- 没有在新修复中发现额外的会话、A/B 数据身份或 AlarmManager 安全 P0/P1。
- 但完整 Gradle 不是“仍在运行、结果未知”：所给日志已结束于 `BUILD FAILED`，共 3 个失败 task。
- 新增 Room/model/Compose host 测试是真实生产代码用例，不再是旧 stub；然而 debug/release 测试源码都编译失败，所以这些用例**没有执行**，不能据此宣称修复通过。
- 本复审仍只是实际代码级 UI/状态审查。没有设备、应用截图、浏览器渲染、TalkBack、真实麦克风、TTS 或硬件验收；没有“看见界面”或“点击真机”的结论。

---

## 1. 实际完整 Gradle 证据

只读文件：`/workspace/qh-unified-evidence/implementation/final-full-gradle-wrapper.log`，并读取其指向的实际 JUnit XML。

### 已完成的真实工作

日志显示：

- 实际 Room kapt debug/release 工作及生产 app debug/release Kotlin 编译已执行（日志 `:127-147,229-230`）。
- `:app:assembleDebug` 与 `:app:assembleRelease` 到达完成（`:254,273`）。
- data 测试实际运行；并非全部因 socket 被跳过。

这些只证明对应 task 到达完成，不抵消后续失败。

### P1：最终构建失败，且新增 host 测试未运行

1. **data wire 测试红灯**：日志 `:231-238` 为 `60 tests completed, 1 failed`，失败于 `RecoveryWireReviewTest.realSaveCopyResponseLossRestartUsesNewIdentityAndKeepsBothServerObjects`。
2. **debug/release app 测试编译均失败**：日志 `:276-292`，包括：
   - `ComposeReviewTest.kt:51-52` 的 bounds `width/height` 解析失败；
   - `LogoutRoomModelTest.kt:133` 的跨模块 public property smart-cast 错误；
   - `ReviewModelFixture.kt:9,13` 的 `ResponseBody.Companion/toResponseBody` 解析失败。
3. 日志 `:303-345` 明确为 `Build completed with 3 failures`、`BUILD FAILED in 3m 28s`。

wire 失败的真实 XML 堆栈为：

- `android/data/build/test-results/test/TEST-dev.qh.data.RecoveryWireReviewTest.xml:5-8`
- `RealRepository.notes` 在 `Repository.kt:191` 执行 `result.addAll(page.items)` 时，`page.items` 为 null，约 60 秒后抛 NPE。

**复现依据：** 当前 coordinator 命令即可得到上述失败；无需声称设备复现。修复时必须先核对实际响应/请求顺序，查明为什么有效 `NotePage` 预期变成 null items。若属于 fixture/响应队列错位，应修正 fixture 并保留所有身份与“无重复 POST”断言；若属于真实 Gson/异常边界，则应把非法响应转成受控契约/IO 错误而不是 NPE。禁止 catch NPE 后当作成功、删除 response-loss 断言或放宽身份检查凑绿。

按 AGENTS.md，CI/测试红灯为 P1。完整命令必须在修复后重新运行；仅让 assemble 成功不够。

---

## 2. 09 P1-1：同步持久登出、清理顺序与跨代 receipt

### 判定：**FIXED（源码层面）；host 执行仍被 P1 编译失败阻断**

**生产证据：**

- `SecureTokens.kt:21-23` 在一次 `commit()` 中同时写新 epoch 和 `closed=true`，不是两个可交错提交。
- `RealRepository.beginLogout` 在 `Repository.kt:157-163` 同步执行 `requireCurrent → closeSession → closing=true → 新 processGeneration`，返回仅含 metadata 的 `LogoutSession`；`LogoutSession.kt:4` 不含 token。
- `AgentModel.logout` 在 `MainActivity.kt:546-553` 于任何协程、Room、TTS、alarm 清理之前调用 `beginLogout`；随后立即 bump AppRepositories session fence。
- 清理由 `MainActivity.kt:555-559` 的 `withClosingSession` 包裹；composer 清理失败仍继续 alarm/UI 清理。
- `Repository.kt:164-178` 对清理、credential read 和最终清除都校验同一 receipt 的 generation + identity；新同用户登录改变 generation/identity 后，旧清理与旧 finish 不得继续。
- 网络 finish 在 model 中有 8 秒边界（`MainActivity.kt:562-565`）。

**原复现窗口已封闭：** 现在不存在“先清 Room/alarm，后持久 closed”的 model 路径。即使在排队清理前进程丢失，持久 closed 与新 epoch 已落盘；旧 owner 不再 current。

**新测试源码：** `LogoutRoomModelTest.kt:55-76` 覆盖 queued cleanup 前持久关闭和重建；`:79-115` 覆盖清理故障与同用户重登；`:139-171` 覆盖真实 Room 写入/logout 序列及迟到写拒绝。这些断言方向正确，但本次编译失败使它们尚未运行。

---

## 3. 09 P1-2：scheduled 与真实本机排程分轴

### 判定：**FIXED（源码层面）；Compose 用例待实际编译/执行**

- `ReminderStatus.kt:38-45` 已将 `scheduled` 改为“服务端已确认”。
- `ReminderCard.kt:37-41` 将服务端标签与 `本机：${local.label}` 分成两个 Text。
- `LocalScheduleResult.kt:4-8` 只有 `SUBMITTED_EXACT/SUBMITTED_INEXACT` 的 label 声称“已提交”，UNKNOWN/FAILED/EXPIRED 均不声称本机成功。
- `ReminderAlarms.kt:96-115` 仍只在实际 AlarmManager 调用返回后产生 submitted；prefs 不是 receipt。
- 全生产源码检索未再发现 `已排程` 映射或 `scheduled → 已提交` 文案。剩余“已排程”命中只在 Compose 测试的反向断言中。
- `ReminderStatusTest.kt:25-33` 对旧断言的错误原因有明确注释，其他状态断言保留，符合测试修正规则。
- `ComposeReviewTest.kt:67-76` 枚举所有 local 结果，检查服务端确认与本机结果分离，并拒绝假“已排程/已提交”。

因此 UNKNOWN/FAILED/EXPIRED 下不再出现旧的独立“已排程”成功标签。真实系统准点、通知权限和频道仍需设备验证。

---

## 4. 09 P1-3：404/tombstone 投影与 409 权威刷新保 B

### 判定：**FIXED（源码层面）；部分 wire 用例已实际通过，model/Room 用例尚未执行**

**404/tombstone：**

- `MainActivity.kt:339-348` 的 `applyMissingNote` 统一执行 projection.delete、清 recoveredNote/detail、移除关联 reminders，并在现有 alarm fence 内取消本地关联闹钟。
- `applyRecoveryState`（`:349-352`）在 durable recovery 为 deleted 时应用上述投影删除。
- `reconcileUnderLock`（`:353-359`）在异常中也通过 finally 应用 durable 状态，因此 GET 404 不再只改 Room 字符串。
- full sync 在 `:242-255` 先独立标记 recovery tombstone，再应用 note deletion 并清 stale recoveredNote；不再受 alarm-consume generation 是否成功影响。
- B 的 ComposerDraft 没有被这些路径清除。

**409：**

- `updateRecovered` 在 `MainActivity.kt:381-405` 仅对 live A 执行 PATCH。
- 409 分支 `:394-397` 调用 `reconcileUnderLock` 获取权威 A/current rev，保留 B，并明确要求用户再次选择；没有自动用新 rev 重发 B。
- 404 分支 `:398-401` 也进入相同 deleted 投影闭环。

**测试证据：**

- `RecoveryModelRoomTest.kt:21-67` 覆盖 GET404、sync tombstone、stale affordance 消失、409 后权威 rev 和第二次显式 PATCH。
- `RealRecoveryModelTest.kt:20-72` 使用 RealRepository + model + 真 Room + 不同 server/client ID 覆盖 409→404。
- `RecoveryWireReviewTest.kt:43-85` 的真实 Retrofit/MockWebServer 404/tombstone 与 409 用例在实际 XML 中通过（XML `:62-63`）。
- 但上述 model/Room host 用例因 app 测试编译失败未运行；不能用 wire 两个通过替代它们。

原“404 后旧 A/更新入口残留”和“409 后反复使用旧 rev”的源码路径已消除。

---

## 5. 09 P1-4：live A 时 B 新 ID 另存，不丢 A/C

### 判定：**FIXED（源码层面）；关键 response-loss wire 用例当前失败，故发布不能放行**

- `RecoveryCard.kt:31-34` 仅在 live A 且 authoritative ID 与 discovered ID 一致、B 非空时显示更新与“将草稿另存为新记事，保留原记事”。
- `AgentModel.saveCopy`（`MainActivity.kt:370-379`）重新经 `run` 捕获新 operation owner，要求当前 live A，持久化 B，再 prepare/send；A 的 `notes` 投影没有删除。
- `CreateRecoveryFlow.prepare(replaceLive=true)`（`CreateRecovery.kt:28-37`）只允许替换已 live 且 operation 不同的 recovery；生成新的 `client_note_id`，先原子保存完整 B 快照，再允许 POST。
- prepare 后 composer 被清为下一草稿空间；用户在 B 在途时输入的 C 由独立 version/draft 保留。response loss 时 B recovery 保留，A 服务器对象与当前进程 A 投影均保留。
- busy 在 coroutine 前设置，双击不会生成第二个操作。

**测试源码：** `RecoveryModelRoomTest.kt:68-96` 检查新旧 operation/client 身份、双击仅一个 B POST、A/B 均存在、C 保留和重启恢复。

**重要限制：** 对应真实 wire 用例 `RecoveryWireReviewTest.kt:87-111` 当前实际失败于后续 notes 读取 NPE，而不是已证明通过。因此这里只能判定“原功能缺口已在代码中实现”，不能判定整条 response-loss/restart wire 验收通过；第 1 节 P1 必须清零。

---

## 6. 09 P1-5：48 dp 与字体重排

### 判定：**FIXED（app-owned 源码布局已覆盖）；原默认触控推断部分修正，实际 host 验证仍未执行**

- `TouchTarget.kt:7-8` 用 `sizeIn(minWidth=48.dp,minHeight=48.dp)` 同时约束可见布局最小尺寸。
- 当前 `MainActivity`、`ReminderCard`、`RecoveryCard`、`AnswerCard`、`ComposerControls`、`UnifiedComposer`、`ReminderDialogs`、`NotificationPrompt`、`SystemStatus` 的 app-owned Button/TextButton/OutlinedButton/Tab/FilterChip 均接入 `touchTarget()`。
- 顶部导航、提醒状态筛选、note/detail 操作改用 FlowRow（`MainActivity.kt:599-603,669-676,689-693,785-788`）。
- composer 移入可滚动流（`:652,711`），避免固定高 dock 在大字体下吃掉整个 viewport。
- `ComposeReviewTest.kt:46-55` 同时检查 layout bounds 和 `touchBoundsInRoot`；`:62-65` 单独验证 Material3 默认触控边界；`:78-165` 覆盖提醒、ASR、恢复/删除/确认对话框、重播及 320dp/fontScale 2 的导航和筛选。

但测试编译在 `:51-52` 已失败，所以当前没有实际 bounds 结果。不得把“测试代码存在”写成“48 dp 测试通过”。Material3 内置 DatePicker 控件和真实手机 ROM 的行为也仍需测试/设备证据。

---

## 7. P2 逐项处置核对

| 09 项 | 当前判定 | 实际依据 |
| --- | --- | --- |
| P2-1 语音 source | **fixed in source** | `MainActivity.kt:97-101,308-314` 统一 acceptVoice 在 note 模式持久化 `voice_asr`；`RecoveryModelRoomTest.kt:99-119` 有真实入口断言，待 host 执行。 |
| P2-2 rejected/deleted 文案 | **核心 fixed；有一处残留 P2** | `RecoveryCard.kt:11-16,27-35` 分状态并隐藏不适用动作；但移除确认 `:38-42` 对 rejected/deleted 仍使用“服务器可能已保存”的泛化文案。 |
| P2-3 首次通知解释 | **fixed in source** | `NotificationPrompt.kt:9-15` 持久原子 claim；`MainActivity.kt:66-79` 已授权/已解释不自动重弹；`NotificationPromptTest` 待执行。系统拒绝/频道仍需设备 QA。 |
| P2-4 重播 helper 脱节 | **fixed in source** | `AnswerCard.kt:25-29` 真实按钮同时使用 busy/ASR/TTS helper、enablement 与 speaking 文案；停止独立。无自动 TTS。 |
| P2-5 注意力/来源/搜索 | **fixed in source** | `MainActivity.kt:653-657` 有真实 suggested attention；`AnswerCard.kt:31-36` 有无来源搜索和不可用文案；`MainActivity.kt:724-733` 补空搜索提示及 null source。 |
| P2-6 成功提问消费版本 | **fixed in source** | `MainActivity.kt:521-540` 仅 version/mode/text 全匹配时清空；新 B 保留，失败/未知不自动重试。对应 host 用例待执行。 |
| P2-7 rev/密度/canvas | **fixed in source** | detail 不再显示 rev；search 不显示技术 ID/score；note 4 行省略（`MainActivity.kt:694`）；答案仅真实 overflow 时展开（`AnswerCard.kt:18-24`）；根 Surface 用 background（`MainActivity.kt:573`）。 |
| P2-8 大字体/键盘/焦点 | **部分 fixed，按 10 的 P2 defer 保留** | 已有 FlowRow、scroll、48dp、Ctrl/Cmd+Enter、文本字段 Escape 和 error live-region；完整物理键盘/焦点回还、全局 ASR Escape、TalkBack、对比度、减少动画及真实手机 viewport 矩阵仍未验证。 |

### 当前准确未决 P2

1. **P2：输入坞不再固定。** `MainActivity.kt:652,711` 把 UnifiedComposer 作为 LazyColumn 最后一项。多内容页面需要滚到底部才能看到语音主入口，弱化 01 的“无需滚动即可触达”；它换来大字体可达性，属于真实 UX 折中而非 correctness 失败。应做普通/大字体布局校准，不能宣称仍是固定底部输入坞。
2. **P2：所有 scheduled 状态都显示“仅重试本机排程”。** `ReminderCard.kt:20-22` 对 submitted 和 expired 也显示；expired 点击只会再次得到 expired，不会重复 confirm，因而安全但无效。应按 local 状态区分“核对/重试”和“修改未来时间”。
3. **P2：移除恢复项确认文案仍可更精确。** `RecoveryCard.kt:38-42` 在已知 rejected/deleted 时仍说“可能已保存”。主卡状态已正确，因此不提升为 P1；建议按状态解释本地移除范围。

10 中明确 defer 的物理键盘/焦点、TalkBack、对比度、减少动画和真实 viewport 证据继续保留，不能因上表其他 P2 fixed 而视为完成。

---

## 8. 保留的端到端能力与测试风险

源码中仍保留：

- 统一记下/提问、不可变创建与整理轮询、详情编辑/删除和 rev 冲突处理；
- 当前进程多答案卡、来源回记事、手动重播/停止、搜索；
- suggested/scheduled/done/cancelled 筛选、无来源提醒保护、确认/完成/取消/10 分钟操作；
- 服务端 confirm、本地 exact/inexact/failed/expired 分轴、本地重试不重复 confirm；
- 通知栏二次确认/fresh rev、boot restore、旧 token/alarm prefs 名称和 `allowBackup=false`；
- 网络串行、忙时保留下一草稿、无自动业务队列、无持久聊天/完整离线业务缓存。

**当前最大测试风险不是“缺少新测试源码”，而是新测试没有全部进入执行：** app debug/release 测试编译均失败；data 的 save-copy response-loss 用例实际红灯。修复后必须重新跑完整命令，并单独确认 LogoutRoomModelTest、RecoveryModelRoomTest、RealRecoveryModelTest、ComposeReviewTest、NotificationPromptTest 确实被执行且通过。

---

## 9. 仍需渲染/设备证据，不作安全或 QA 放行

1. 真实 APK 的 320/390dp 与 fontScale 1.0/1.3/1.6/2.0 截图/测量：首页、固定或滚动输入坞、长卡、所有对话框、恢复状态和提醒双轴。
2. 所有触控目标的实际 Compose bounds 结果；功能边界、文字、焦点对比度；减少动画。
3. TalkBack 遍历/公告、物理键盘 Tab/Enter/Space/Escape/Ctrl+Enter、Dialog 焦点进入与回还。
4. 真实麦克风拒绝/永久拒绝、ASR 生命周期、TTS 初始化/中文不可用/后台/手动重播。
5. exact 允许/拒绝/撤回、fallback、通知权限与频道、POST 后本地失败、refresh 失败、进程死亡和 boot/升级。

这些没有设备证据；本报告没有也不声称完成它们。

## 10. 复审 verdict 与唯一 P1 出口

**源码复审：09 五项 P1 均已修复，无新增安全/数据正确性 P0/P1。发布复审：仍有 1 项 P1，即最终完整 Gradle 红灯。**

Codex 必须：

1. 修正三个 app 测试编译错误，不能删测试或绕过 host 用例；
2. 查明并修复 save-copy response-loss wire 用例的 null `page.items` 失败，保留不同 ID、A/B/C、重启和无重复 POST 断言；
3. 重新执行完整 Gradle，提供最终全绿或新的真实失败输出；
4. 之后再由独立复审核对实际结果。P2 与设备证据不得被写成已验收，也不需要为它们扩大 Android 平台或服务器范围。
