# Kimi 最终独立复核：真实源码、完整 Gradle 与 Compose host 证据

日期：2026-11-08  
性质：最终代码级 UI 反向 review + 真实 host/JUnit 证据核对；不重写 09/11 历史报告。  
范围：更新后的 `10-codex-review-fixes.md`、三份完整 Gradle 日志、测试汇总、真实 JUnit XML、生产源码 hash 清单，以及本轮唯一生产变更 `ReminderDialogs.kt` 和对应 `ComposeReviewTest.kt`。

## 0. 最终判定

**当前审查快照：source P0 = 0，source P1 = 0；11 留下的构建/测试 P1 已清零。**

- 完整最终候选、独立 stack-01、独立 stack-02 均为真实 `BUILD SUCCESSFUL`，不是辅助 stub 编译，也不是只跑选择性测试后的通过声明。
- 最终候选 debug/release app 各 111 个测试、data 60 个测试均为 0 failure / 0 error / 0 skipped；关键 Room/model/wire/Compose XML 与汇总一致。
- 本轮 DatePicker 0dp 动作区是实际产品布局缺陷；当前 `weight + body scroll` 修复合理，短屏/大字体真实 host 交互回归已在 debug 和 release 通过。
- **本评审不再以 P0/P1 阻塞最终 PR。** 这不是设备 QA、视觉验收或硬件安全放行；未决三组 P2 与下述真机风险仍然存在。

**没有看过应用截图、浏览器渲染或真实设备 UI，没有点击真机、听到语音或完成 TalkBack/硬件验收。** 下文“通过”只指实际 host/Gradle/JUnit 证据。

---

## 1. 完整构建不是 stub 或选择性测试冒充

### 1.1 最终完整候选

只读日志：`/workspace/qh-unified-evidence/implementation/final-full-gradle-green-candidate.log`。

- 日志实际执行/完成 `:app:testDebugUnitTest`（`:199`）、`:app:testReleaseUnitTest`（`:263`）、`:app:test`（`:266`）。
- 同时完成 debug/release assemble、`app:lint/check/build`、data build 和 core-security build（`:212-278`）。
- 结尾 `:280-281`：`BUILD SUCCESSFUL in 1m 5s`，204 个 actionable tasks，无 FAILED。
- 最终 invocation 的 `data:test` 是 UP-TO-DATE，不伪称该次重新执行；其实际 data XML 来自前一次真实运行，当前显示 60/60 通过。最终 app debug/release 测试 task 均实际执行。

`verified-gradle-test-summary.json:2-20`：

| 完整候选 task | tests | failures | errors | skipped |
| --- | ---: | ---: | ---: | ---: |
| app debug | 111 | 0 | 0 | 0 |
| app release | 111 | 0 | 0 | 0 |
| data | 60 | 0 | 0 | 0 |

### 1.2 两个独立前置 stack

- `stack-01-full-gradle.log:276-277`：`BUILD SUCCESSFUL`，192/192 tasks executed；包含 data test、app debug/release test、assemble、lint/check/build。
- `stack-02-verified-full-gradle.log:289-290`：`BUILD SUCCESSFUL`，204/204 tasks executed；同样包含完整目标。
- 汇总 JSON `:22-60`：stack01 为 app 90+90、data52；stack02 为 app94+94、data60，全部 0 failure/error/skipped。

这些结果支持当前三段实现的实际构建与 host 回归，不把最终候选的测试数量倒填到前置 stack。

### 1.3 关键真实 JUnit XML

- debug `ComposeReviewTest`：8 tests，0 skipped/failure/error；明确包含短屏用例和原大字体用例。
- release `ComposeReviewTest`：同样 8/8 通过。
- debug/release `LogoutRoomModelTest`：各 4/4，通过同步持久登出、Room 重开、清理故障、跨代与写锁场景。
- debug/release `RecoveryModelRoomTest`：各 4/4，通过 404/tombstone、409、save-copy response loss/restart/C、voice source/ask version 场景。
- `RealRecoveryModelTest`：1/1，通过 RealRepository + model + 真 Room 的 409→404。
- `RecoveryWireReviewTest`：3/3，通过真实 Retrofit/MockWebServer 不同 ID、404/tombstone、409 和 response-loss save-copy。

因此 11 的“测试编译失败 + wire NPE”红灯已经由实际执行结果关闭，不是仅凭 10 的自评。

---

## 2. 本轮 DatePicker 真实产品 diff 审查

### 2.1 原缺陷与修复机制成立

10 记录的真实失败为“使用所选本地时间”高度 0dp；本轮不是通过放宽断言解决。

当前 `ReminderDialogs.kt:30-46`：

- DatePickerDialog 的确认/取消按钮仍是 Material3 未加权动作区。
- 日期/时间正文在 `:41` 使用 `Modifier.weight(1f, fill=false).heightIn(max=600.dp).verticalScroll(...)`。
- 这让父 Column 先为未加权 footer 分配空间，再把剩余空间给可缩小、可滚动的正文；不再让正文吃掉整个 capped height。
- 主 AlertDialog 的文字区也在 `:51-58` 独立滚动，降低长文案/大字体把外层动作挤走的风险。

这是局部、可实现的 Compose 布局修复，不改变平台、服务端或提醒契约。

### 2.2 确认、取消、日期时间语义保留

- 只有点击“使用所选本地时间”才把 Material date/time state 转为设备时区 Instant 并调用 `onFireAtChange`（`ReminderDialogs.kt:31-37`）。
- picker 取消仅 `choosing=false`（`:38`），不写时间、不确认提醒。
- 外层确认仍调用 `confirmOverrides`、校验未来时间，然后仅调用一次 `onConfirm`（`:60-69`）；busy 禁用与 dismiss 行为保留。
- 标题、时区、目标时间、服务端/本机分轴说明和错误文案均保留。

没有发现本轮 diff 引入重复 confirm、取消误提交、时间改写或本地排程契约回归。

### 2.3 48dp 可达不是滚动 footer 造假

共享真实场景 `ComposeReviewTest.kt:163-193` 同时用于 320×1600 和 320×640、fontScale2：

1. 打开 picker 后，确认按钮在正文滚动前就必须 `assertIsDisplayed()` 且宽高至少 48dp（`:173-178`）。
2. 取消同样必须 displayed + 48dp；点击后断言 `selectedTime == null` 且 `confirmed == 0`（`:177-181`）。
3. 重开后只滚动日期/时间正文到时区（`:182-184`）。
4. footer 在正文滚动后再次要求 displayed + 48dp，再点击（`:185-187`）。
5. 选中时间必须是未来 Instant，外层确认仅一次（`:188-192`）。

因此它不是先 `performScrollTo()` footer 再宣称可达，也没有用不可见 semantics node 代替可见动作。原 48dp 数值断言仍存在于 `:77-80` 及该场景中，没有降阈值或删除测试。

---

## 3. 320dp harness 修正不是掩盖产品故障

当前 `ComposeReviewTest.kt:40-53` 使用真实 `AndroidComposeTestRule` 管理 activity、recomposer、clock 与 disposal；PAUSED main looper 的启动/销毁工作显式 drain。`content()` 在 `:54-70` 同时断言：

- configured `screenWidthDp == 320`；
- 实际 `decorView.width / density == 320dp`；
- 带 tag 的真实 Compose root 宽度为 320dp。

这使 host 窗口、独立 Dialog window 和原测试意图的 320dp 内容一致。10 `:132-154` 记录了 Robolectric #8460 与旧 390 host/320 内容不匹配的依据；本报告把它作为环境背景，不宣称亲自联网复验该 issue。

关键是：统一窗口后测试没有变绿掩盖 0dp，而是继续暴露真实 DatePicker 动作区缺陷，随后才修改产品并新增 320×640 短屏交互。所有 bounds 阈值保留，且加入实际窗口断言。因此没有证据表明这是改 fixture 消除产品失败。

**明确限制：这些结果只覆盖 320dp host/content、所列 1600/640 高度与 fontScale2。390dp platform Dialog、其他手机窗口和真实设备渲染仍未验证。**

---

## 4. 三个关键 P1 约束抽查与源码 hash 轨迹

`final-production-source-hashes.json` 提供了当前生产文件 SHA-256；本次实际读取对应源码并抽查如下。未运行 Shell 重算 hash，清单值与行号共同用于审查轨迹。

| 约束 | 当前源码证据 | 清单 SHA-256 |
| --- | --- | --- |
| 登出先持久封闭，再清理；receipt 防跨代 | `SecureTokens.kt:21-23` 同 commit 写新 epoch+closed；`MainActivity.kt:546-565` beginLogout 先于协程/Room/alarm；`Repository.kt:157-178` receipt 校验覆盖清理、token read/final clear | Main `5309ab1420ac72a6b542e14450c5f52a90bae101afa787361d50bb531f4d74c2`；Repository `f852b6ac1490801fd93b25e91124a2cfd035cb35a2e85533a0e4f1055e482a3d`；SecureTokens `83c850ac09da172d6a763fc394645a29ff71e6e96afed89b0efde8359ded434a` |
| 恢复删除/409 与 B 新身份另存 | `MainActivity.kt:339-407` projection delete、清 stale affordance、409 authoritative reload 保 B；`CreateRecovery.kt:28-37` 仅 live A 可替换且生成新 operation/client 快照 | CreateRecovery `75cc50be8ad8bbfc515264a23782a7cd82c19ecff7d29a3fad279d438d773217` |
| scheduled 不等于本机成功 | `ReminderStatus.kt:38-45` 为“服务端已确认”；`ReminderCard.kt:37-41` 分列本机 local label | ReminderStatus `00bb7c40f84299ea33c8174ccf049372c02f2d2ee4cf27df4d5fdb1225be4d67` |

本轮 `ReminderDialogs.kt` 清单 hash 为 `c8c38144b2925815d0de18115ca76991e744dfa3b60ba4929a761ee508391c38`。实际 diff 审查未发现破坏上述约束的新代码路径。

---

## 5. 未决三组 P2（不因全绿而消失）

1. **输入坞位置 UX**：`MainActivity.kt:652,711` 将 UnifiedComposer 放在 LazyColumn 末项。大字体下可滚动是正确的，但多内容普通页面也不再固定展示语音主入口，需后续产品/视觉校准。
2. **scheduled 的本地重试适用范围**：`ReminderCard.kt:20-22` 对 submitted/expired 也显示“仅重试本机排程”。expired 重试不会重复服务器 confirm，安全但无效；应按 local 状态区分核对、重试和修改未来时间。
3. **恢复移除确认文案**：`RecoveryCard.kt:38-42` 对已知 rejected/deleted 仍使用“服务器可能已保存”的泛化说明。主卡状态已经正确，因此保持 P2，不升级为数据正确性问题。

这些不需要扩大平台、服务器或持久化范围。

---

## 6. 仍需明确保留的测试/设备风险

### 物理键盘、TalkBack 与视觉证据

- 物理键盘完整 Tab/Enter/Space、输入框外 active-ASR Escape、Dialog 焦点进入/回还尚未实测。
- TalkBack 遍历、状态公告次数、双轴提醒与 unknown 恢复的实际朗读尚未验收。
- 没有截图或视觉渲染检查；色彩/对比度、装饰/功能边界、长文案截断、IME、减少动画仍需证据。
- 390dp、fontScale 1.0/1.3/1.6 的完整矩阵、更多短屏/横屏和真实 ROM 未覆盖。

### 音频、权限、闹钟与通知

- 真实麦克风允许/拒绝/永久拒绝、系统 ASR 服务/网络、Permission/Listening/Processing 的旋转与后台交错。
- 真实 TTS 初始化延迟、中文不可用、长文本、停止/后台/新 ASR 仲裁；host 测试不能证明实际听到了什么。
- POST_NOTIFICATIONS 拒绝、频道开关、SCHEDULE_EXACT_ALARM 撤回与 fallback、AlarmManager 实际送达/Doze。
- 通知栏完成/取消确认、贪睡、真实 PendingIntent 与通知展示。

### boot、升级与端到端环境

- boot 断网恢复 pending alarms、进程在 confirm/schedule 间死亡及真实系统重启。
- 旧版升级保留 SecureTokens、`pending_reminder_alarms` 和通知行为。
- MockWebServer/RealRepository host 证据不是连接真实部署服务器的手机端到端验收。

## 7. 最终出口

- **源码 P0/P1：清零。**
- **11 的构建/测试 P1：清零。** 最终完整候选和两个独立前置 stack 均有真实完整 Gradle 成功及 XML 证据。
- **本轮 DatePicker 产品修复：接受代码与 host 证据；390dp/真实设备仍未验证。**
- **三组 P2 与物理设备/视觉/音频/权限/通知/boot/升级风险继续明示，不冒充 QA 通过。**
