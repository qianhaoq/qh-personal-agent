# 随记：一条连续的个人流（修订版）

> Kimi 统一交互设计 · 首个完整改版范围  
> 日期：2026-11-09；根据 `02-codex-design-review.md` 修订  
> 交付：`01-kimi-design.md` + `prototype.html`；逐项 disposition 见 `03-kimi-revision.md`  
> 阶段：设计契约修订；生产实现尚未开始，本阶段不改生产代码、服务端、CI 或依赖。  
> 原则：代码是当前能力的事实来源，历史文档只作背景，不把设想写成已支持能力。

---

## 0. 一句话与首版决定

**把「想到就说」和「随时问回自己的记忆」放进同一个安静的个人流：说过的内容、得到的答案、需要确认的提醒都能沿来源回到同一条记事。**

首版是一个完整的 Android 端到端发布单元，不是给旧导航换皮，也不是一个 S40 小补丁：

1. 首页改为单一“个人流”，记事、当前进程内的独立问答、提醒确认连续呈现。
2. 底部只有一个语音主入口，用户明确选择“记下”或“提问”；不增加任意意图路由器。
3. 记事保存后真实反映 `uploaded / organized / failed`；问答结果带来源；提醒建议在同一张来源卡中确认。
4. 保留文字输入、记忆搜索、记事详情/编辑/删除、全部提醒状态与完成/取消/贪睡、TTS、通知和 AlarmManager。
5. **采纳首版范围收口**：保留既有安全的网络串行机制；等待时允许浏览和编辑下一份本机输入，但不并行发送其他业务请求，也不自动排队。
6. 持久化只覆盖**用户文字草稿与创建恢复所需的不可变请求快照**。不做完整业务缓存，不做持久问答历史；进程重启后重新获取服务器记事/提醒。
7. 每次操作绑定登录会话，并在取得 session mutex 后、读取或使用凭证前再次授权；不是只在响应回来时检查。
8. 服务端提醒确认与本机系统排程是两个独立状态；`scheduled` 不再翻译成“本机已排程”。
9. 不新增服务端接口，不声称存在持久聊天、通用任务中心、后台问答查询或服务器任务取消。

**核心视觉隐喻不是聊天气泡，而是“纸面上的呼吸”**：暖白纸面、墨色文字、青绿色语音动作、少量陶土色待确认标记。内容永远比装饰先被看见。

---

## 1. 已核实的现状与不可误述的边界

### 1.1 真实客户端能力

| 事实 | 一手代码依据 |
| --- | --- |
| 当前 UI 是单 Activity + Compose Navigation，有 timeline/capture/ask/search/detail/edit | `android/app/src/main/kotlin/dev/qh/agent/MainActivity.kt:334-686` |
| 语音使用 Android 系统 `SpeechRecognizer`，中文、无 partial results，点击开始后停止并识别 | `VoiceAsr.kt:16-105` |
| 语音识别可能依赖系统语音服务与网络；不是已承诺的端侧离线 ASR | `VoiceAsr.kt:29-30,59-63,77-80` |
| 语音记事识别成功后直接保存；语音提问在非 busy 时自动提问 | `MainActivity.kt:199-213,307-311` |
| 答案是文字 + citations；首次回答可在前台自动播报，保留答案可手动重播 | `MainActivity.kt:616-679`、`AnswerTts.kt:5-9` |
| 停止播报只停止本机 TTS；停止听写只操作系统识别器 | `AnswerSpeaker.kt:75-103`、`VoiceAsr.kt:86-103` |
| 记事整理由客户端轮询 notes，最多 20 次 / 30 秒；没有任务进度百分比 | `OrganizePoll.kt:3-16`、`MainActivity.kt:271-305` |
| 提醒确认后，服务端响应 action，客户端 `ReminderAlarmScheduler.confirmed` 尝试排程并刷新；当前返回 Unit，不是本机排程回执 | `MainActivity.kt:170-175`、`ReminderAlarms.kt:59-66` |
| 精确闹钟不可用时尝试非精确 fallback；prefs 写入发生在系统调用之前，prefs 存在不等于系统排程成功 | `ReminderAlarms.kt:90-111` |
| 开机先恢复本地闹钟，再做最多 10 秒的 best-effort sync；断网不删本地闹钟 | `ReminderAlarms.kt:175-200` |
| 通知栏完成/取消需要确认，贪睡 10 分钟一键执行；顺序与 App 内一致 | `ReminderNotificationAction.kt:7-51` |
| 业务列表/答案主要是 ViewModel 内存状态；本地持久闹钟与凭证另有存储；当前没有 Room | `MainActivity.kt:70-125`、`ReminderAlarms.kt:33-47`、Gradle 依赖 |
| `RealRepository.authorized()` 在 session mutex 内读取凭证，锁覆盖整个 HTTP 调用；新设计必须增加请求 owner 前置复核 | `Repository.kt:65-101` |
| `SecureTokens.sessionEpoch()` 是持久登录会话标识；普通 refresh 不改变它 | `SecureTokens.kt:27-32` |

### 1.2 真实服务端契约

- 创建记事：`POST /v1/notes`，202 返回 `{note_id,status,rev}`；`client_note_id` 提供创建幂等。正文最多 5000 字。
- 幂等创建按 `(user_id, client_note_id)` 返回已有对象，**不比较正文，也可能返回已软删除对象**；所以恢复重试必须复用完整原请求并随后 GET/sync 对账。
- 记事读取/编辑/删除：`GET /v1/notes`、`GET /v1/notes/{id}`、`PATCH`（带 `rev`）、`DELETE`。正文变更会清派生物、重新整理，作废旧 suggested 提醒，不动已确认提醒。
- 问答：`POST /v1/ask`，同一 HTTP 请求返回答案与 citations；正文最多 2000 字。没有可供 Android 查询历史 answer WorkItem 的 API。
- 搜索：`GET /v1/search?q=&top_k=`，query 最多 500 字，返回 memory_id、可空 note_id、text、score。记忆异步同步，organized 不保证立即可搜。
- 同步：`GET /v1/sync?since=&limit=` 返回 notes/reminders/actions/deleted/cursor；删除墓碑只有 ID，没有可直接与 note.rev 比较的删除 rev。
- 提醒：`confirm / cancel / complete / snooze` 均带 `rev`；snooze 当前固定传 10 分钟。无 note_id 的提醒在 mutation 入口会 409，不能渲染保证成功的操作。
- `note.status` 使用 `uploaded / organized / failed`；`memory_status` 和 WorkItem 进度没有通过 Android `Note` DTO 暴露，**不设计“记忆写入 80%”之类状态**。
- 无相关记忆时答案为“我没有相关记录。”且 citations 为空；这是有效答案，不是网络错误。

主要依据：`server/app/api/notes.py`、`reminders.py`、`ask.py`、`sync.py`，`server/app/schemas.py:131-173,208-251`，`server/app/workers/answer.py:11-69`，以及 `android/data/src/main/kotlin/dev/qh/data/Api.kt:53-78`。

### 1.3 本设计不借用的历史承诺

历史 v2 文档中的端侧 sherpa、离线自动整理、小组件、画像管理、每日摘要、任意 actions、utterances 路由等不等于当前能力，均不进入本首版。

---

## 2. 体验原则

1. **先放下，再整理。** 保存原文与得到整理结果分开表达。
2. **一个对象，一个位置。** 编辑、整理、建议提醒都更新原记事卡，不制造一连串系统消息。
3. **问答有出处。** 答案先读，来源后查；引用可回原记事。没有来源就明说没有相关记录。
4. **只问一个需要决定的问题。** “待确认”是需要做决定的注意力区，其余结果安静留在流中。
5. **语音优先，文字始终平权。** 不用麦克风也能完成记事与提问；语音失败不回丢已写文字。
6. **停止什么就写什么。** “停止听写”“停止播报”“关闭结果”绝不写成“取消任务”。
7. **不制造确定性。** 无百分比、无“即将完成”、无伪造在线状态、无后台持久任务假象，也不把 prefs 或 `scheduled` 当本机排程回执。
8. **少概念。** 用户只看到记事、答案、提醒；Thread/Ticket/Worker/rev/action 不进入普通 UI。
9. **不丢新文字。** 已发送快照与继续编辑的草稿分离，任何迟到回包都不能清掉另一版输入。

---

## 3. 信息架构与页面层级

### 3.1 两个层级，不设传统三 Tab 首页

```text
登录（未登录）
└─ 邀请码输入 → 既有 login

个人流（唯一主页面）
├─ 顶部：随记 / 搜索 / 同步与权限状态
├─ 注意力区：待确认提醒（仅存在时出现）
├─ 连续内容流
│  ├─ 记事卡（整理结果 + 有关联提醒内嵌）
│  ├─ 问答卡（当前进程内的问题 + 答案 + 引用 + 播报）
│  ├─ 本机文字草稿（未发送，不冒充服务器记事）
│  └─ 创建恢复项（结果未知，明确服务器可能已保存）
├─ 流筛选：全部 / 记事 / 问答 / 提醒
│  └─ 提醒内二级筛选：待办 / 已结束 / 全部
└─ 底部统一输入坞
   ├─ 模式：记下 / 提问
   ├─ 主动作：语音
   └─ 次动作：键盘文字

覆盖层 / 二级页
├─ 听写层（权限 → 听写 → 识别）
├─ 文字输入层（与启动时的模式一致）
├─ 记事详情 → 编辑原文 / 删除 / 关联提醒
├─ 提醒确认层（标题 + 本地时间 + 确认）
├─ 记忆搜索（独立检索，不生成持久聊天）
└─ 状态与权限说明 / 退出登录
```

### 3.2 提醒在同一流中筛选，不新建任务中心

有关联提醒嵌于原记事卡；“提醒”筛选提供所有 `suggested / scheduled / done / cancelled` 的独立列表，因此已结束提醒始终可达。无 note_id 的提醒也在这里显示：

- 标为“来源记事不可用”。
- 因现有服务端 mutation 会 409，不显示确认/完成/取消/贪睡等保证成功的按钮，只提供同步与状态说明。
- 空态分别为“暂无待办提醒”“暂无已结束提醒”“暂无提醒”，并可切到另外两个筛选。

### 3.3 默认排序

- 注意力区：`suggested` 按 `fire_at` 升序；无 fire_at 的排后。过去的 suggested 仍显示，进入确认后必须改成未来时间。
- 个人流：记事按 `recorded_at`，当前进程问答按创建时间，新的在前；本机草稿与待对账创建恢复项固定在最前。
- 提醒状态变化不移动原记事的位置，避免用户刚要点确认卡片就跳走。
- 搜索结果是临时二级页，不插入个人流。

---

## 4. 主界面与统一输入

### 4.1 首页结构

**顶部**只保留产品名、搜索和状态入口。问候语最多两行，不使用营销 dashboard。

**内容卡**：

- 记事卡：时间 + 原文/标题 + 标签/待办摘要 + 真实整理状态。
- 问答卡：小号“你问”与问题，下面直接是答案；底部“来源 N 条 / 播报”。只在当前进程保留。
- 草稿卡：明确“仅保存在本机”，主按钮“发送”。
- 创建恢复卡：明确“保存结果未知，服务器可能已保存”；不能与“确定未发送”共用文案。

**输入坞**固定在底部但不遮内容。实际标签为 **“点一下说话”**，不用“按住说话”，因为当前 `VoiceAsr` 是 start/stop。

### 4.2 一次听写与提交的固定归属（P2 采纳）

每次语音尝试创建不可变 `VoiceAttempt`：

```text
attemptId + mode(note|ask) + ownerKey + loginEpoch
+ targetDraftId + targetDraftVersion
```

- 开始权限流程即捕获 mode/owner/epoch/draft version；Permission、Listening、Processing 都沿用同一尝试。
- **产品决定：听写期间模式锁定；若通过任何路径切换模式，则取消本次尝试。** 结果回调必须同时匹配 attemptId、mode、owner/epoch 和 draft version，否则只丢弃回调，不误投。
- 权限尚未返回、Processing 中切模式、旋转或后台返回均遵守同一规则；旧 recognizer generation fence 继续保留。
- 开始语音尝试时停止当前 TTS 与 pending utterance，并撤销所有在途 ask 的自动播报资格。
- 每次提交生成不可变内容快照与 `submittedDraftVersion`；只有当前 composer 仍是同一 draftId+version 时才消费草稿。A 在途时继续输入 B，A 的回包绝不清空 B。

### 4.3 记下模式

1. 点语音 → 即时请求麦克风权限（若未授权）→ 听写层。
2. 点“结束并识别” → 系统识别；点“取消听写”只放弃本次识别。
3. 识别成功且当前没有其他网络操作 → 先持久化完整不可变 `NoteCreate` 快照，再调用既有 create。
4. 若另一网络操作仍在进行 → 不排队、不自动发送；把识别文字保留为本机草稿，提示“上一操作结束后请手动保存”。
5. 202 只表示创建已接收；随后用 GET/sync 获取真实正文和状态，对账后才显示服务器记事卡。重放 create 可能直接返回 organized/failed，不能一律改回 uploaded。
6. 整理完成后同卡更新标题、tags、todos、category 和建议提醒。
7. 识别失败保留此前输入，提供“重试听写”和“改用文字”。

文字路径：输入 1–5000 字，主按钮“保存记事”。只有对应版本草稿已安全消费，或本机草稿落盘成功后，才清空对应编辑内容。

### 4.4 提问模式

1. 同一语音入口获得文字后，在无其他网络操作时调用 `POST /v1/ask`；否则保留问题草稿，不自动排队。
2. 等待卡片写“正在查找记忆并回答…”；可浏览和编辑下一条，但不能并行发起另一业务请求。
3. 每次提问独立；不把上一问答自动作为下一问上下文。
4. 回答成功后生成当前进程的问答卡；只有仍具自动播报资格的那次回答可自动播报。
5. 引用以“来源 N”编号；有 note_id 时可打开记事。无引用时说明没有可打开来源。
6. 答案是“我没有相关记录。”时给“去搜记忆”入口，不作为异常。

### 4.5 网络串行是明确产品契约

- 保留 `RealRepository` 的进程级 session mutex 与 refresh 串行化；不删除锁来制造“并发”。
- 首版最多一个 repository 网络操作在途，包括 create、ask、sync、提醒 mutation；不承诺零 mutex 等待。
- UI 允许等待期间阅读当前内存内容、编辑下一草稿、管理 ASR/TTS；新提交若遇到占用，不自动排队，而是保留文字并提示稍后手动提交。
- 同一对象防重复提交；不同对象也不能绕过全局网络串行。
- 后台 boot/notification 工作仍沿用既有有界执行与 fences，并与前台共享 session/alarm 协调。

### 4.6 为什么不是自由意图路由

“明天提醒我…”仍先作为记事保存，由既有整理产生建议提醒，用户明确确认或选择“10 分钟后提醒”后才授权排程。输入模式只有用户显式选择的“记下 / 提问”，不会猜一句话应该创建、删除还是修改提醒。

---

## 5. 代表性页面规格

### 5.1 个人流：有内容

- 首屏能看到一个待确认提醒和至少一条最近内容；语音按钮无需滚动即可触达。
- 建议提醒卡显示标题、本地日期时间、“服务端建议 · 尚未确认”，主按钮“确认时间”。
- 不在 suggested 状态写本地闹钟，也不把服务器 `scheduled` 单独翻译成本机排程成功。
- 卡片正文最多预览 4 行；长答案默认 8 行并可展开，大字体下仍可操作。

### 5.2 记事详情

顺序：返回 → 标题/状态 → 原文 → 记录时间 → 标签/分类/待办 → 关联提醒 → 编辑/删除。

- 不显示 rev；rev 仅作为请求数据保留。
- `uploaded`：原文已保存，整理尚未完成；提供“同步”，不提供“重新整理”。
- `failed`：原文仍在，整理失败；可“编辑并重新保存”。同文 PATCH 不重启整理，因此没有“重试整理”按钮。
- 删除：二次确认保留“相关提醒也会删除，此操作不可撤销”；成功后才移除卡片并取消相关闹钟。
- 导航 A→B 使用 navigationRequestId；A 的迟到 GET 不得覆盖 B。

### 5.3 提醒确认：服务端与本机双轴

Sheet 包含来源、标题（最多 256 字）、Material 日期/时间选择器和摘要。产品决定采用 Material 控件，用户不手写 ISO；客户端转换为既有 `fire_at`。

主按钮为“确认提醒”。确认期间禁止重复提交，关闭手势不等于撤销请求。结果分两步：

1. **服务端确认**：POST 成功且 fresh reminder 为 `scheduled` → “服务端已确认”。
2. **本机排程**：使用响应 action，或 action 缺失时使用响应中的 scheduled reminder fallback，调用系统排程并获得明确本地结果。

只有系统 AlarmManager 调用实际返回且未抛错，才显示：

> 服务端已确认 · 本机已提交系统排程（精确 / 非精确）

“已提交系统排程”不代表系统保证准点送达。通知权限、通知频道开关和精确性另外显示。刷新失败不会把已成功的 confirm 改写成确认失败。

### 5.4 本机排程异常页内状态

| 双轴状态 | 用户文案 | 操作 |
| --- | --- | --- |
| server=scheduled；local=unknown | “服务端已确认 · 本机排程待核对” | 通过 fresh sync/action 或有效 pending payload 核对；不靠缓存推断成功 |
| server=scheduled；local=submitting | “服务端已确认 · 正在提交本机排程” | 防重复本地调用 |
| server=scheduled；local=submitted_inexact | “服务端已确认 · 已提交非精确排程” | 查看权限说明 |
| server=scheduled；local=failed | “服务端已确认 · 本机排程失败” | “重试本机排程”，只重试本地，不重复 POST confirm |
| server=scheduled；local=expired | “服务端已确认 · 提醒时间已过，本机未排程” | “修改为未来时间”，这是新的明确修改，不冒充本机重试 |

- 网络往返后 fire_at 已过期且没有既有 pending alarm：不设闹钟，标 expired。
- 既有 pending alarm 的迟到恢复沿用现有系统行为；但 prefs 本身不当作成功回执。
- 进程在 POST 与 schedule 之间死亡：重启后先显示 unknown，fresh sync 后尝试本地恢复；不重新 confirm。
- 通知拒绝/频道关闭：显示“系统通知可能不显示”，不改变 server 或 local submitted 状态。
- 精确权限撤回竞态且 fallback 成功：标 submitted_inexact；fallback 也失败则 failed。

### 5.5 记忆搜索

- 搜索框提示“搜索已同步的记忆”，最多 500 字。
- 结果显示 memory text；相关度不解释为置信度。
- note_id 存在时显示“查看原记事”；为空时显示“来源记事不可用”，无死按钮。
- 空结果：“没有找到相关记忆。可以试试更短的关键词。”

### 5.6 空、离线、错误与权限

- 空态主按钮“说第一句”，次按钮“用文字记录”。
- **首版没有持久业务缓存**。冷启动离线时显示“当前离线，无法读取服务器记事和提醒；本机草稿仍可编辑”，不展示上一进程的 Note/Answer。
- 当前进程已经加载的内容可在短暂断网后继续阅读，标为“本次打开时已加载，可能不是最新”；同步失败不当成空列表。
- 确定尚未发送的 local_only 草稿与已经发出但结果未知的 create recovery 使用不同卡形和文案。
- 错误附着在对应卡或 Sheet，不用全局错误盖住所有内容。
- 麦克风拒绝后提供完整文字路径，不循环自动弹权限。
- TTS 不可用时保留完整文字答案。

---

## 6. 状态、连续性与“停止”的准确含义

### 6.1 四类工作必须分开

| 用户动作 | 实际影响 | 绝不代表 |
| --- | --- | --- |
| 结束并识别 | 停止系统 listening，等待 ASR result | 已保存记事 / 已提问 |
| 取消听写 | cancel + destroy 当前 recognizer，作废旧回调 | 取消服务器整理或问答 |
| 停止播报 | `AnswerSpeaker.stop()` 停本机 TTS | 删除答案 / 取消 `/v1/ask` |
| 关闭卡片或返回 | 改变本机可见状态；请求是否仍在当前进程由真实协程决定 | 服务器 WorkItem 已取消 |

首版不提供“取消服务器任务”按钮，因为没有对应 API。

### 6.2 整理状态

- `uploaded` + 正在轮询：“已保存 · 正在整理”。
- `uploaded` + 30 秒轮询窗口结束：“已保存 · 整理尚未完成”，提供“同步”，不自动改称失败。
- `organized`：显示整理产物。
- `failed`：显示整理失败，原文仍在。
- 进入后台不承诺轮询继续；下次前台通过既有 sync/notes 获取真实状态。

### 6.3 问答状态与音频仲裁（P2 采纳）

- `asking` 是当前 HTTP 请求的本机状态，不是持久后台进度。
- 首版最多一个 ask 在途。离开再返回可观察同一请求；进程死亡后不展示永久转圈。
- 问题文字可持久化为草稿并标记“上次提问结果无法恢复”；**答案与 citations 只保留在当前进程**，重启不恢复历史问答卡。
- `/v1/ask` 没有客户端幂等键，网络超时/5xx 后不自动重试；用户明确点“重新提问”才发起新请求。401 后现有 refresh+单次重试属于鉴权协议，不等于业务网络超时自动重试。
- 单个 `AudioCoordinator` 管理 ASR/TTS：开始 ASR（包括 Permission）停止 TTS 与 pending；Permission/Listening/Processing 时到达的答案只保留文字，不自动播。
- 自动播报还需同时满足：App RESUMED、对应当前 autoPlayCandidate、未开始更新语音尝试、没有用户主动播报其他答案。乱序旧回答不得抢占。
- 退后台停止播报；旋转不自动重播；恢复历史/旧卡永不自动播。保留中文不可用、长文本分块与 UTF-16 surrogate 不拆分行为。

### 6.4 提醒状态是二维，不再合成“已排程”

**服务端轴**：`suggested / scheduled / done / cancelled`。  
**本机轴**：`not_applicable / unknown / submitting / submitted_exact / submitted_inexact / failed / expired`。

| 组合 | UI 摘要 | 可执行动作 |
| --- | --- | --- |
| suggested + not_applicable | “待确认 · 尚未确认” | 确认、完成、取消、10 分钟后提醒 |
| scheduled + unknown | “服务端已确认 · 本机排程待核对” | 本地核对/同步 |
| scheduled + submitted_exact/inexact | “服务端已确认 · 已提交系统排程” | 完成、取消、贪睡；可复用 confirm 修改 |
| scheduled + failed | “服务端已确认 · 本机排程失败” | 仅本地重试；其他既有提醒操作 |
| scheduled + expired | “服务端已确认 · 时间已过，未排程” | 明确修改未来时间；其他既有提醒操作 |
| done/cancelled + not_applicable | 已完成/已取消 | 查看 |

suggested 的“贪睡”在 UI 中前置写为 **“10 分钟后提醒”**。这是用户主动授权服务端 scheduled 与后续本机排程，不偷换为单纯推迟卡片。

---

## 7. 端侧投影、授权与最小持久化

### 7.1 权威来源与首版存储边界

- **服务端权威**：Note、Reminder、整理结果、提醒服务端状态与 Channel actions。
- **当前进程投影**：notes/reminders 列表、已接收 answer/citations、ASR/TTS、筛选、导航和本地排程结果。
- **首版持久化**：当前文字草稿、创建恢复所需的不可变完整请求快照。没有 note/reminder 业务缓存，没有 ask history。
- **既有本机闹钟 prefs**继续用于恢复，但不是服务器提醒或系统排程成功的证明。
- SpeechRecognizer 的音频处理由系统服务决定；本设计不新增音频文件存储，也不宣称音频绝不离机。

### 7.2 会话身份与提交前授权（P1 修复契约）

定义不可变提交身份：

```text
RequestOwner:
  operationId (UUID)
  runtimeKey (real|mock + 当前服务配置标识)
  userId
  loginEpoch (SecureTokens 的持久 epoch)
  processGeneration (AppRepositories 的进程内 fence)
```

`runtimeKey + userId` 组成 `ownerKey`；同一用户重新登录也因 loginEpoch 不同而不继承旧操作授权。

所有 Repository 公开操作都必须接收 `RequestOwner`，包括 create、ask、sync、search、note read/patch/delete 和提醒 mutation。`authorized(owner)` 的顺序固定为：

1. 用户提交时从当前已认证 `SessionDescriptor` 捕获 owner；登出封闭后禁止生成新 owner。
2. 进入 `sessionMutex.withLock`。
3. **取得锁后，先读取不含 access/refresh token 的当前 SessionDescriptor，逐项比较 runtimeKey、userId、loginEpoch 与 processGeneration。**
4. 只有完全匹配才调用 `store.read()` 获取凭证并发起 HTTP；不匹配抛 `StaleRequestOwner`，不得读取旧操作所需的新 token，不得发请求。
5. 401 refresh 路径在使用 refresh token 前再次复核同一 owner；刷新后使用新 access token 重试前再复核。普通 refresh 保留 loginEpoch，不清有效当前投影。
6. 响应应用、数据库写入、TTS、alarm 仍需 owner/fence 检查；这些是第二道防线，不能替代第 3 步的凭证使用前授权。

实现需在会话层提供不含密钥的 descriptor 读取，不能通过“先读 Tokens 再检查”宣称满足。进程重启不以重置后的 AtomicLong 判断旧数据归属，持久记录仍由 runtimeKey+userId+loginEpoch 限定。

### 7.3 登出与本地写入的有序条件提交

- 登出开始：立即封闭新提交、bump persistent loginEpoch 和 process generation、作废旧 ASR/TTS/操作；取消协程只是辅助。
- 本地投影写入统一经过 `ProjectionCoordinator` 的 session 写锁；登出清库也走同一写锁。
- 每次异步写入取得写锁后重新读取 descriptor，并在**同一数据库事务**中以 `ownerKey + loginEpoch + operationId + expectedDraftVersion` 为条件提交。
- 若写锁取得时发现会话已变，直接拒绝；若写入已在登出前完成，登出随后清库。禁止“先检查 epoch → await DB → 无条件写回”。
- 启动读取只返回当前 runtimeKey+userId+loginEpoch 的草稿/恢复快照；缓存先读后发生会话失效时不得显示。
- 登出清空所有本地草稿/恢复快照、当前投影、TTS 和闹钟；不向新会话重放旧操作。

### 7.4 首版私有存储模型

产品决定：实现阶段在 **Android app 模块或独立 Android 存储模块**使用应用私有 Room；不把现有 JVM `data` 模块改成 Android 模块。只设两类记录：

```text
ComposerDraft:
  draftId + ownerKey + loginEpoch + mode + text + version
  + linkedCreateOperationId? + askOutcomeUnknown?

CreateRecovery:
  operationId + ownerKey + loginEpoch
  + immutable NoteCreate 全字段快照
    (client_note_id,text,recorded_at,timezone,source,asr)
  + state(sending|unknown|rejected) + discoveredNoteId?
```

继续 `allowBackup=false`；Room 私有不等于加密，不记录正文日志。存储写失败时不发送创建请求，保留当前内存文字并显示“无法保存恢复信息，尚未发送”。

### 7.5 不可变创建快照与对账（P1 修复契约）

**`local_only` 与 `CreateRecovery` 是两个实体。** local_only 可自由编辑/删除；一旦提交，CreateRecovery 永远保存原始 A 快照，后续编辑写入新的 ComposerDraft B，绝不改写 A 的 client_note_id 或正文。

状态语义：

| 状态 | 含义 | 文案 |
| --- | --- | --- |
| local_only | 尚未发起 create | “仅保存在本机 · 尚未发送” |
| sending | 快照已落盘，请求已发出或在当前进程执行 | “正在保存原文…” |
| unknown | 网络中断、超时、5xx、进程死亡等导致结果不确定 | “保存结果未知 · 服务器可能已保存原文 A” |
| rejected | 收到能确定未接受创建的响应，如本地校验或明确 422 | “本次创建未被接受” |

**恢复流程：**

1. 重启只恢复当前 owner+epoch 的记录；旧会话记录不自动重放。持久记录中的 sending 在进程恢复时一律转为 unknown，不能继续显示为仍在发送。
2. 重启后的手动对账/重试是一个新的用户授权动作：重新捕获**当前** processGeneration，继续绑定同一 CreateRecovery.operationId 与原 A 快照；不得直接沿用旧 RequestOwner 发送。
3. 用户点“核对原记事”后，先 full sync/notes 查找服务端已有且匹配 `client_note_id` 的 live Note；Android DTO 补上服务端已有字段。
4. 若找到，直接 GET 当前对象，不重复 POST。
5. 若未找到，只允许用户明确重试**逐字段完全相同的 A 快照**。双击、旋转和重启都不能产生第二个 operation 或修改 A。
6. create 返回后，无论 202 状态如何，都 GET/sync 对账真实 note_id、client_note_id、正文、status、rev；只有权威读到的正文才标“服务器已保存”。
7. 若 A 已 live：原子合并 CreateRecovery 与 NoteItem。若用户另有 B，B 保留为本机草稿，并提供两个明确选择：
   - “用 B 更新此记事”：展示服务器 A 与 rev，用户确认后走既有 PATCH(rev,text=B)；409 则刷新并继续保留 B。
   - “将 B 另存为新记事”：新的用户动作生成新的 client_note_id 与新快照；不偷偷换 A 的 ID。
8. 若 create 返回已有 ID 但 GET 404/sync 为 tombstone：视为原对象已删除或不可用，不复活缓存、不清 B；只允许用户明确另存新记事或移除恢复项。
9. 若对账网络失败：保持 unknown，不显示“确定未发送”，也不清任何文字。
10. 删除 unknown 恢复项的动作叫“移除本机恢复项”，确认文案明确“不会撤销或删除服务器可能已经保存的 A”； linked B 自动转为独立 local_only 草稿，不随 A 被移除。
11. 若用户要先删除服务器 A，必须先对账到 live 对象，再另走现有 DELETE 二次确认；不能用移除恢复项替代。

### 7.6 投影提交顺序与删除栅栏（P2 采纳）

- 同一 owner 内所有 repository→projection 应用经单一 `ProjectionCoordinator`，并沿用 `refreshMutex` 串行完整 refresh；批量 notes/reminders/deleted 在同一 UI/本地事务中应用。
- 每次 refresh 有单调 refreshId；只允许最新代次全量替换。旧全量响应不能清除之后创建的卡，也不能把同步失败当空列表。
- live Note 按 rev 拒绝较旧对象；删除另以当前会话 tombstone 集合 + refreshId 栅栏拒绝删除前快照，**不只比较 rev**。
- create 对账、edit、delete、confirm 与通知 complete 的本地应用都进入同一协调顺序；session/alarm consume 锁不削弱。
- 相同 alarm action.id 的 payload 更新必须再次消费并按最新 reminder rev/fire_at 重排，不做永久 action-id 去重。
- 无持久业务缓存，因此首版没有跨进程缓存覆盖问题；当前进程仍执行上述规则。

---

## 8. 首版动作 / 状态 → 现有实现与 API 全量映射

所有网络行都隐式包含 §7.2 的 RequestOwner 提交前授权和 §7.6 的有序应用。

| 首版动作或状态 | 复用的真实函数 / 数据 | 网络或本地效果 | 备注 |
| --- | --- | --- | --- |
| 邀请码登录 | `AgentModel.login` / `RealRepository.login` | `POST /v1/auth/login` | 创建新 loginEpoch，封闭旧 owner |
| 退出 | `AgentModel.logout` + epoch/generation bump | `POST /v1/auth/logout` + 本地清理 | 登出开始即拒绝旧/新提交 |
| 开始语音 | `VoiceAsr.start` + VoiceAttempt | 系统 SpeechRecognizer | 固定 mode/owner/epoch/draft version |
| 结束听写 | `VoiceAsr.stop` | 系统识别 | 不发业务 API |
| 取消听写/切模式 | `VoiceAsr.cancel/destroy` | 仅本机 | 旧回调不误投 |
| 保存语音/文字记事 | `noteDraft`、不可变快照、`Repository.create` | 先本地落盘，再 `POST /v1/notes` | 5000 字；网络串行，不自动排队 |
| 本机未发送草稿 | `ComposerDraft` | 仅客户端持久化 | 与 unknown 明确区分 |
| 创建结果未知 | `CreateRecovery` | 无自动网络重试 | 保留完整 A，不影响新草稿 B |
| 核对/重试创建 | full sync/notes + 原 A create + GET | 既有 GET/POST | 真实正文对账，tombstone 不复活 |
| 将 B 更新到已发现 A | `Repository.patch(id,currentRev,B)` | `PATCH /v1/notes/{id}` | 用户明确选择；409 保 B |
| 将 B 另存新记事 | 新 NoteCreate/client_note_id | 新 `POST /v1/notes` | 不复用 A 身份 |
| 查看流/详情 | `notes/note/sync` | `GET /v1/notes`、`/{id}`、`/v1/sync` | 当前进程投影，无持久业务缓存 |
| 编辑记事 | `Repository.patch(id,rev,text)` | `PATCH /v1/notes/{id}` | rev 409 后刷新，保留编辑文字 |
| 删除记事 | `Repository.delete` | `DELETE /v1/notes/{id}` | 二次确认；本地取消关联闹钟 |
| 整理中/完成/失败 | `Note.status` | 创建/同步/轮询可得 | 重放 create 不强制回 uploaded |
| 整理观察超时 | `shouldContinueOrganizePoll` | 停止本机轮询，可手动 sync | 不等于整理失败 |
| 语音/文字提问 | `askBody`、`Repository.ask` | `POST /v1/ask` | 2000 字；一次一个在途 |
| 回答等待/成功 | 当前进程 AskItem / `Answer` | 同请求 HTTP | 不持久化聊天历史 |
| 进程重启后的问题 | ComposerDraft.askOutcomeUnknown | 无查询 API | 只恢复问题，不恢复答案 |
| 打开答案来源 | Citation.note_id + navigationRequestId | `GET /v1/notes/{id}` | 迟到 A 不覆盖 B |
| 播报/重播答案 | `AnswerSpeaker` + AudioCoordinator | 系统 TTS | ASR/后台/旧回答不自动播 |
| 停止播报 | `AnswerSpeaker.stop` | 仅本机 TTS | 不取消 ask |
| 搜索记忆 | `Repository.search` | `GET /v1/search` | 500 字，top_k=10 |
| 提醒建议 | `ReminderDto(status=suggested)` | sync 获取 | 未确认，不造 alarm |
| 修改并确认提醒 | `confirmOverrides` + `confirmReminder` | `POST /v1/reminders/{id}/confirm` | rev + fire_at/title；过期要改未来 |
| 服务端已确认 | `ReminderConfirmResponse.reminder.status=scheduled` | 服务端结果 | 不等于本机已排程 |
| 本机排程提交结果 | 新本地 `LocalScheduleResult` 包装既有 scheduler | AlarmManager 实际调用结果 | action 缺失用 scheduled reminder fallback |
| 本机排程失败重试 | fresh action/reminder 或已确认响应 | 仅本地 AlarmManager | 不重复 confirm |
| 完成提醒 | `completeReminder` + 二次确认 | `POST .../complete` | 成功后取消闹钟 |
| 取消提醒 | `cancelReminder` + 二次确认 | `POST .../cancel` | 成功后取消闹钟 |
| 10 分钟后提醒 | `snoozeReminder(...,10)` | `POST .../snooze` | scheduled 才尝试重排 |
| 查看提醒/已结束 | 统一流提醒筛选 | 无网络 | 包含无 note_id；无来源不显示 mutation |
| 通知完成/取消/贪睡 | `performConfirmedReminderNotificationAction` | 先 sync 取 fresh rev，再对应 POST | 保留确认和 fences |
| 通知点击 | 现有 MainActivity PendingIntent | 打开 App | 首版不承诺精确定位 reminder |
| 开机恢复 | `ReminderBootReceiver` / `scheduler.restore` | 本地恢复 + best-effort sync | prefs 不是成功回执 |
| 手动同步 | `AgentModel.reload/refresh` | 既有 sync + notes + alarm consume | 不重跑所有失败任务 |
| 权限说明 | Android 系统设置/权限 UI | 仅本机 | 服务端、排程、通知、频道、精确性分开 |

**没有任何首版动作依赖未存在的服务端 endpoint。**

---

## 9. 状态矩阵

| 对象 | 状态 | 首要文案 | 主操作 | 恢复/禁止 |
| --- | --- | --- | --- | --- |
| App | 首次无内容 | “这里会留下你说过的事” | 说第一句 / 文字 | 不混入示例 |
| App | 冷启动离线 | “无法读取服务器内容；本机草稿可用” | 编辑草稿 / 重试同步 | 不展示持久业务缓存 |
| App | 当前进程有内容后断网 | “内容可能不是最新” | 阅读 / 存草稿 | 失败不当空列表 |
| App | 401/owner 失效 | “会话已变化，请重新登录” | 重新登录 | 旧请求不得读取新凭证 |
| Network | 另一操作在途 | “上一操作尚未结束；文字已保留” | 稍后手动提交 | 不自动排队、不并行写 |
| ASR | Permission | “需要麦克风才能听写” | 授权 / 用文字 | mode/owner 已固定 |
| ASR | Listening | “正在听…点一下结束” | 结束并识别 / 取消 | 切模式取消；退后台取消 |
| ASR | Processing | “正在识别…” | 取消听写 | 无伪实时文字；结果校验 attempt |
| ASR | 无匹配/超时 | “未听清内容” | 重试 / 用文字 | 不清已有草稿 |
| ASR | 引擎/网络不可用 | “语音服务暂不可用” | 用文字 | 不承诺离线识别 |
| Draft | local_only | “仅保存在本机 · 尚未发送” | 发送 / 编辑 / 删除 | 不等于 unknown |
| Create | snapshot persist failed | “无法保存恢复信息，尚未发送” | 保留文字 / 重试 | 不落盘就不发 POST |
| Create | sending | “正在保存原文 A…” | 可继续编辑 B | A 快照不可变 |
| Create | unknown | “结果未知，服务器可能已保存 A” | 核对原记事 | 不标未发送；不丢 B |
| Create | rejected | “本次创建未被接受” | 编辑/重试 | 仅确定拒绝使用 |
| Create | live A + edit B | “已找到服务器原文 A；另有修改稿 B” | 更新 A / 另存 B | 不按 B 合成 A 成功卡 |
| Create | deleted/tombstone A | “原记事不存在或已删除” | 另存 B / 移除恢复项 | 不复活、不清 B |
| Create | remove unknown local | “只移除本机恢复项” | 二次确认 | 不表示服务器撤销 |
| Note | uploaded+polling | “已保存 · 正在整理” | 查看原文 | 可继续编辑下一输入 |
| Note | uploaded+timeout | “整理尚未完成” | 同步 | 不推断 failed |
| Note | organized | 标题/标签/待办 | 查看详情 | 不保证记忆立即可搜 |
| Note | failed | “原文已保存，整理失败” | 编辑并重新保存 | 无同文重试整理 |
| Note edit | 409 | “内容已在别处更新” | 查看最新 / 保留我的文字 | 不覆盖新 rev |
| Note delete | 请求中 | “正在删除…” | 无 | 成功前不提前移除 |
| Ask | asking | “正在查找记忆并回答…” | 浏览/编辑下一条 | 不并行 ask、不自动重试 |
| Ask | answered | 答案正文 | 来源 / 播报 | 当前进程保留 |
| Ask | no memories | “我没有相关记录。” | 去搜记忆 | 正常答案 |
| Ask | failed/unknown | “这次没有拿到答案” | 明确重新提问 | 不显示旧答案 |
| Ask | process lost | “上次回答无法恢复，问题仍在草稿” | 手动提问 | 无历史 WorkItem 查询 |
| TTS | Speaking | “正在播报” | 停止播报 | 新 ASR 停止并撤销自动播 |
| TTS | Unavailable | “请阅读文字答案” | 无 | 不阻塞阅读 |
| Reminder | suggested | “待确认 · 尚未确认” | 确认时间 | 不排程 |
| Reminder | suggested 已过期 | “建议时间已过” | 改为未来时间 | 不只提示同步 |
| Reminder | scheduled+unknown | “服务端已确认 · 本机排程待核对” | 核对本机排程 | 不靠 prefs/缓存推断 |
| Reminder | scheduled+submitting | “正在提交本机排程” | 无 | 防重复 |
| Reminder | scheduled+submitted | “已提交系统排程（精确/非精确）” | 完成/取消/贪睡 | 不保证准点送达 |
| Reminder | scheduled+failed | “服务端已确认 · 本机排程失败” | 重试本机排程 | 不重复 POST confirm |
| Reminder | scheduled+expired | “时间已过，本机未排程” | 修改未来时间 | 不把过期写成功 |
| Reminder | done/cancelled | 已结束状态 | 查看 | 提醒筛选可达 |
| Reminder | note_id 为空 | “来源不可用，操作暂不可用” | 同步 | 不渲染必 409 动作 |
| Reminder mutation | 409/404 | 按原因显示版本冲突/不存在/时间过期 | 同步或改时间 | 不一律“同步后重试” |
| Search | no result | “没有找到相关记忆” | 修改关键词 | 不自动生成答案 |
| Notification | 权限拒绝/频道关闭 | “系统通知可能不显示” | 去设置 | 不改变 scheduled/local submitted |
| Exact alarm | 不可用 | “已提交非精确排程” | 了解 | fallback 失败另标 local failed |

---

## 10. 生命周期与权限行为

### 10.1 Android 生命周期

- `ON_PAUSE/ON_STOP`：Listening/Processing 按既有策略取消并释放识别器；权限弹窗导致的 pause 不误取消 Permission。
- 退后台停止 TTS；当前进程答案文字保留，恢复时不自动播。
- 退后台不宣称轮询或 ask 一定继续；进程死亡按 §6/§7 恢复。
- 旋转/重组不复制请求；VoiceAttempt、recognizer generation、TTS generation、navigationRequestId 共同防旧回调。
- 登出/账户切换首先在凭证使用前封闭旧 owner；响应侧再防旧 UI、数据库、TTS 和 alarm 写入。
- 开机沿用本地闹钟恢复与有界同步，不新增常驻服务。恢复结果只有实际系统调用返回后才能标 submitted。

### 10.2 权限时机（已作产品决定）

| 权限/能力 | 请求时机 | 拒绝后的路径 |
| --- | --- | --- |
| RECORD_AUDIO | 用户第一次点语音时 | 完整文字记事/提问；不循环自动弹 |
| POST_NOTIFICATIONS | **首次本机系统排程提交成功后**解释并请求；已授权不重复请求 | 保留服务端 scheduled 与本机 submitted；说明通知可能不显示 |
| 通知频道 | 状态页检查并跳系统设置 | 与运行时权限独立 |
| SCHEDULE_EXACT_ALARM | 不用系统设置打断主流程 | 实际走 fallback 才标非精确；fallback 失败标本机失败 |
| TTS 引擎 | 非运行时权限 | 永远保留文字答案 |

通知权限不再是 App 启动的第一件事。本机排程失败/过期时不因 confirm 成功而立即请求通知权限。

---

## 11. 确认安全、来源与错误语义

### 11.1 授权边界

- 保存记事、提问不需要二次确认，但必须绑定 RequestOwner 并先落创建快照。
- 建议提醒需要明确“确认提醒”或“10 分钟后提醒”授权；完成/取消与删除记事保留二次确认。
- 编辑提醒标题/时间与 confirm 同一请求提交，使用当前 rev；409 后保留编辑值并刷新，让用户重新决定。
- 服务端 confirm 成功后的本地重试只重试 AlarmManager；若用户要主动改未来时间，则是新的 confirm 修改，不称为本地重试。
- 通知栏动作不能绕过确认 Activity；进程死亡后不自动重放结果不明的 mutation。

### 11.2 结果来源

- 记事只显示权威 GET/sync 对账后的服务器正文；本地 B 不与服务器 A 混写。
- 整理字段来自同一 note 当前 rev，不混合旧 rev。
- 问答 citation 文本原样来自 API；不补造 citation，不把 score 当置信度。
- 来源 note 删除/404：当前进程答案可保留，来源标不可用；进程重启后无历史答案卡。
- 本机草稿、创建结果未知、服务器已保存、服务端已确认、本机已提交排程在视觉上分别可辨。

### 11.3 错误与重试

- 错误附着于发生错误的对象；会话失效是全局边界。
- 网络失败不清草稿；已确认的服务器状态不能因本机排程或 refresh 失败被改写。
- create 对账与重试只使用原不可变 A 快照；进程恢复不自动重放。不存在“保留 ID 但改成 B 重试 create”。
- ask 网络未知不自动重试；提醒 mutation 冲突必须重新同步 rev。
- HTTP 422 用固定输入说明；409 按版本、状态、时间过期等原因给操作，不全部归为同步。
- 同步只获取状态，不声称重跑 failed organize。
- “成功”文案必须对应真实步骤：服务器创建、服务器确认、本机系统排程提交、通知可见性分别表达。

---

## 12. 视觉系统与 tokens

### 12.1 色彩

| Token | 值 | 用途 |
| --- | --- | --- |
| `canvas` | `#F4F5F0` | 页面底色 |
| `surface` | `#FFFFFF` | 主卡片 |
| `surface-soft` | `#E9EEE8` | 次级区、输入坞 |
| `ink` | `#17231F` | 主文字 |
| `ink-secondary` | `#53635C` | 次文字 |
| `accent` | `#116B5B` | 主按钮、链接 |
| `accent-strong` | `#0B4D42` | 深色主面 |
| `accent-soft` | `#D8EAE1` | 选中模式、来源 |
| `attention` | `#A44E35` | 待确认 |
| `attention-soft` | `#F5E4DA` | 待确认容器 |
| `danger` | `#A43B42` | 删除/错误 |
| `divider` | `#DCE2DC` | **仅装饰分隔**，不承担输入边界、焦点或状态 |
| `control-border` | `#74877C` | 输入框、次按钮等功能边界 |
| `focus` | `#9B4A30` | 2dp 可见焦点 |

- 正文对比目标至少 4.5:1；大文字与功能图标/输入边界至少 3:1。实现后必须对实际 Compose 色值计算/测量，不由本表替代验证。
- 状态不只靠颜色；装饰边界不冒充状态或焦点。

### 12.2 字体与空间

系统字体；display 30sp、title-lg 22sp、title-md 18sp、body 16sp、body-small 14sp、label 14sp、caption 12sp。正文行高 1.65，标题 1.35–1.5。

4dp 基准间距：4/8/12/16/20/24/32；页面左右 20dp，卡片内 16–20dp，卡片圆角 20dp，Sheet 28dp，按钮 14dp。所有目标至少 48×48dp。内容在平板/桌面最大 680dp。

### 12.3 动效

Sheet 180–220ms；状态更新 150ms。听写只用低幅呼吸环，不画假声纹。系统减少动画时取消呼吸与位移。所有状态即使无动画也可读。

---

## 13. 无障碍、键盘与大字体

### 13.1 TalkBack

- 语音按钮语义：“开始语音记事”“结束并识别”“开始语音提问”。
- 模式用 Role.Tab + selected；听写锁定/切模式取消需有状态说明。
- 卡片摘要与内部按钮分别聚焦；创建 unknown 卡必须读出“服务器可能已保存”，不能只读“草稿”。
- 提醒读出服务端状态、本机排程状态、完整时间和通知异常，不只读“已排程”。
- 引用读出“来源 1，来自记事《…》，双击查看”。
- 状态变化只公告一次；轮询与计时不用 live-region 连读。
- 装饰呼吸环和 divider 不进语义树。

### 13.2 键盘、焦点与功能边界

- Tab 顺序：搜索/状态 → 待确认 → 流内容 → 模式 → 键盘 → 语音。
- Enter/Space 激活；Sheet 焦点进入首字段，关闭返回触发按钮。
- 多行文本 Enter 不默认提交；Ctrl/Cmd+Enter 可提交且有可见按钮。
- Escape 只关闭未提交 Sheet/听写层，不撤销请求。
- 功能输入与次按钮使用 `control-border`；焦点用 `focus` 2dp。`divider` 仅装饰。

### 13.3 大字体与触控

验证 fontScale 1.0/1.3/1.6/2.0。200% 时输入坞可两行、卡片按钮纵排、Sheet 全屏；完整服务端/本机状态、错误与确认按钮不裁切。无固定卡高；底部留白按输入坞实际高度。

HTML 原型只展示布局方向，不能替代实现后的 TalkBack、键盘、对比度和 fontScale 真机测试。

---

## 14. 原型阅读与操作

`prototype.html` 是无网络、无外部资源、无 Canvas 的静态 **DESIGN DEMO**；全部为人造示例，不连接服务，不请求真实权限，不播放真实音频或设置闹钟。

场景包括：

- 统一个人流、听写、整理、带来源答案、提醒确认。
- 创建结果未知 A + 继续编辑 B 的对账界面。
- 服务端已确认但本机排程待核对/失败/过期，以及成功后的非精确提交。
- 冷启动离线仅有本机草稿，不展示持久业务缓存或持久问答历史。
- 搜索、提醒待办/已结束/全部、错误、权限与大字体。

原型中的状态切换只是视觉演示，不是真实网络、凭证、AlarmManager、通知或 TTS 回执。

---

## 15. 一个完整首版的端到端范围

这是一个发布单元，不是散落小补丁；实现 PR 可按约 800 行产品代码门槛拆分，但只有以下整条链路共同验收后才称为统一首版完成。

### 15.1 必须同时交付

1. **统一壳与个人流**：新 IA、视觉 tokens、Compose 组件、空/加载/当前进程离线/错误，以及完整提醒筛选。
2. **统一输入**：固定 VoiceAttempt 归属、文字平权、长度验证、模式切换取消、ASR 生命周期、下一草稿版本保护。
3. **会话安全**：RequestOwner 进入所有 Repository 操作；session mutex 内、凭证使用前复核；登出与本地条件写有序。
4. **记事闭环**：不可变创建快照、未知结果对账、真实整理状态、详情、编辑 rev 冲突、删除及关联提醒。
5. **问答闭环**：同步 ask、当前进程结果、来源跳转、AudioCoordinator 自动/手动 TTS 与停止；进程死亡诚实恢复问题而非答案。
6. **提醒闭环**：建议确认、本地时间编辑、服务端/本机双轴状态、失败本地重试、完成/取消/贪睡、通知栏与 boot 回归。
7. **搜索闭环**：入口、结果、空态、来源跳转。
8. **最小端侧恢复**：Android 私有 ComposerDraft + CreateRecovery；无完整业务缓存和持久问答历史。
9. **无障碍**：TalkBack、键盘、大字体、减少动画、触摸目标、功能边界对比。
10. **回归证据**：Android 单元 + Compose 语义/UI + Repository/MockWebServer 契约 + 可控竞态 + 真机权限/生命周期/Alarm/TTS 验收。
11. **迁移**：保留既有令牌、pending alarm SharedPreferences 和通知 PendingIntent 行为；不要求服务端迁移。

### 15.2 明确不在首版增加的服务端工作

不修改 FastAPI 路由、schema、Runtime、Worker、数据库、鉴权或部署；不新建 `/utterances`、chat history、task list、task cancel、organize retry、memory CRUD；不把 WorkItem 暴露成任务中心。

---

## 16. OUT OF THIS FIRST RELEASE

以下未来能力不得在首版 UI 中以可用入口、成功 Toast 或进度状态出现：

- 持久多轮聊天、持久问答历史、跨问题自动上下文、服务器会话列表。
- 完整离线 Note/Reminder/Answer 业务缓存、跨进程离线阅读。
- 多个并行在途业务 HTTP 请求、自动排队发送、零 session-mutex 等待承诺。
- 任意意图路由、一句话直接完成/删除/改提醒而不经明确动作与 rev。
- 通用任务中心、持久问答后台进度、取消服务器任务、WorkItem 历史查询。
- 离线 ASR 保证、离线自动整理、后台自动发送保证、实时流式语音。
- 通知点击精确定位 reminder；首版保持打开 App。
- 音频时间轴、真实声纹/分贝、唤醒词、耳机/锁屏/小组件。
- 记忆画像管理、忘记记忆、导出、每日摘要、主动周报。
- 日历/地图/天气/外部 MCP、操作手机或电脑、多 agent、iOS。
- 新通知 routine、常驻服务、推送通道、多设备同步策略升级。
- 同文“重新整理”所需的新服务端接口。

---

## 17. 验收测试矩阵

| ID | 场景与操作 | 必须结果 |
| --- | --- | --- |
| A01 | 首次登录且无数据 | 空态可操作，无示例混入真实流 |
| A02 | 语音记事成功且网络空闲 | 先落完整快照，再创建；权威对账后显示真实状态 |
| A03 | ASR 无匹配/超时/网络错误 | 有文字路径，原草稿不丢 |
| A04 | 麦克风拒绝/永久拒绝 | 不循环弹窗，文字完整可用 |
| A05 | Permission/Listening/Processing 切模式、旋转、后台 | 本次尝试取消或旧回调被拒；无误投、无重复创建 |
| A06 | A 提交在途，继续输入 B | A 回包只消费 A 对应版本，B 不丢 |
| A07 | 创建超时后原样核对/重试 | 使用同一完整 A 快照与 client_note_id；权威正文对账，身份唯一 |
| A08 | A 已保存但 202 丢失→重启→编辑 B→重试 | 找到唯一 A；B 不丢；服务器卡只显示 A；可明确 PATCH B 或新 ID 另存 |
| A09 | A 在另一端修改/删除后恢复 | 修改则 rev 冲突保 B；删除/tombstone 不复活、不清 B |
| A10 | 恢复期间 sync 已找到 A、双击发送、落盘失败 | 已找到则不 POST；双击仅一操作；落盘失败不发送且文字仍在 |
| A11 | 本机移除 unknown A 恢复项 | 明确不删服务器；linked B 转为独立 local_only 并保留 |
| A12 | create 重放直接返回 organized/failed | 以 GET/sync 当前状态为准，不回写 uploaded |
| A13 | uploaded 轮询超时 / failed | 超时不等于 failed；failed 无同文重试整理 |
| A14 | 编辑 409 / 删除记事 | 编辑文字保留；删除二次确认且成功后才清关联闹钟 |
| A15 | 提问成功/无记忆/有无 citation note_id | 答案属于本次；无记忆非异常；来源按钮真实可用或禁用说明 |
| A16 | ask 超时、离开、进程重启 | 不自动重试、不永久转圈；只恢复问题与“答案无法恢复” |
| A17 | 答案在 ASR Permission/Listening/Processing、TTS 初始化、退后台时到达 | 不自动播；文字保留；新 ASR 停止旧 TTS/pending |
| A18 | 用户播报旧答案时新回答到达/旋转恢复 | 不抢占主动播报；旋转不自动重播；长答案分块不拆 surrogate |
| A19 | suggested / 过去 suggested | 未确认不排程；过去时间必须改未来，不只提示同步 |
| A20 | confirm 成功 + 本机排程成功 | 分别显示服务端已确认与实际精确/非精确提交结果 |
| A21 | confirm 成功 + scheduler/fallback 抛错 | 服务端 scheduled 保留；本机 failed；只提供本地重试，不重复 confirm |
| A22 | 响应到达时 fire_at 已过期 | local=expired，不显示已提交；可明确修改未来时间 |
| A23 | confirm 成功后 refresh 失败 | 不改写 confirm 失败；保留服务端结果和独立本地状态 |
| A24 | 进程在 POST 与 schedule 之间死亡 | 重启 local=unknown；fresh sync 后本地恢复，不重新 POST |
| A25 | 精确权限撤回、fallback 成功/失败 | 成功标非精确；失败标本机失败；均不改服务器状态 |
| A26 | 通知拒绝/频道关闭 | 独立提示；不回滚 scheduled/submitted；排程失败时不主动请求通知权限 |
| A27 | 完成/取消/贪睡与通知栏进程死亡 | 既有确认/fresh rev/fences 不变，不自动重放 ambiguous mutation |
| A28 | boot 断网 | 先恢复 pending alarms；同步失败不删除；prefs 不单独构成 submitted 回执 |
| A29 | 搜索边界与记忆延迟 | 500 字有效；organized 不保证立即可搜，空结果诚实 |
| A30 | 冷启动离线 | 只显示当前 owner 本机草稿/恢复项；不显示旧 Note/Reminder/Answer 缓存 |
| A31 | 当前进程加载后断网 | 已加载内容标可能非最新；失败不转换为空列表 |
| A32 | 暂停 session mutex，旧 create/ask 等锁，登出/同用户再登录 | 旧操作取锁后 owner 不符，在读/用新 token 前拒绝；无新代 HTTP/DB/TTS/alarm |
| A33 | refresh 期间登出或 owner 变化 | 使用 refresh token 和重试新 access token 前均复核；旧操作不能跨代 |
| A34 | 旧响应在清库后到达、缓存/草稿读后失效 | 条件写失败/被登出清理；新页面只显示当前 owner |
| A35 | 普通 refresh 不换登录 | loginEpoch 不变，当前有效投影/草稿不清 |
| A36 | 旧/新 refresh 与 delete/organize/notification complete 交错 | 单一投影顺序；rev+tombstone+refreshId 防复活 |
| A37 | 导航 A→B 后 A 迟到；同 alarm action.id 改 payload | B 不被覆盖；最新 payload 重排，不做永久 ID 去重 |
| A38 | 提醒待办/已结束/全部与无 note_id | 所有状态可达；空态明确；无来源不显示必 409 mutation |
| A39 | TalkBack、键盘、fontScale 2.0、减少动画、边界对比 | 独立真机/Compose 验证；完整双轴状态和操作可达 |
| A40 | Mock 与 Real 不同 server ID | 不用 Mock 的 client_note_id==id 掩盖问题；Real 契约验证对账/tombstone/rev |
| A41 | 冷启动旧版本升级 | SecureTokens、pending alarm prefs、通知动作不退化；Room 不影响 JVM data 测试入口 |
| A42 | 服务端无变更 | 仅使用 Api.kt 已有网络形状；RequestOwner/LocalScheduleResult 是客户端改造 |

---

## 18. 迁移与回归硬约束

1. 不丢 notes/search/ask/sync/reminders/auth、已结束提醒、编辑、通知操作。
2. 不更名/清空 `pending_reminder_alarms`；Room 升级不得影响旧闹钟。
3. 通知完成/取消仍二次确认；fresh rev、8 秒有界执行、session/alarm fences 不削弱。
4. 保留 recognizer/TTS generation，并增加 VoiceAttempt 与 AudioCoordinator，不靠 busy 隐式互斥。
5. 创建恢复快照不可变；任何 B 不得复用 A 的创建身份。
6. 不自动重放 ask、提醒 mutation 或跨 loginEpoch 的创建。
7. rev 只防旧对象，不防删除复活；必须有 tombstone+refreshId 栅栏和单一投影顺序。
8. 网络串行与锁等待明示；不删 session mutex、refreshMutex 或 alarm consume 锁。
9. 私有存储继续 `allowBackup=false`；不记录正文日志，不把 Room 当加密。
10. `scheduled`、pending prefs、通知权限、频道、精确性均不得单独翻译为“本机已排程”。
11. 测试不删断言、不 skip 凑绿；Mock 不能替代不同 server ID 的真实契约测试。
12. 用户可见切换在完整闭环后进行；回滚不得影响既有 token/alarm 存储。

---

## 19. 已作决定与剩余非阻塞视觉校准

以下原未决项现已决定：

1. 问答只保留当前进程；问题草稿可恢复，答案不持久化。
2. 草稿手动发送；无“全部发送”，无自动网络队列。
3. 通知权限在首次本机排程提交成功后请求；失败/过期不请求。
4. 时间编辑采用 Material 日期/时间控件，客户端生成 ISO。
5. 长答案默认 8 行，提供展开；大字体测试可调整视觉行数但不改变信息完整原则。
6. 私有 Room 只存 ComposerDraft/CreateRecovery，位于 Android app 或独立 Android 存储模块。
7. 进程死亡后未回答提问标无法恢复，由用户明确重新提问。
8. 通知点击首版仅打开 App，不做精确 reminder 路由。
9. 网络操作保持串行；遇忙保文字、不排队，由用户稍后手动提交。

仍需实现后校准但不改变契约的事项只有：具体日期/时间控件的视觉密度、长答案折叠行数、大字体下卡片按钮排列。这些不构成 P1 状态或安全选择。

---

## 20. 最终体验校验

用户不需要理解“记事功能”“问答功能”“agent 任务”三套概念：

- 想留下什么，就在“记下”里说或写。
- 想找回来，就在“提问”里问，或搜索。
- 需要设提醒时，卡片只问一次明确确认，并分别告诉用户服务器与本机结果。
- 网络等待时可以继续写下一句话，但不会暗中并行、排队或跨会话发送。
- 创建结果不确定时，原请求与修改稿都保留，先核对再更新或另存。
- 离开后回来，能做到的恢复清楚呈现；做不到的恢复直说。

这就是首版统一的全部：**一个连续对象流、两种显式输入、明确的会话与确认边界、一套诚实且可测试的恢复语义。**
