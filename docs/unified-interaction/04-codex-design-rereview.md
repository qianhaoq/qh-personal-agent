# Codex 独立复审：统一 Android 语音交互修订稿

评审基线：`3600c3ab4a1c499f099e39be6b8084b3351dd7b7`，实际 HEAD 一致。评审对象是本轮实际读取的 `01-kimi-design.md`、`03-kimi-revision.md` 和 `prototype.html`，并与自己的 `02-codex-design-review.md` 逐项比较；不是对 Kimi 自评结论的背书。

**结论：没有发现 REMAINING DESIGN P0/P1。此前 3 项 P1 在规范层面均已消除，可据此进入实现；仍有下列 P2 原型一致性问题。不能声称原型已经验证这些安全机制，更不能声称未来实现安全或 App 测试通过。** 原型的关键安全说明已改正，但点击行为不是规范的完整可执行模型。Kimi 所说“P2 全部采纳”可以描述规范处置，不能解释成所有原型行为已正确或验收完成。

## 实际范围与快照

已读 AGENTS.md、02 全文、01 全文、03 全文与 HTML/CSS/JavaScript 源码。重新核对 Android `Repository.kt`、`Api.kt`、`MainActivity.kt` 的提交/刷新/提醒链路、`AppRepositories.kt`、`core-security/.../SecureTokens.kt` **源码**、`VoiceAsr.kt`、`AnswerSpeaker.kt`、`ReminderAlarms.kt`、`ReminderNotificationAction.kt` 与 app/data Gradle 声明；核对服务端 `api/notes.py` 创建/读取、`schemas.py` 序列化、`api/reminders.py` 确认、`domain/channel.py` 未来时间约束以及 `api/ask.py`、`api/sync.py`。这些是可实现性证据，没有修改服务端。

没有读取 key、实际凭证或 env；没有进入其他 worktree，没有外部消息、网络检索、push、PR、合并、部署或 routine。`git diff --stat <baseline> -- android server` 无输出；本次只新增本报告。未运行浏览器、设备、TalkBack、构建、Android 单元/Compose 或服务端测试；没有任何渲染/可用性实测结论。

实际最终复核文件的 SHA-256：

| 文件（相对本目录） | SHA-256 |
| --- | --- |
| `01-kimi-design.md` | `f6142f6d0d2b11e0e4055aeebbca3e73c1f026186a6e2702979ed52872592f8f` |
| `03-kimi-revision.md` | `bdf6d3c3ec612ad939939836196221d8a3ba0ac20207e0db5ed666007a51cfa4` |
| `prototype.html` | `7d6307bff1144d62fe5c4cc0e3319ef45e04a03e060616bbf40659bfc5552bb4` |

并发变化说明：首次读 03 时 hash 为 `647327e25833a9bc317adad45d112b076e29a2bd63f28c5b2f1c4a462852aa5c`，后续发现变化后已重新完整读取，以上表的后一个版本为本报告对象。01/prototype 在前后采样中未变。协调者若看到不同 hash，应对变更重新评估，不能沿用本结论。

## 原 3 项 P1 的独立核验

### 1. 旧会话不得读取/使用新凭证：规范已闭环

01 §7.2 不再只有响应检查：RequestOwner 固定 runtimeKey、userId、持久 loginEpoch、processGeneration 和 operationId；所有业务 Repository 操作接收 owner。顺序明确为 session mutex → 不含 token 的 SessionDescriptor 逐项比较 → 匹配才 store.read/HTTP。refresh token 使用前、刷新后的 access token 重试前再次检查；同用户重新登录也隔离。§7.3 要求取得统一写锁后重验，在同一数据库事务条件提交 owner/epoch/operation/version；登出清理也经过同一写锁。先读缓存后换代不可显示，普通 refresh 不换登录 epoch，重启不依赖重置 AtomicLong。

这正面覆盖原反例，而非仅取消协程或藏掉旧卡。现有 `Repository.kt:70–101` 仍没有 RequestOwner，在 mutex 内直接读 token；`SecureTokens.kt:27–32` 只有持久 epoch 能力，没有新的无密钥 descriptor 接口。故这些是必须实现的新契约，不是现有修复。原型 statusPage 明说“取得 session mutex 后先复核，再读取或使用凭证”；无认证引擎，不能动态证明该顺序。

### 2. unknown 创建与编辑稿：规范已闭环

01 §7.4–7.5 将不可变 CreateRecovery A 与可编辑 ComposerDraft B 分开，快照包含 client_note_id/text/recorded_at/timezone/source/asr；落盘失败不 POST。持久 sending 重启转 unknown；手动恢复重新捕获本进程 generation，但不改 operation/A。先查 live client_note_id，找到不 POST；未找到只允许明确原样重试 A。返回 ID 后还须 GET/sync 权威对账；B 只能明确 PATCH 当前 rev 或另存新 ID，409 保留 B。404/tombstone 不复活、不吞 B；移除本机恢复项明确不删服务器，linked B 转独立草稿。

服务端 `notes.py:48–97` 确实只按 user/client_note_id 幂等，不比较正文，并以 `new_id("nte")` 创建另一 server ID；已有软删对象仍可返回 ID，GET 会拒绝。`schemas.py:131` 已输出 client_note_id，Android `Api.kt` 的 Note 仍缺该字段；Mock 不能替代不同 ID 的测试。这些修订有真实 API 落点，无需新 endpoint。原型 recoveryCards/removeRecovery 展示独立 A/B、核对后权威 A、PATCH/另存和明确移除范围；但对编辑内容的模拟存在下述 P2，不能把点击演示当持久恢复测试。

### 3. 服务端确认与本机排程：规范已闭环

01 §5.3–5.4、§6.4、§9–11 分离 server suggested/scheduled/done/cancelled 与 local unknown/submitting/submitted_exact/submitted_inexact/failed/expired 等。只有实际系统调用成功返回才 submitted；scheduled/action/prefs 均不足以证明成功。不承诺准点送达；action 缺失保留 scheduled reminder fallback。POST 成功后 scheduler/fallback 抛错只标本机失败，本地重试不重复 confirm；后续 refresh 失败不改写已成功 mutation。POST/schedule 间死亡先 unknown，fresh sync 后恢复；新过期提醒不假报成功，修改未来时间是新的明确授权。既有 pending 的迟到恢复例外也明示，不等同于 prefs 是回执。

通知运行时权限、频道、精确闹钟能力独立；精确权限撤回后的 fallback 成功/失败分别落不同本地状态，失败/过期不因 confirm 成功主动请求通知权限。现有 `ReminderAlarms.kt:59–110` 返回 Unit，存在静默不排程、先写 prefs、fallback 抛错；`MainActivity.kt:170–175` 仍把 confirm/schedule/refresh 串在同一异常边界。规范正确要求新 LocalScheduleResult 和步骤结果拆分，不能简单包 Unit 后无条件返回成功。

原型确认层、成功卡和 alarmStateCards 已分轴写明服务端确认、本机非精确提交、通知未开，以及 unknown/failed/expired；失败按钮说明仅重试 AlarmManager。没有把服务端 scheduled 单独写成本机成功。它们都是人工样例，没有 POST 后失败或 refresh 失败的真实执行路径。

## 原 P2 disposition 核验

| 原发现 | 规范核验 | 原型及剩余边界 |
| --- | --- | --- |
| ASR 归属 | §4.2 固定 mode/owner/attempt/draft version，Permission/Processing 同口径；锁模式，任何路径切换即取消；A 回包不消费 B | listening 文案对应；JS 仍读可变 state.mode，无 VoiceAttempt 或取消仲裁。只能作为表达，不能据此验收 |
| 迟到 TTS | §6.3 单 AudioCoordinator，ASR 从 Permission 起停止 pending 并撤销 autoPlayCandidate；旧答不抢主动播报，后台/旋转不重播 | HTML 无音频；voice handler 不清 state.speaking。说明已采纳，状态模拟尚不完整 |
| 删除/刷新栅栏 | §7.3/7.6 单投影提交顺序、完整 refresh 原子应用、rev+tombstone+refreshId；旧全量不能删新卡；导航 ID，action 同 ID 新 payload 重消费 | HTML 无异步仓储，不能证明栅栏。实现必须把“最新 refreshId”与写入顺序一起落实，不能只编号就 replaceAll |
| 提醒可达/无障碍 | §3.2 待办/已结束/全部覆盖四种 server 状态；无 note_id 不渲染必 409 mutation；过期需改未来时间；divider 仅装饰，control-border/focus 承担功能 | reminderCards 有 suggested/scheduled/done/cancelled 与无来源说明；无来源无 mutation。可达控件存在，但真实键盘/焦点/字体/对比未测，见下 |

未发现这些规范条款与首版范围相互冲突。原 P2 不能因为不再持久业务缓存就删掉当前进程的删除栅栏、音频仲裁或无障碍验收。

## 剩余 P2：原型一致性，不是生产数据事故

以下均可从源码直接判断，无需声称点击或渲染测试。它们没有提升为 P1 的理由是：全局“设计演示/示例数据/不代表真实服务状态”、01 §14 与 03 明确限制原型用途；没有真实数据、凭证、网络、系统排程或音频副作用，规范又明确要求正确行为。**若将这些 handler 直接移植为产品逻辑，内容丢失、忙碌误提交或错误排程成功将重新构成 P1，不能以本次设计结论放行。**

1. **P2 — 输入/恢复/忙碌与离线模拟不兑现所写语义。** `prototype.html:650` recoveryCards 的 A/B 正文硬编码；`input` listener（:857）虽写 state.draft，但卡片不读取它。`send-text`（:876 起）不检验空白、NUL 或 busy，清 draft 后转固定“浇水/停车”示例；asking 时继续输入可跳去 organizing，offline 的 send-draft 也直接转成功。offline 下切 note/ask/reminder 筛选还会在 streamContent 中先命中样例卡，绕过“无已加载内容”分支。不能用这些路径证明 B 无损、网络串行或冷启动无缓存。后续原型应保留编辑值、模拟拒绝/保草稿，或在相应控件明确标为跳转预设样例。
2. **P2 — 提醒编辑值没有进入摘要/结果，部分既有操作未演示。** confirmSheet 的标题/时间接收 input，但摘要固定 2026-11-10 09:00，submit-confirm 无校验直接显示固定浇水成功；edit-future 对其他示例提醒也打开同一个浇水 Sheet。原型没有完成/取消/贪睡的实际操作控件，规范 §8/§15 保留它们，因此是原型覆盖不足，不是已获准删除操作。不能用该原型验收时间修改或既有操作保留。
3. **P2 — 键盘/焦点与音频状态只是规格，未落入原型。** JS 只有 click/input listener，没有 Escape/Ctrl+Enter/Tab 键处理、dialog 焦点进入/回还、focus trap 或背景 inert。原生 button 的 Enter/Space 不等于完整键盘支持；aria-modal 不会自行封闭背景按钮。背景模式按钮 handler 只改 mode，不取消听写；voice 不撤销 speaking。必须实现规范仲裁，或使示例背景真正不可交互。CSS 中多个 small-button/filter/mode/icon 目标为 36–44px，部分次按钮仍用低透明度边界；不能凭侧栏“48dp/TalkBack”文案声称可用性已证实。
4. **P2 — 场景说明索引错位。** scenarios 是五元素数组，render（:833–834）用 meta[4] 当说明、meta[5] 当真实能力映射；后者不存在，前者实际是能力映射。应以实际字段呈现，避免右侧说明为空或出现 undefined。此为原型展示缺陷，不是新增产品能力要求。

没有发现需增加聊天服务器、持久任务、自动队列或全离线缓存才能解决的阻塞，不要求扩大范围。

## 首个完整发布与实现验收出口

01 §15 保持完整发布单元：统一输入 → 原文保存 → 异步整理 → 建议提醒明确确认 → 本机真实排程结果；提问 → 本次答案 → 可用来源 → 原记事 → 手动重播/停止。还保留详情编辑/删除、搜索、所有提醒状态及完成/取消/贪睡、通知动作、boot 和授权迁移。§6/§7/§16 明确只存草稿/创建恢复，答案仅当前进程，无持久聊天/完整离线业务缓存/并行 HTTP/自动业务队列。Room 与新协调器是新增工作，data 仍为 JVM 模块；收口没有把首版变成仅换壳或删除原功能。

实现完成前至少应提交以下可重复证据（下列是验收要求，**本次均未执行**）：

| 测试 | 具体断言 |
| --- | --- |
| session mutex 可控调度 | 占锁、排入旧 create/ask/read，再登出/同用户重登，让旧操作后取锁；用 spy 断言 stale 后没有 store.read、refresh 或新代正文 HTTP，不仅是无旧卡。refresh 前及 fresh access 重试前插入换代，结果相同 |
| 条件提交与登出 | 在 DB 写锁前/事务提交前暂停，交错清库、迟到响应和先读后换代；只允许旧写被拒或先提交后清库，当前 owner 无旧草稿/UI/TTS/alarm；普通 refresh 不换 epoch、不清有效草稿 |
| unknown 创建 Real 契约 | server-id 与 client-id 必须不同。A 已落服务端但丢 202，进程死亡后 sending→unknown，编辑 B，手动对账/原样重试；仅一个 A，POST 每字段一致，服务器卡正文权威，B 保留。覆盖另一端 edit/delete、GET 404/tombstone、sync 先找到、双击/旋转、磁盘写失败无 HTTP、移除项保 B、PATCH 409 与明确另存 |
| 提醒分步骤故障注入 | POST 成功后精确调用失败/fallback 成功或失败、到达过期、后续 refresh 断网、POST/schedule 间死亡；保留 server scheduled，local 分别 inexact/failed/expired/unknown，无虚假 submitted、无重复 confirm。action 缺失走合法 fallback；权限/频道独立，失败时不请求通知权限 |
| 投影与通知/boot | 暂停旧全量 refresh，插入 create/edit/delete/organize/notification complete，再应用旧结果；不丢新卡、不复活删除；导航 A→B 迟到不覆盖 B；action 同 ID 更新 fire_at 确实重排。boot 断网先恢复 pending，不删既有项；保留终态二次确认、fresh rev、8 秒有界与 session/alarm fences |
| 音频与草稿 | Permission/Listening/Processing 下切模式、旋转、后台；旧 ASR 不误投。A 在途写 B 不吞字；TTS 初始化/pending 时启动 ASR 或迟到答案不播；主动播旧答案不被新答抢占，停止/旋转不重播，长文本不拆 surrogate |
| 完整 UI 与升级回归 | 真正操作两条端到端闭环、500/2000/5000 字边界、organized 但记忆尚未可搜、来源缺失/删除、全部提醒状态及无 note_id；冷启动离线只有当前 owner 草稿。既有 token/pending alarm prefs 升级不损；网络忙保文字、不排队 |
| Android 质量证据 | 既有单元/Repository 回归、MockWebServer 不同 ID 契约、Room 事务竞态、Compose semantics/UI、Android 构建；真实设备权限拒绝/撤回、通知频道、ASR/TTS、AlarmManager/boot、TalkBack、键盘焦点、fontScale 2.0、减少动画及功能边界对比 |

本轮执行的仓库检查仅为 `python3 scripts/check-invariants.py`，输出 `check-invariants: 0 error(s), 0 warning(s)`；它不是本设计或原型的功能验证。报告落盘后复测三文件 hash，与上表一致。

后续实现仍须独立评审与既有回归交叉验证，不能仅让新测试与新实现互相印证。本轮未实现应用、未跑构建、未完成设备/UI 验收；没有对 CI/QA 或上线作任何通过判断。
