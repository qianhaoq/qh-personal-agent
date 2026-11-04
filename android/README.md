# S27 · 贪睡按钮文案

紧凑的 Kotlin / Compose 客户端：登录、时间线、记事创建/编辑/删除、问答、提醒同步、确认/取消/完成；
确认后的 `schedule_reminder` Channel action 会落到 AlarmManager，响铃时显示系统通知。
系统 SpeechRecognizer 中文语音记事与语音问答，系统 TextToSpeech 中文答案播报。minSdk 26，compile/targetSdk 34，JDK 17，Gradle 8.7，AGP 8.5.2。

## S16 提醒取消

- API：`POST /v1/reminders/{id}/cancel`，body `{"rev": <当前 rev>}`，返回
  reminder、可选 cancel_reminder action、work_item_id 与 duplicate。
- 主提醒列表和记事详情「关联提醒」对 suggested/scheduled 显示「取消」，busy 时禁用。
  成功后立即撤销本地闹钟并刷新；sync 同时消费 cancel action、schedule action 墓碑及 cancelled 状态。
- Mock 同样支持取消、rev 冲突和当前 rev 幂等；scheduled/suggested → cancelled 时 rev+1，
  墓碑 schedule action 并发出 cancel action；取消后的提醒不能再次确认。
- 不部署、不引入 FCM/Room/Hilt/Pipecat，不做贪睡、rrule 执行或已确认后改时间，ASR/TTS 核心保持现状。

## 安装与运行

1. 安装 Android Studio，使用 JDK 17（Gradle JDK 也选 17）。SDK Manager 安装
   Android SDK Platform 34、Build Tools 34.0.0、Platform Tools，以及 API 26+ 模拟器。
2. 在 Android Studio 打开本目录，等待 Gradle sync。命令行使用 `ANDROID_HOME`
   指向 SDK，或在 **本目录** `local.properties` 设置 `sdk.dir=/absolute/path/to/sdk`。
3. 启动模拟器，选择 `app` 的 debug variant 后 Run。默认无需服务器：debug 的
   `USE_MOCK_API=true`，任意非空字符串均可登录（例如 `demo`，不是实际邀请码）。
4. API 33+ 首次启动会请求通知权限。若系统未授予精确闹钟权限，提醒退化为
   `setAndAllowWhileIdle` 的非精确闹钟；需要准点触发时，可在系统设置中允许
   「闹钟和提醒」。未声明仅限系统闹钟/日历类应用使用的 `USE_EXACT_ALARM`。

从本目录运行：

```sh
./gradlew :app:assembleDebug
./gradlew :data:test                 # 纯 JVM mock / Retrofit 契约与会话测试
./gradlew --no-daemon assembleDebug testDebugUnitTest
./gradlew installDebug               # 已连接设备或模拟器
./gradlew assembleRelease            # 未配置签名；生成 unsigned APK
```

Debug APK：`app/build/outputs/apk/debug/app-debug.apk`。
仓库根目录自检：`python3 scripts/check-invariants.py`。
现有 `scripts/ci-test.sh` 会运行 Android 构建及测试，无需修改。
不要提交 `local.properties`、SDK、JDK、签名文件或凭证。

## 接入真实服务

在本目录被 Git 忽略的 `local.properties` 添加：

```properties
api.baseUrl=http://10.0.2.2:8000/
api.useMock=false
```

`10.0.2.2` 是标准 Android Emulator 对宿主机的地址。服务端须监听模拟器可达的
接口；服务端配置、数据库初始化及邀请码签发按 [server README](../server/README.md)
操作。真机使用可达的 HTTPS 地址。URL 必须以 `/` 结尾，不能带 `/v1/` 前缀。
本工程不会内置实际邀请码或密钥。不要将 API key 放在移动客户端。

仅 debug manifest 允许 HTTP 明文以便连接本地服务；release 禁止明文，且无论
`api.useMock` 如何设置都会使用真实 Retrofit。发布构建须配置 HTTPS `api.baseUrl`，
默认地址为生产占位 `https://agent.pinhaoyun.com/`；debug 默认 mock 不访问该地址。
切回 mock：删除 `api.useMock=false` 或改为 `api.useMock=true`。
`local.properties` 在构建时读取，改动后需重新构建安装。

## S15 应用标签与 README 文案对齐

本刀仅做文案对齐：应用标签、UI 标题与 README 验证节中的旧版本残留统一更新为 S15，
与 README 主标题一致。历史章节与模块列表中的版本号保留，继续表示对应的历史能力。

不做：ASR、AlarmManager/boot、confirm 协议、server、网络、FCM、Room、Hilt；
不新增依赖或权限；不改业务逻辑。

## S14 提醒 status 中文徽章

提醒主列表与记事详情「关联提醒」在标题下方共用 Material3 Surface 徽章：
suggested →「待确认」（tertiaryContainer）、scheduled →「已排程」（primaryContainer）、
done →「已完成」（surfaceVariant）、cancelled →「已取消」（errorContainer）。
未知状态使用 surfaceVariant，文案保留 trim 后的原值，null / 空白兜底「未知」。
颜色均搭配对应的内容文字色；状态同时以文字表达。
`ReminderStatus.kt` 提供可测纯函数，`ReminderStatusTest.kt` 覆盖已知、未知与空值映射。

不做：ASR、AlarmManager/boot、confirm 协议、server、网络、FCM、Room、Hilt；
不新增依赖或权限。保留原始 status 与提醒操作逻辑。

## S13 confirm 对话框草稿进 ViewModel

确认对话框的标题与 fire_at 草稿从 Composable `remember` 迁入 `AgentModel`：
`confirming` / `confirmTitle` / `confirmFireAt`，经 `beginConfirm` / `updateConfirm*` /
`cancelConfirm` 同步。旋转与配置变更后对话框仍打开且草稿不丢，与现有编辑草稿旋转契约一致。
取消、确认成功提交、`clearUi` / 登出均清空草稿。校验错误文案仍可留在对话框本地。
不改 ASR、AlarmManager/boot、server、网络协议；不新增依赖或权限；不引入 FCM/Room/Hilt。

## S12 确认前编辑标题与时间

时间线 suggested 提醒点击「确认并设闹钟」打开对话框，预填标题与原始 ISO 时间。
空白或未改字段不传覆盖，沿用原值；原时间为空也可补填。时间经 Instant.parse
校验，修改后归一化；非法格式、缺失时间、标题超过 256 码点或含 NUL 提示可读错误。
未来时间仍由 data / 服务端校验。取消不提交，busy 时禁用操作；确认通过 Repository
传可选 fireAt/title，成功沿用 alarms.confirmed + refresh。详情关联提醒继续只读。
ConfirmOverrideTest 纯 JVM 测试覆盖无覆盖、单字段、双字段、空白、补填及非法输入。
不改 ASR、闹钟/boot 核心、server，不部署，不新增依赖或权限。

## S11 可读本地时间

提醒卡片与详情关联提醒的 fire_at、时间线与详情的 recorded_at、todos due
统一通过 `:app` 的纯函数 `DisplayTime.kt` 展示。`formatDisplayTime(iso, zone)`
默认使用设备 `ZoneId.systemDefault()`，可注入固定时区；Instant / OffsetDateTime
转为本地墙钟 `2026年11月3日 20:00`，省略秒，不硬编码 +08:00。
纯日期显示 `2026年11月3日`，不做时区位移。不含偏移的日期时间不猜测时区。
null / blank 返回空串，解析失败返回 trim 后原文且不抛解析异常；
`formatDisplayTimeOrUnset(iso, zone)` 对空串返回「未设置」。待办空 due 不显示括号。
仅改变展示，排序继续用原始 ISO 字符串，DTO、请求与闹钟时间不变。
JVM 单测覆盖空值、Zulu、偏移、固定时区、纯日期、非法输入、surrogate 与奇怪空白。

不做清单：不改 ASR / VoiceAsr / AsrState、AlarmManager / ReminderAlarms /
ReminderAlarmReceiver / ReminderBootReceiver、boot restore 核心路径；
不改 server、不部署、不碰拼好运；
不引入 FCM / Room / Hilt / Pipecat、小组件、语音搜索、提醒完成/取消 API 或云端 TTS。
保留手动 DI，不新增 Gradle 依赖或权限。

## S10 问答 TTS

文字「提问」与语音自动提问成功后，客户端 trim `Answer.answer`，非空才用系统
TextToSpeech 自动播报，不读取 citations；DTO 只有 answer + citations，无 speak 字段。
优先简体中文（zh-CN），无引擎、缺中文语音数据或播报失败时显示可读提示，文字答案仍可阅读。
初始化期间只保留最新答案；新提问停止旧播报，新答案替换队列；长答案按系统单次长度上限分段，
保留完整正文且不拆开 surrogate pair。播报期间显示「停止播报」。

开始语音提问前在页面层 stop TTS；ON_PAUSE 停播并清除待播答案，离开问答页或
onDispose 销毁引擎，logout / clearUi 停播。仅在问答页 RESUMED 时自动播报新答案；
旋转由 ViewModel 保留答案文字，引擎重建，不重播历史答案，不恢复播报进度。
手动 DI，不新增 Gradle 依赖或权限；Manifest queries 与 RecognitionService 并列声明 TTS_SERVICE。
不保存音频文件；使用设备系统引擎，是否联网由系统引擎及设备语音数据决定。

不做清单：不改 ASR / VoiceAsr / AsrState、AlarmManager / ReminderAlarms /
ReminderAlarmReceiver / ReminderBootReceiver、boot restore 核心路径；
不改 server、不部署、不碰拼好运；无云端 TTS 接入，不引入 Pipecat / FCM / Room / Hilt、
小组件、语音搜索或提醒完成/取消 API。

## S9 提醒↔记事双向跳转

时间线提醒卡片的 note_id 经 trim 非空时显示「打开记事」，复用已有详情请求与导航，
加载期间禁用入口。详情页按 note_id 与当前记事 id 精确匹配内存中的 reminders，
只读展示「关联提醒」的标题、触发时间与状态；无关联时隐藏该区块，不提供确认按钮。
纯函数 JVM 测试覆盖空/空白 id、trim、匹配/不匹配/空列表及顺序与状态保留。

不做清单：不改 ASR / VoiceAsr / AsrState、AlarmManager / ReminderAlarms、
boot restore 核心路径；不改 server、不部署、不触碰拼好运；
不引入 FCM / Room / Hilt、小组件、语音搜索或提醒完成/取消 API。
退出登录与 clearDetail 行为保持原样。

## S8 记事详情

时间线「详情」、搜记忆中带非空 note_id 的「打开记事」、问答 citations 的
「打开记事」均通过 Repository.note 调用 `GET /v1/notes/{id}`，沿用 Bearer 与
401 refresh。详情展示 title/text/tags/todos/category/status/recorded_at/rev；
加载期间禁用操作，404 显示可读错误，旧详情在新请求前清空，退出清空详情。
详情可复用编辑页与删除确认；编辑成功更新详情，删除成功返回来源页。
无 note_id 的搜索结果不提供跳转；已删除或不可访问的引用不会降级展示缓存正文。
页面状态仅保留在 ViewModel 内存，不做离线缓存；不改 server、不部署，
不改 ASR、AlarmManager、confirm 或 boot 核心逻辑，无语音搜索、提醒完成/取消 API 或小组件。

## S7 记忆搜索

时间线「问一问」旁新增「搜记忆」，输入文字后调用 `GET /v1/search?q=&top_k=`。
查询 trim 后为 1–500 字，默认 top_k=10（允许 1–50）；展示记忆正文、可选 note_id
与相关度 score，无命中时提示；请求期间禁用输入与搜索。沿用 Bearer 和 401 refresh，
退出清空查询和结果。不提供语音搜索，不改 ASR、闹钟、confirm 或 boot 核心行为。
Mock 登录预置绿植/周报记忆，创建、整理和编辑更新记忆，删除记事移除关联记忆；
大小写不敏感子串匹配，仅返回命中项并截断 topK，退出清空。

## S6 记事编辑/删除

时间线每条记事提供「编辑」「删除」，busy 时禁用；整理轮询期间仍可操作。
编辑页预填正文，保存以原 rev 调用 PATCH；正文不变时保持 rev，变更后清空整理字段、
显示「整理中…」并重新短轮询。版本冲突显示「版本冲突，请同步后重试」，刷新最新版本，
保留编辑正文供检查后重试。不存在/已删除由服务端返回 404。
删除须确认，再调用 DELETE；成功后取消该记事轮询、移除列表项并完整刷新，
通过既有 sync 墓碑消费取消关联提醒的本地闹钟。不改 ASR、闹钟或确认核心路径。

## S5 整理可见闭环

文字或语音 create 成功后立即返回时间线，按响应的 `note_id` 自动短轮询
`GET /v1/notes`：每 1.5 秒一次，最多 20 次、总预算 30 秒（含请求等待）。
列表暂未找到目标时继续等待；状态离开 `uploaded`（包括 organized / failed）即停止。
轮询使用独立协程，不占用全局 busy；仍可浏览、记录、提问及手动「同步」。
终态或超时后完整 sync + notes 刷新，让 suggested 提醒出现并沿用「确认并设闹钟」。
最后一次完整刷新不计入轮询预算。超时保留列表，不作为硬错误，可继续手动同步。
网络失败停止本轮并显示既有可读错误，可手动同步重试；退出登录取消轮询，旋转由 ViewModel 保留。
时间线显示「整理中…」、可读状态、title / tags / todos（含可选 due）与 category。

## S4 语音问答

「问一问」页面新增「语音提问」，复用 S3 的系统 SpeechRecognizer / VoiceAsr，
语言为 `zh-CN`。识别成功先填入问题，再自动调用 Repository.ask → `POST /v1/ask`，
沿用原有答案与 citations 展示；若已有请求进行中，仍保留问题供稍后手动「提问」。
听写期间禁用问题编辑与「提问」，提供状态、「停止并识别」与「取消」。
无引擎、权限拒绝或识别失败会提示改用文字提问，识别结果在请求失败后保留以便重试。
权限与后台释放规则和记事页一致：ON_PAUSE / onStop 释放活动中的识别，
但 Permission 状态保留，避免系统权限弹窗触发后台事件后丢失授权回调；
离开页面时销毁 recognizer，旧回调失效。系统引擎可能联网，不保存录音文件。
默认 debug mock 仍可使用语音或文字提问并展示固定问答，无需连接服务器；
mock 只替代问答 API，语音识别仍需要设备系统引擎。

## S3 语音随手记

进入「记一条」后点「语音记事」，首次使用申请 `RECORD_AUDIO`。使用系统默认
SpeechRecognizer 引擎，语言为 `zh-CN`；设备需安装可用 RecognitionService。
不保证离线识别，系统引擎可能联网处理音频；应用不保存或上传录音文件。
无引擎、拒绝权限、网络异常或未听清时显示提示，可继续文字记事。
听写时显示状态，可「停止并识别」或「取消」；离开页面、退出登录、Activity
进入后台或销毁时取消听写，释放 recognizer，并忽略旧回调；等待权限时的
pause/stop 保留 Permission 状态，允许权限结果继续启动识别。
识别成功直接复用 Repository.create / `POST /v1/notes`，`source="voice_asr"`；
创建成功返回时间线。失败则保留识别文本供编辑、保存重试，未编辑重试沿用请求 ID。
文字记事默认仍为 `source="app"`。不引入 Pipecat、自带模型或后台持续录音。

## S2 提醒闭环

1. 进入时间线或点「同步」时调用 `GET /v1/sync?since=0&limit=100`，真实仓库遍历
   `has_more` 并合并全部分页；界面显示 reminders，suggested 状态提供确认按钮。
2. 「确认并设闹钟」调用 `POST /v1/reminders/{id}/confirm`，body 携带当前 `rev`；S12 确认前可编辑 title/fire_at，仅修改时传覆盖。
   成功后立即消费响应中的 `schedule_reminder` action 设置闹钟，再刷新列表。
3. 同步返回的同类型 action 也会设置/更新闹钟；`deleted.actions`、
   `deleted.reminders` 以及 done/cancelled reminder 会取消对应本地闹钟。
   使用 reminder id 的稳定 hash 作为 requestCode，action id 同时存入本地记录，
   因此可以按 action 墓碑找到并取消闹钟。
4. 尚未触发的提醒以 action/reminder id、标题和触发时间保存在应用私有
   SharedPreferences；触发或退出登录时清除。开机后真实模式若本地会话仍有效，
   先本地 restore 持久化闹钟（不占用广播生命周期等网络），再在后台以约 10 秒预算尝试 sync；超时只保留本地重放。
   仍 pending 的过期闹钟会立即重设，无本地记录的历史 action 则跳过（不当作已响）。
   前台与开机共用 AppRepositories 单例仓库与会话代际；sync 分页按 id 合并并应用墓碑。
   mock 没有跨进程会话，开机任务会跳过。
5. 精确闹钟使用 `setExactAndAllowWhileIdle`，权限不可用或检查后被撤销时退化为
   非精确闹钟。API 33+ 未授予通知权限时，系统闹钟仍会触发，但无法显示通知。

声明权限：`INTERNET`、`POST_NOTIFICATIONS`、`RECEIVE_BOOT_COMPLETED`、
`SCHEDULE_EXACT_ALARM`、`RECORD_AUDIO`。麦克风 feature 非必需；声明语音服务查询。Alarm receiver 不导出；boot receiver 仅处理
`BOOT_COMPLETED`。Doze、厂商省电策略及用户强制停止仍可能影响实际触发。

## 模块与契约

- `:app`：Compose Navigation、提醒列表与确认/取消、AlarmManager、通知、开机重放、
  S13 confirm 草稿 ViewModel 保留、S12 确认前编辑标题/时间、S11 本地可读时间、S10 系统 TTS 答案播报、S9 提醒↔记事跳转与关联提醒（S16 新增取消）、S8 记事详情与搜索/问答引用跳转、S7 记忆搜索、S6 记事编辑/删除、创建后短轮询与整理字段展示、系统 SpeechRecognizer 语音记事与语音提问、ViewModel 保留旋转期间状态、手动 DI。
- `:data`：纯 JVM DTO、Retrofit、仓库、mock、S5 轮询纯函数和测试。接口核对自当前服务端
  `app/schemas.py` 与 `app/api/{auth,notes,ask,sync,reminders}.py`，未知响应字段由 Gson 忽略。
- `:core-security`：Android Keystore MasterKey + EncryptedSharedPreferences。
  access/refresh 成对保存；稳定随机设备 UUID 在退出后保留。应用禁用备份。
- 不为 S5 新建 `:sync`、`:feature-reminder` 等空壳模块。

真实路由：`POST /v1/auth/login`、`POST /v1/auth/refresh`、
`POST /v1/auth/logout`、`POST /v1/notes`、`GET /v1/notes`、`POST /v1/ask`、
`GET /v1/notes/{id}`、`PATCH /v1/notes/{id}`、`DELETE /v1/notes/{id}`、
`GET /v1/search`、`GET /v1/sync`、`POST /v1/reminders/{id}/confirm`、`POST /v1/reminders/{id}/cancel`。
创建记事使用 `client_note_id/text/recorded_at/timezone/source`，返回 `note_id/status/rev`。
记事列表遍历 `next_cursor` 分页；问答使用 `text/client_time/timezone` 并显示 citations；S10 仅在客户端播报 answer 正文，不增加 API 字段。
受保护请求发送 Bearer；401 后串行轮换 refresh 并重试一次。刷新失败即清本地会话，
避免不确定网络结果下重放旧 refresh 导致服务端吊销设备。没有后台刷新或无限重试。
退出尝试服务端吊销并始终清本地会话和闹钟；离线时服务端凭证可能仍有效至过期。
不会记录 token、邀请码、请求正文或响应日志。

## Mock 行为与测试

Mock create 返回 client_note_id 作为 note_id，初始状态 uploaded；默认第 3 次 notes()
查询自动整理并填入 title/tags/todos，新增一条关联的 suggested 演示提醒。
可用 organizeAfterReads=0 禁用自动整理，或 organizeNote 手动整理供 JVM 测试。
Mock 登录后预置「给绿植浇水」「整理周报」两个 suggested 提醒。确认会将其改为
scheduled、bump rev，生成 `schedule_reminder` action 并推进 cursor；重复确认当前
scheduled rev 保持幂等；取消会将 suggested/scheduled 改为 cancelled、bump rev，
墓碑 schedule action 并生成 cancel action，当前 cancelled rev 再取消保持幂等。mock 维护 note/action/reminder 删除墓碑；编辑使 suggested 提醒失效，删除记事删除所有关联提醒与 actions。
记事、提醒、actions 和 cursor 仅在当前进程内存保存，退出或进程结束后清空；
已经设置的闹钟记录仍在私有 SharedPreferences，退出时会主动清除。

`:data:test` 使用 JUnit + MockWebServer：覆盖 mock 登录校验、会话隔离、创建幂等、
长度校验、固定问答、sync 空页/确认/action 与删除墓碑；真实 Retrofit 请求字段/路径/
分页/鉴权、cancel rev/取消墓碑/幂等/不可取消、confirm rev 与可选修改、refresh 轮换及失败清理、可读 API 错误。
契约测试不连接真实服务，也不替代真机或服务端端到端验收。

## 明确不做 / 边界

- 不引入 FCM、Room、离线写队列、Hilt；不做 Backfill confirmation subscriptions。
- S16 同步新增服务端 cancel API；不部署服务端，服务端只负责产生 sync action，不执行系统闹钟。
- 创建后自动短轮询；无常驻后台同步，另保留进入/刷新时间线、confirm/cancel 后刷新和 boot 同步。
- `rrule` 目前只解析不执行，闹钟均为一次性；仅确认前可改标题/时间，没有已确认提醒任意编辑或重复提醒 UI；主列表、详情与通知支持「10分钟后」贪睡。
- 尚无推送或发布签名；自动短轮询结束后仍可手动同步。
- 同一份未修改草稿在失败重试时保留 `client_note_id`，但编辑草稿会生成新 ID；
  网络结果不确定时请先刷新时间线，避免编辑后重复记录。
- 页面状态可跨旋转保留，不能跨进程恢复；不保存邀请码或草稿到磁盘。
- Keystore 异常/设备 UUID 的真实持久性仍需设备测试；损坏的安装可清除应用数据后重登录。
- UI/无障碍、弱网、通知权限、精确/非精确闹钟、Doze、开机重放仍需模拟器/真机验收。
  不要把本脚手架视为完整生产客户端。

## Wrapper 来源

`gradlew`、`gradlew.bat` 和 `gradle-wrapper.jar` 原样来自官方
[Gradle v8.7.0](https://github.com/gradle/gradle/tree/v8.7.0)。
JAR SHA-256：`cb0da6751c2b753a16ac168bb354870ebb1e162e9083f116729cec9c781156b8`。
`gradle-wrapper.properties` 固定官方 8.7 bin 分发包及官方 SHA-256，首次运行下载。

## 本次环境验证

使用 `/home/box/.local/jdk/jdk-17.0.16+8` 与 `/home/box/.local/android-sdk`
（`GRADLE_USER_HOME=/tmp/s16-gradle`）。
`./gradlew --no-daemon :data:test :app:testDebugUnitTest :app:assembleDebug`：`BUILD SUCCESSFUL`。
`:data` RepositoryTest：**30** + OrganizePollTest：**9**（共 39），0 failures / 0 errors / 0 skipped；
`:app` ConfirmOverrideTest：**8** + DisplayTimeTest：**6** + AnswerTtsTest：**7** + AsrStateTest：**6** + VoiceQuestionTest：**2** + EditBehaviorTest：**5** + ReminderNoteLinkTest：**4** + AppRepositoriesTest：**2** + ReminderStatusTest：**3**（共 43），0 failures / 0 errors / 0 skipped；
`:core-security` unit tests：`NO-SOURCE`。
服务端 `tests/test_subscription_channel.py` 等取消相关用例通过；`python3 scripts/check-invariants.py`：`0 error(s), 0 warning(s)`。
应用标签与标题更新为 S16；新增 cancel API / Mock / UI；历史 S15 及更早章节保留。
未运行模拟器/真机或连接实际服务的端到端测试；本刀不部署。

## S17 提醒完成

- API：`POST /v1/reminders/{id}/complete`，body 为当前 `rev`；suggested/scheduled → done，rev+1。当前 done rev 幂等，旧 rev 返回 409。
- Mock 与 RealRepository 提供 `completeReminder`；Mock 墓碑 schedule action，并经 sync 返回 `complete_reminder`。
- 主列表与详情的关联提醒在 suggested/scheduled 时显示「完成」，与「取消」并列，busy 时禁用。
- 完成后经 `AppRepositories.invalidateAlarms { alarms.cancelReminder(id) }` 栅栏取消本地闹钟，再刷新；sync 消费 `complete_reminder` 同样取消闹钟。

里程碑 / changelog：S17 补齐提醒完成闭环；保留 S16 取消及更早历史。

## S18 通知操作

- 提醒响铃通知提供「完成」「取消」，使用独立、不导出的 `ReminderNotificationActionReceiver`；
  不同提醒和操作通过 action + URI + 独立 requestCode 区分 PendingIntent。
- 会话有效时先 sync 取当前 rev，再调用既有 complete/cancel API；同步前后与成功回调检查登录状态与 sessionGeneration。
- 仅 API 成功后经 `AppRepositories.invalidateAlarms { alarms.cancelReminder(id) }` 清除本地闹钟并 dismiss 该通知（对齐主界面完成/取消）。
- 网络失败、版本冲突或约 8 秒超时只记安全错误类型并 Toast，保留通知供重试；失败不误清闹钟。广播 `goAsync` + IO 协程，结束时 `finish`。
- 应用标签与页面标题统一为 S18。JVM 单测覆盖操作路由、sync rev 匹配、会话栅栏、失败不跑成功回调及取消传播。

里程碑 / changelog：S18 通知快捷完成/取消；保留 S17 完成及更早历史。


## S19 通知贪睡

- 通知在「完成 / 取消」旁新增「10分钟后」；先 sync 获取当前 rev，再调用 `POST /v1/reminders/{id}/snooze`（`rev` 必填，`minutes` 默认 10，范围 1–1440）。
- suggested/scheduled → scheduled，fire_at 为锁内 UTC now + minutes，rev+1；经 Subscription → 原 WorkItem → Channel 更新原 `schedule_reminder` payload 与 change_seq，不产生新 action 类型或 schedule 墓碑。
- Trigger dedupe 为 `reminder_snooze:{id}:r{rev}:m{minutes}`：同一请求重放返回 200 / duplicate=true 及当前 reminder/action，不再次改时间；不同 dedupe 的旧 rev 返回 409。Mock 重复旧 rev 返回冲突，用新 rev 可再次贪睡。
- API 成功后在 sessionGeneration / invalidateAlarms 栅栏内取消旧闹钟、按响应重设闹钟并 dismiss 通知。失败保留闹钟与通知，Toast 提示，异常日志仅含类名。
- JVM 测试覆盖 Real 请求/响应、Mock 多次贪睡和 action 更新、通知路由与会话栅栏；服务端覆盖同步、重放、rev、状态、归属、参数边界及 PG 并发。

本刀不做：主列表/详情贪睡按钮、rrule 执行、二次确认、FCM/Room/Hilt/Pipecat、改 ASR/TTS 核心、根路径 `GET /`；不部署。

里程碑 / changelog：S19 通知贪睡闭环；保留 S18 及更早历史。


## S20 主列表/详情贪睡

- 提醒主列表与记事详情「关联提醒」对 suggested/scheduled 显示「10分钟后」，与「完成」「取消」并列；busy 时禁用。
- 点击复用 S19 的 `Repository.snoozeReminder(id, rev, minutes = 10)`，不改 data / Mock / API 实现。
- 成功后经 `AppRepositories.invalidateAlarms` 取消旧闹钟并调用 `alarms.snoozed(response)` 重设，再刷新列表；API 失败由既有 `run` 显示可读错误，不清闹钟。
- `reminderCanSnooze` 纯函数供两处 UI 共用；JVM 单测覆盖 suggested/scheduled 可操作，其它状态不可操作。应用标签与标题更新为 S20。

本刀不做：rrule 执行、二次确认、FCM/Room/Hilt/Pipecat、改 ASR/TTS、改 server、根路径 `GET /`；不部署、不改 S19 通知栏操作。

里程碑 / changelog：S20 补齐主列表与详情贪睡入口；保留 S19 及更早历史。


## S21 通知栏操作后前台自动刷新

- `AppRepositories` 提供进程内轻量回调订阅，通知携带当前 sessionGeneration；无订阅时可安全发送。
- 通知栏完成 / 取消 / 贪睡成功后，在原 `ifCurrent` 栅栏内撤通知并发送提醒变更信号，保留既有闹钟处理。
- `AgentModel` 订阅后通过 viewModelScope 调用既有 refresh，等待 refreshMutex 后再次校验登录态与事件 generation；不因 busy 丢弃事件，不依赖 ON_RESUME。ViewModel 清理时取消订阅，logout 后旧事件由会话栅栏丢弃。
- JVM 测试覆盖信号 generation、取消订阅、会话切换与迟到事件；保留既有 AppRepositories 测试。

本刀不做：FCM/Room/Hilt/Pipecat、rrule、根路径 `GET /`、二次确认、data / Mock / API / server 改动、ASR/TTS 改动、routine；不改通知按钮集合或 API 语义，不部署、不合入。

里程碑 / changelog：S21 补齐通知遮罩未触发 ON_RESUME 时的前台自动刷新；保留 S20 及更早历史。


## S22 提醒主列表筛选

- 主提醒列表默认只显示 suggested / scheduled，精确匹配，不去空白、不忽略大小写；done / cancelled / 空值 / 未知状态均隐藏。
- 提醒标题旁提供 Material3「显示已结束」开关，开启后展示全部状态；showEnded 保存在 AgentModel，默认 false，配置变更 / 旋转后保留。
- 筛选仅用于主列表展示；ViewModel 仍按 fire_at 排序并保存完整同步列表，记事详情「关联提醒」保留完整历史。S22 当时在筛选后为空且非 busy 时显示「暂无提醒。」；S23 的文案调整见下节。
- reminderIsActive / filterRemindersForList 纯函数的 JVM 测试覆盖严格状态匹配、全部展示与顺序保持；应用标签与标题更新为 S22。

本刀不做：FCM/Room/Hilt/Pipecat、rrule、根路径 `GET /`、二次确认、data / Mock / API / server 改动；不改通知栏操作或贪睡分钟，不部署、不合入。

里程碑 / changelog：S22 增加主列表进行中筛选与「显示已结束」开关；保留 S21 及更早历史。


## S23 提醒列表筛选空状态文案

- 无提醒时显示「暂无提醒。」；关闭「显示已结束」、存在非进行中提醒且筛选结果为空时，显示「暂无进行中提醒。可开启「显示已结束」查看。」。
- reminderListEmptyMessage 纯函数接收完整提醒列表与 showEnded；其他情况返回「暂无提醒。」。主列表仅在可见列表为空且非 busy 时展示文案。
- JVM 测试覆盖空列表、仅进行中、仅已结束、开启显示已结束及混合状态；应用标签与标题更新为 S23。
- 保持 S22 的严格状态匹配、筛选顺序、FilterChip 和记事详情完整历史行为。

本刀不做：FCM/Room/Hilt/Pipecat、rrule、根路径 `GET /`、二次确认、data / Mock / API / server、ASR/TTS、部署；不改通知栏操作或贪睡。

里程碑 / changelog：S23 区分无提醒与已结束提醒被筛选隐藏的空状态；保留 S22 及更早历史。

## S24 提醒完成与取消二次确认

- 行为：主列表及详情页关联提醒中，suggested/scheduled 的「完成」「取消」先打开 AlertDialog，点击「确认」后才调用现有 completeReminder / cancelReminder；「取消」或关闭不执行动作。
- busy 时禁用确认与取消按钮，并阻止对话框关闭；确认后先清除待确认动作，再执行现有状态更新。应用标签与顶部标题更新为 S24。
- 测试：`ReminderActionConfirmTest` 覆盖两种动作的中文标题、正文及 busy 确认/关闭门禁；运行 `python3 scripts/check-invariants.py` 和 `cd android && ./gradlew --no-daemon :data:test :app:testDebugUnitTest :app:assembleDebug`。

本刀不做：通知栏操作、snooze 分钟、筛选/空状态、确认并设闹钟流程、data / Mock / API / status 规则 / server、FCM/Room/Hilt/Pipecat、rrule、根路径 `GET /`、ASR/TTS；不部署、不合入。

里程碑 / changelog：S24 为列表与详情页的完成/取消增加二次确认，降低误触；保留 S23 及更早历史。


## S25 详情页返回文案

- 详情页顶部导航钮从「取消」改回「返回」：S24 #51 误改后与「取消提醒」同名易误导。仅改文案，点击仍执行 `clearDetail()` + `popBackStack()`，busy 门禁保持原样。
- 应用标签与顶部标题更新为 S25；删除确认、提醒二次确认、编辑页、语音与 ConfirmDialog 等其它「取消」文案保持原样。

本刀不做：通知栏、完成/取消二次确认逻辑、筛选/空状态、API / server / data、FCM/Room/Hilt/Pipecat、根路径 `GET /`；不部署、不合入。

里程碑 / changelog：S25 恢复详情页顶部导航「返回」文案；保留 S24 及更早历史。


## S26 提醒操作钮文案消歧

- 提醒主列表与记事详情页关联提醒的操作按钮：「取消」→「取消提醒」、「完成」→「完成提醒」，避免与对话框 dismiss「取消」混淆。
- 贪睡 /「10分钟后」保持不变；详情页「返回」与「确认并设闹钟」保持不变。
- 对话框 dismiss、删除确认、提醒二次确认（ReminderActionConfirmDialog）、编辑页、语音与 ConfirmReminderDialog 的「取消」保持不变；提醒二次确认标题与消息不变。
- 应用标签更新为「随记 S26」，顶部标题更新为「随记 · S26」；仅 Android 文案调整。

本刀不做：筛选/空状态、二次确认逻辑、通知栏 PendingIntent、API / server / data、FCM/Room/Hilt/Pipecat、根路径 `GET /`；不部署、不合入。

里程碑 / changelog：S26 提醒主列表与详情页操作钮文案消歧；保留 S25 及更早历史。


## S27 贪睡按钮文案

- 提醒主列表与记事详情页关联提醒的贪睡按钮：「10分钟后」→「贪睡10分钟」；仍调用 `model.snoozeReminder`，贪睡分钟数保持 10，仅改文案、行为不变。
- 应用标签更新为「随记 S27」，顶部标题更新为「随记 · S27」。
- 通知栏 `ReminderNotificationAction` 仍为「10分钟后」，保持不变。

本刀不做：不改通知栏 PendingIntent、「取消提醒」/「完成提醒」、对话框 dismiss「取消」、二次确认逻辑、snooze 分钟、API / server / data、FCM/Room/Hilt/Pipecat、筛选/空状态、根路径 `GET /`、rrule；不部署、不合入。

里程碑 / changelog：S27 提醒主列表与详情页贪睡按钮文案明确为「贪睡10分钟」；保留 S26 及更早历史。
