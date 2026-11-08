# Codex 独立设计评审：统一移动交互

评审日期：2026-11-09。代码基线：`3600c3ab4a1c499f099e39be6b8084b3351dd7b7`（实际 HEAD 一致）。评审对象为本次实际读取的 `01-kimi-design.md`，对应用户指定的 Kimi 会话 `c5428a31-aaef-4b2a-9ef1-ca832da18257`；不是对协调者摘要或历史方案的评审。未自行重写 Kimi 设计。

**结论：需 Kimi 修改后复审，暂不进入生产实现。没有发现 P0；有 3 项 P1 设计契约阻塞、4 项 P2 实现/验收细化。** 统一流、显式“记下/提问”、来源回溯和局部错误的方向可行；阻塞在并发授权、创建恢复和闹钟状态真实性，不在视觉风格。以下 P1 是新设计必须补齐的边界，不声称现有串行客户端已发生所述并发回归。

## 评审依据与验证范围

- 已读仓库 `AGENTS.md`、`00-primary-sources.md` 与实际设计全文。核对 Android 的 `MainActivity/AgentModel`、`Api.kt`、`RealRepository/MockRepository`、`AppRepositories`、`SecureTokens` **源码**、ASR/TTS、AlarmManager、通知确认及 manifest/Gradle 依赖。没有读取实际凭证、环境或 key 文件。
- 核对服务端 `api/notes.py`、`api/ask.py`、`api/sync.py`、`api/reminders.py`、`schemas.py`、`domain/channel.py`，并对照鉴权代际代码与服务端不变量。历史文档不作为已有客户端能力证明。
- 使用只读本地官方原型快照 `/workspace/qh-unified-evidence/primary-sources` 的 README、哲学、领域、授权、执行与 panel 文档：它们支持少概念、明确授权、诚实状态和恢复前核对，不证明本 App 有原型的工具或任务接口。本次没有重新联网核验文章，也没有将原型实验数字作为移动端效能证据。
- 检查时 `docs/unified-interaction/prototype.html` 尚不存在，未等待/轮询。没有浏览器、设备、TalkBack、真实权限或音频测试；没有完整构建。本报告是静态契约与可实现性评审，不是 UI 测试通过报告。
- 当前没有 Room 实现或依赖：`android/data/build.gradle.kts:1` 是 Kotlin/JVM 模块，`android/app/build.gradle.kts` 也未接 Room。设计 §7 的 Room 是新增客户端工作，不能当作已存在存储直接调用。

下文 Android 路径以 `android/app/src/main/kotlin/dev/qh/agent/` 为默认前缀；数据层为 `android/data/src/main/kotlin/dev/qh/data/`。行号对应本次基线，设计位置以章节为准。

## P0 / P1：必须先修订的设计阻塞

### 1. P1 — 拆分 busy 后，只有“响应回来校验 session”挡不住旧请求使用新会话凭证

**设计位置：** §7.2–7.4，§10.1“所有返回结果先验证 session generation”，§18.8。

**代码证据：** `Repository.kt:70–101` 的 `authorized()` 在获得进程级 session mutex **之后**才 `store.read()`；没有接收请求发起时的 owner/generation。该 mutex 覆盖整个 HTTP 调用，包含最长可达 75 秒的 ask（`:65–67,136–138`）。`MainActivity.kt:132–143` 当前用全局 busy 限制前台操作；`:323–331` 登出先 bump generation 再等待 `repo.logout()`。`AppRepositories.kt` 的 generation 是进程内计数；持久的 login-session epoch 在 `SecureTokens.kt:27–32`，refresh 不改变它。服务端 `deps.py:45–59` 校验 JWT `gen`，`api/auth.py:385–414,467–477` 维护设备凭证代际；它们不能识别“客户端旧草稿拿到了新的合法 token”。

**失败路径：** 新设计允许其他对象继续提交。旧会话的创建/提问若仍在调度或等待 mutex，登出/重新登录先完成后，该操作进入 `authorized()` 会读取新会话 token。即使回包被 generation 检查丢掉，旧正文仍可能已经写入或发给新会话；同一用户重新登录也不能自动继承旧操作授权。服务器目前还限制同一 device_id 跨用户绑定（`api/auth.py:51–98`），不能因此省略客户端 owner 隔离。

**Kimi 需明确的契约：** 每次操作在用户提交时绑定不可变 owner、login-session epoch、操作 ID；等待会话锁后、读取/使用凭证前再次核验该绑定，拒绝过期操作。全局 session mutex 与 refresh 安全必须保留，不能靠删除锁实现并发。登出开始立即封闭新提交并作废旧提交；取消旧协程只是辅助。Room 的所有行与读取也绑定服务器/运行模式及 user owner，异步写入必须与登出清库形成有序的条件提交，不能“先验 epoch → 等数据库 → 无条件写回”。进程重启不以重置后的 AtomicLong 值判断旧数据归属；普通 refresh 不清有效投影，也不冒充新登录。

**验收：** 暂停一个请求占有 session mutex，分别排入旧创建、旧 ask 和登出/登录；用可控调度让旧操作在换代后才取锁，验证没有携旧正文的新代 HTTP 请求、数据库写入、播报或重设闹钟。再覆盖同用户重新登录、旧响应在清库后到达、缓存先读后发生会话失效。新页面只显示当前 owner 的内容。仅验证“没有旧卡片”不足以通过。

### 2. P1 — 不确定创建仅保留 client_note_id 不够；编辑/删除和重试正文必须有明确边界

**设计位置：** §7.2 `DraftItem`，§7.3.1–2，§9 草稿“编辑/删除”与 send_unknown，§11.3，§18.5；A07/A29。

**代码证据：** `server/app/api/notes.py:48–59,84–97` 以 `(user_id, client_note_id)` 返回已有对象，**不比较新提交正文**；`:53` 分支也不排除已删除对象。`schemas.py:131–146` 确实已有 `client_note_id`，但 Android `Api.kt:16–19` 的 Note DTO 尚未接该字段。现有 `MainActivity.kt:204–210` 保留完整 `pending NoteCreate`，却在 `updateDraft():198` 中清 pending；不能把这套编辑行为直接搬到持久草稿。

**失败路径：** 正文 A 已被服务端保存但响应丢失；用户把草稿改成 B，保留同一 ID 重试时，服务端返回 A 的 note_id。若客户端按本地 B 合成成功卡并清掉草稿，B 丢失且暂时显示了不存在的服务端内容。反过来，换 ID 会重复创建 A。send_unknown 被标为“未发送”或直接删除，也会暗示服务器不存在 A。若 A 已在别处删除，幂等 POST 仍可能返回其 ID，不能复活缓存。

**Kimi 需明确的契约：** 首次发出前持久化完整不可变请求快照（包括正文、ID、来源、时间、时区），恢复重试逐字段复用；不能将可编辑 composer 与已发送快照混作同一实体。`local_only` 可以自由编辑/删除；`sending/send_unknown` 的修改要保留用户新文字，先核对原对象，再明确执行带 rev 的 PATCH 或另存新记事，不能偷偷换 ID。send_unknown 的删除最多表示移除本机恢复项，不表示服务器撤销。创建返回后以真实 GET/sync 正文及 client_note_id 对账，原子合并草稿与 NoteItem；404/tombstone 不复活，不清掉尚未保存的新文字。区分确定未发送、确定拒绝和结果未知，不把所有网络失败写成离线未发送。

**验收：** 服务端已保存 A 但丢 202 → 重启 → 改 B → 重试；最终 A 身份唯一，B 不丢，任何“服务器已保存”正文都与服务器一致。再覆盖在另一端修改/删除 A、恢复期间 sync 已找到 A、双击手动发送、落盘失败和本机删除未知项。当前 Mock 用 client_note_id 作为 note.id（`Repository.kt:250–259`），必须另用真实不同的 server ID 做契约测试，不能只靠 Mock 验收。

### 3. P1 — `scheduled` 与本机实际排程仍被合成一个成功状态

**设计位置：** §5.3“成功后……本机已排程”，§6.4/§9 `scheduled → 已排程`，§8 确认成功映射，§11.3；A20–A23。

**代码证据：** `ReminderAlarms.kt:59–65` 的 `confirmed/snoozed` 返回 Unit；`:73–88` 在缺字段、解析失败或非 scheduled 时可静默跳过，`:90–110` 在过去时间且无 pending 项时不设 alarm，fallback 也可能抛出异常。写 prefs 发生在系统排程之前（`:100–110`），prefs 存在同样不是成功回执。服务器 `domain/channel.py:108–125` 只在确认时要求未来时间；网络往返、进程中断后到客户端时可以已过期。`MainActivity.kt:170–175` 将 POST、local schedule 与后续 refresh 串在一次 run 中，不能直接继承其单错误结果。服务器根本没有设备本地排程状态字段。

**失败路径：** 服务器确认成功，但到客户端时已越过 fire_at、fallback 失败或进程在本机排程前死亡，刷新得到 scheduled 后 UI 仍按矩阵显示“已排程”。用户会认为本机将提醒。通知被拒绝/频道被关闭与精确闹钟不可用，又是另外两个维度。

**Kimi 需明确的契约：** 服务端 scheduled 只表示“提醒已确认”；本机另有未知/待核对、已提交系统排程、失败/已过期状态。只有相应本机步骤真实完成才显示“本机已排程”，且不保证系统必定准点送达。保留 action 缺失时的 scheduled reminder fallback，不从 suggested 缓存造 alarm。排程失败保留服务器成功并给出同步/本机恢复路径，不重新 POST confirm 冒充修复；refresh 失败不能把已成功 mutation 改写为失败。进程重启后不得仅由缓存 scheduled 或 pending prefs 推断已排程。通知权限、频道可见性、精确性均独立说明。

**验收：** 确认成功后本机抛错、fallback 抛错、到达时已经过期、确认后 refresh 失败、进程在 POST 与 schedule 之间死亡，均不能出现假的“本机已排程”，也不重复服务器 mutation。覆盖权限撤回竞态、非精确降级、通知拒绝/频道关闭与断网 boot 恢复；旧 pending alarms 不被同步失败删除。

## P2：实现与验收护栏（不要求新增产品能力）

### 4. P2 — 统一输入需要固定一次听写/提交的归属，不能只看当前模式

**设计位置：** §4.1“模式切换只改变提交目标”，§7.4 多对象 inFlight，§10，A05/A17/A36。

**证据与风险：** 目前记事/提问分别在不同 Compose route 创建 VoiceAsr，回调分别绑定 `createFromVoice/askFromVoice`；`VoiceAsr.kt:40–72` 的 generation 只防旧识别尝试，不知道用户模式、账户或草稿。改为一个入口后，若听写启动于“记下”，识别中切到“提问”再按当前模式分发，就会自动发送错误意图。提交 A 后继续输入 B，A 回包也不能像现有 `MainActivity.kt:208` 那样无条件清空当前 draft。

**护栏/验收：** 每次听写捕获 mode、owner epoch、attempt ID 与目标草稿版本；识别期间锁定模式或切换即取消本次尝试，二选一写清楚。每次提交快照化内容，仅消费提交时对应版本的草稿。覆盖 permission 尚未返回时切模式、Processing 时切模式、A 在途继续输入 B、旋转和后台返回，无误投、无吞字、无重复创建。

### 5. P2 — 多卡问答和不阻塞输入后，TTS 仲裁必须覆盖异步答案，不只覆盖按钮

**设计位置：** §4.3、§8 播报、§9 TTS、§10.1、§18.4。

**代码证据：** `MainActivity.kt:624–625` 自动播报只检查 RESUMED，现状靠 busy 避免 ask 与新 ASR 并行；`:645,663–665` 及 `AnswerTts.kt:8–9` 管手动互斥。`AnswerSpeaker.kt` 有 utterance generation 和未初始化时的 pending，但不负责多个 AskItem 的选择。

**护栏/验收：** 单个音频协调者；开始新 ASR（含权限流程）停止 TTS 及 pending，答案可持久化但 Listening/Processing/Permission 时不播。若允许多个 ask 在途，明确只有当前仍具自动播报资格的那次回答可自动播，乱序旧回答不抢占用户主动播报；恢复历史卡不自动播。测试答案恰在语音开始、TTS 初始化、切模式、退后台/旋转与 stop 后到达。保留中文不可用、长答案分块与 UTF-16 surrogate 不拆分行为。

### 6. P2 — 缓存与全量刷新需定义一个提交顺序；rev 本身不能防删除复活

**设计位置：** §7.3、§7.4、§11.2、§18.7–8。

**代码证据：** `RealRepository.sync():140–166` 完整聚合分页及 deleted；`notes():107–117` 是另一组分页请求。`MainActivity.kt:145–166,228–269,283–288` 当前以 refreshMutex 协调刷新、编辑、删除和整理观察。`Note` 有 rev，而 Android `SyncDeleted` 只有 ID，没有可与 note.rev 直接比较的删除版本。`openNote():77` 当前单 detailNote 也没有导航请求 ID。

**护栏/验收：** 同一会话内保持单一 repository→投影写入协调顺序，批量 notes/reminders/deleted 的应用原子化；不要为拆 busy 把旧请求完成结果并行无条件 replaceAll。按 rev 拒绝较旧对象，另用删除栅栏/刷新代次拒绝删除前快照；成功的旧全量请求不能清除之后创建的卡。离线失败不能当成空列表。导航 A→B 后 A 的迟到返回不能覆盖 B；引用正文可保留历史，但目标不存在只标不可用。覆盖 edit/organize、delete/refresh、confirm/notification complete、同一 alarm action ID 更新 payload 的交错；沿用 alarm consume 锁，不只对 action.id 做永久去重。

### 7. P2 — 无障碍规格可实施，但 token 边界色与“已结束”入口需要落到具体可达控件

**设计位置：** §3、§12.1、§13、§15/§19。

**证据与风险：** 当前 `ReminderListFilter` 和 `MainActivity.kt:376–408` 有独立提醒筛选；设计以 note 内嵌提醒替代后，要保证 scheduled、done、cancelled 及无 note_id 的提醒都有可达列表，不能只剩 suggested 注意力区。`Api.kt` 明确 `note_id` 可空，服务端 mutation 对无 note thread 返回 409（`api/reminders.py:63–64,134–135`），不能为这种卡渲染保证成功的确认入口。无障碍规范写得充分，但给出的 `divider #DCE2DC` / `surface-soft #E9EEE8` 在白底很接近，若承担唯一输入边界/焦点则不能凭文字宣称满足 3:1；纯装饰分隔线可不承担这个责任。

**护栏/验收：** 在最终设计里指出已结束/无来源提醒的入口与空态，不新增任务中心；明确过去的 suggested 提醒确认需改未来时间（服务端 `channel.py:108–112`），不能所有 409 都只让用户同步。Compose 中使用实际 dp/sp、可测 semantics、可见焦点及独立按钮。对实际有功能的边界颜色计算对比，装饰边界不冒充状态/焦点。TalkBack、fontScale 2.0、键盘、减少动画由实现后的真实测试证明，不由 HTML 或 tokens 表替代。

## 已核对成立的契约与保留项

| 契约 | 真实依据 | 评审判断 |
| --- | --- | --- |
| 显式记事/提问、独立 ask、非持久聊天 | `Api.kt:53–78`；`api/ask.py` | 可以统一到同一流，不需要自由路由或新聊天 endpoint；ask 超时不自动重放，refresh 后的合法 401 重试与网络超时重试要区分 |
| 202 保存与异步整理分离 | `api/notes.py:42–99`；`OrganizePoll.kt`；`MainActivity.kt:271–305` | uploaded 轮询超时不等于 failed；重放 create 可能返回 organized/failed，不能一律改回 uploaded |
| 编辑/删除及派生失效 | `api/notes.py:153–241,245–284` | PATCH rev 冲突保留编辑稿；正文变化清派生、作废 suggested，已确认提醒不因改文取消；DELETE 连关联提醒/actions 墓碑，无虚构 DELETE rev 参数 |
| 搜索/来源 | `api/ask.py`；`schemas.py:243`；`Api.kt:24–28` | 500 字 search、2000 字 ask；score 不等于置信度，citation.note_id 可空；记忆异步同步，organized 不保证立即可搜 |
| 确认/贪睡授权 | `api/reminders.py`；`domain/channel.py:207–245` | suggested 一键贪睡确实会 scheduled；“10 分钟后提醒”必须在点击前明确，这是用户主动授权排程，不应误标成未经 confirm endpoint 就绝不能排程 |
| 通知安全 | `ReminderNotificationConfirmActivity.kt:35–65`；`ReminderAlarms.kt:220–275` | 保留终态二次确认、fresh rev、8 秒有界执行、持久 session epoch、已提交进程死亡不重放；snooze duplicate 可返回后续终态，现有 scheduler 的状态检查不能去掉 |
| 生命周期 | `VoiceAsr.kt:86–105`；`AnswerSpeaker.kt` | 系统识别不是离线保证；Permission 暂停例外、cancel/destroy generation、TTS stop/destroy 都应保留 |
| 迁移/隐私 | manifest `allowBackup=false`；`SecureTokens.kt`；`ReminderAlarms.kt:33–46` | 不换 token/alarm prefs 名，不因 Room 升级清掉旧闹钟；私有投影不记录正文日志，不把 Room 存在等同于已加密 |

## 首个完整发布的建议边界

§15 同时引入新 IA、Room 四类存储、离线草稿、持久问答、并发重构、日期时间控件和通知定位，范围偏宽；它可以成为最终统一版本，但不适合作为一次未经收口的首轮实现。建议 Kimi 明确一个**完整端到端发布单元**，而非 S40 微补丁：

保留统一首页与新视觉、显式记下/提问输入、真实整理状态、详情编辑删除、全部提醒状态与确认/完成/取消/贪睡、搜索来源、TTS、通知/boot 以及无障碍。第一轮保持已有安全的网络串行机制，网络等待时仍允许浏览和编辑下一份本机输入，明确“正在提交”但不承诺其他写请求并行执行。进程重启重新获取服务器记事/提醒；未返回的问答诚实说明无法恢复。持久化先限定为恢复幂等创建所必需的不可变请求快照与用户文字草稿（仍须通过发现 1/2）。

完整业务缓存的离线阅读、本机长期问答历史、多个在途 HTTP 请求与通知点击精确定位可以后置为下一次完整能力升级；现有问答文字、手动重播、通知点击打开 App 和所有提醒操作不能退化。若产品坚持 §15 全量范围，亦可，但需先解决三项 P1，并将缓存、会话与并发验收作为前置工作，不能先做新壳再补安全。

实现可分 PR 控制代码体积，仍按仓库约 800 行产品代码门槛拆分；用户可见切换要在整个闭环可用后进行，不能把旧流程拆坏后称为阶段成果。这是评审建议，尚未替 Kimi 或产品修改首版范围。

## 修订及实现验收出口

1. Kimi 对发现 1–3 更新原设计的状态/恢复/并发契约，消除互相冲突文案，再由 Codex 复核；P2 可以明确采纳或记录合理延期，不新增猜测性功能。
2. 实现阶段以既有 Android 回归加独立契约测试交叉验证，重点覆盖上述可控竞态和未知结果，不让新测试迁就实现。新 Room 若采用，应在 Android 模块或独立 Android 存储模块接入，避免直接把现有 JVM data 模块变成 Android 模块而破坏其测试入口。
3. 完成实现后再运行适当单元/Repository/Compose 检查与真实设备权限、ASR/TTS、通知/闹钟、升级回归。服务端保持不变，无需为了本轮纯设计报告启动完整构建或 PG 并发测试；以后若实际改 server，并发敏感改动仍须满足仓库 PG 测试门禁。

本阶段仅交付本独立报告；未修改产品代码、Kimi 原设计或仓库运行设置，未发送外部消息、push、建 PR、合并或部署。
