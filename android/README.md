# Android S5 · 整理可见闭环

紧凑的 Kotlin / Compose 客户端：登录、时间线、文字记事、问答、提醒同步与确认；
确认后的 `schedule_reminder` Channel action 会落到 AlarmManager，响铃时显示系统通知。
系统 SpeechRecognizer 中文语音记事与语音问答。minSdk 26，compile/targetSdk 34，JDK 17，Gradle 8.7，AGP 8.5.2。

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
2. 「确认并设闹钟」调用 `POST /v1/reminders/{id}/confirm`，body 携带当前 `rev`。
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

- `:app`：Compose Navigation、提醒列表与确认、AlarmManager、通知、开机重放、
  S5 创建后短轮询与整理字段展示、系统 SpeechRecognizer 语音记事与语音提问、ViewModel 保留旋转期间状态、手动 DI。
- `:data`：纯 JVM DTO、Retrofit、仓库、mock、S5 轮询纯函数和测试。接口核对自当前服务端
  `app/schemas.py` 与 `app/api/{auth,notes,ask,sync,reminders}.py`，未知响应字段由 Gson 忽略。
- `:core-security`：Android Keystore MasterKey + EncryptedSharedPreferences。
  access/refresh 成对保存；稳定随机设备 UUID 在退出后保留。应用禁用备份。
- 不为 S5 新建 `:sync`、`:feature-reminder` 等空壳模块。

真实路由：`POST /v1/auth/login`、`POST /v1/auth/refresh`、
`POST /v1/auth/logout`、`POST /v1/notes`、`GET /v1/notes`、`POST /v1/ask`、
`GET /v1/sync`、`POST /v1/reminders/{id}/confirm`。
创建记事使用 `client_note_id/text/recorded_at/timezone/source`，返回 `note_id/status/rev`。
记事列表遍历 `next_cursor` 分页；问答使用 `text/client_time/timezone` 并显示 citations。
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
scheduled rev 保持幂等。mock 还维护 action/reminder 删除墓碑供增量同步测试。
记事、提醒、actions 和 cursor 仅在当前进程内存保存，退出或进程结束后清空；
已经设置的闹钟记录仍在私有 SharedPreferences，退出时会主动清除。

`:data:test` 使用 JUnit + MockWebServer：覆盖 mock 登录校验、会话隔离、创建幂等、
长度校验、固定问答、sync 空页/确认/action 与删除墓碑；真实 Retrofit 请求字段/路径/
分页/鉴权、confirm rev 与可选修改、refresh 轮换及失败清理、可读 API 错误。
契约测试不连接真实服务，也不替代真机或服务端端到端验收。

## 明确不做 / 边界

- 不引入 FCM、Room、离线写队列、Hilt；不做 Backfill confirmation subscriptions。
- 不修改 `server/`，不部署服务端；服务端只负责产生 sync action，不执行系统闹钟。
- 创建后自动短轮询；无常驻后台同步，另保留进入/刷新时间线、confirm 后刷新和 boot 同步。
- `rrule` 目前只解析不执行，闹钟均为一次性；没有提醒编辑、贪睡或重复提醒 UI。
- 尚无编辑/删除记事、推送或发布签名；自动短轮询结束后仍可手动同步。
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

使用 `/home/box/.local/jdk/jdk-17.0.16+8` 与 `/home/box/.local/android-sdk`。
`./gradlew --no-daemon :data:test :app:testDebugUnitTest :app:assembleDebug`：`BUILD SUCCESSFUL`。
`:data` RepositoryTest：**13** + OrganizePollTest：**5**（共 18），0 failures / 0 errors / 0 skipped；
`:app` AsrStateTest：**6** + VoiceQuestionTest：**2**（共 8），0 failures / 0 errors / 0 skipped；
`:core-security` unit tests：`NO-SOURCE`。
`python3 scripts/check-invariants.py`：`0 error(s), 0 warning(s)`。
应用标签更新为 S5；create 返回 `Created` 后短轮询 notes，终态/超时再完整 sync。
未运行模拟器/真机、Keystore instrumentation 或连接实际服务的端到端测试。
会话与同步安全：前台与开机共用 `AppRepositories.real`（进程级单例仓库，同一 Mutex），
并以 session generation 栅栏防止 logout 后在途 boot sync / 整理轮询再设闹钟或写回。
分页 sync 按 ID 合并，后页覆盖前页，并应用 deleted 墓碑剔除。
