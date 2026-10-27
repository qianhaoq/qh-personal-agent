# Android S1 · 随记

紧凑的 Kotlin / Compose 客户端：登录、时间线、文字记事、问答；录音按钮仅作占位。
minSdk 26，compile/targetSdk 34，JDK 17，Gradle 8.7，AGP 8.5.2。

## 安装与运行

1. 安装 Android Studio，使用 JDK 17（Gradle JDK 也选 17）。SDK Manager 安装
   Android SDK Platform 34、Build Tools 34.0.0、Platform Tools，以及 API 26+ 模拟器。
2. 在 Android Studio 打开本目录，等待 Gradle sync。命令行使用 `ANDROID_HOME`
   指向 SDK，或在 **本目录** `local.properties` 设置 `sdk.dir=/absolute/path/to/sdk`。
3. 启动模拟器，选择 `app` 的 debug variant 后 Run。默认无需服务器：debug 的
   `USE_MOCK_API=true`，任意非空字符串均可登录（例如 `demo`，不是实际邀请码）。

从本目录运行：

```sh
./gradlew :app:assembleDebug
./gradlew --no-daemon assembleDebug testDebugUnitTest
./gradlew :data:test                 # 纯 JVM mock / Retrofit 契约与会话测试
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

## 模块与契约

- `:app`：Compose Navigation、四个页面、ViewModel 保留旋转期间状态、手动 DI。
- `:data`：纯 JVM DTO、Retrofit、仓库、mock 和测试。接口核对自当前服务端
  `app/schemas.py` 与 `app/api/{auth,notes,ask}.py`，未知响应字段由 Gson 忽略。
- `:core-security`：Android Keystore MasterKey + EncryptedSharedPreferences。
  access/refresh 成对保存；稳定随机设备 UUID 在退出后保留。应用禁用备份。
- 计划模块见 `settings.gradle.kts`：`:sync`、`:feature-capture`、`:feature-asr`、
  `:feature-entry`、`:feature-reminder`，S1 不创建空壳模块。

真实路由：`POST /v1/auth/login`、`POST /v1/auth/refresh`、
`POST /v1/auth/logout`、`POST /v1/notes`、`GET /v1/notes`、`POST /v1/ask`。
创建使用 `client_note_id/text/recorded_at/timezone/source`，返回 `note_id/status/rev`。
列表遍历 `next_cursor` 分页；问答使用 `text/client_time/timezone` 并显示 citations。
受保护请求发送 Bearer；401 后串行轮换 refresh 并重试一次。刷新失败即清本地会话，
避免不确定网络结果下重放旧 refresh 导致服务端吊销设备。没有后台刷新或无限重试。
退出尝试服务端吊销并始终清本地会话；离线时服务端凭证可能仍有效至过期。
不会记录 token、邀请码、请求正文或响应日志。

## 测试与边界

`:data:test` 使用 JUnit + MockWebServer：mock 登录校验、会话隔离、创建幂等、
长度校验、固定问答；真实 Retrofit 请求字段/路径/分页/鉴权、refresh 轮换及失败清理、
可读 API 错误。契约测试不连接真实服务，也不替代真机或服务端端到端验收。

Mock 仅在当前进程内存保存记事，退出或进程结束后清空；真实模式仅 token 持久化。
尚无 Room、离线队列、后台 sync、自动轮询、编辑/删除、ASR、录音权限、提醒调度、
推送或发布签名。后台提取完成后需手动刷新时间线。
同一份未修改草稿在失败重试时保留 `client_note_id`，但编辑草稿会生成新 ID；
网络结果不确定时请先刷新时间线，避免编辑后重复记录。
页面状态可跨旋转保留，不能跨进程恢复；不保存邀请码或草稿到磁盘。
Keystore 异常/设备 UUID 的真实持久性仍需设备测试；损坏的安装可清除应用数据后重登录。
UI/无障碍、弱网与设备测试尚需后续验证。不要把本脚手架视为完整生产客户端。

## Wrapper 来源

`gradlew`、`gradlew.bat` 和 `gradle-wrapper.jar` 原样来自官方
[Gradle v8.7.0](https://github.com/gradle/gradle/tree/v8.7.0)。
JAR SHA-256：`cb0da6751c2b753a16ac168bb354870ebb1e162e9083f116729cec9c781156b8`。
`gradle-wrapper.properties` 固定官方 8.7 bin 分发包及官方 SHA-256，首次运行下载。

## 本次环境验证

工具链在仓库外 `/tmp/qh-android-tools`（Temurin JDK 17 + Android SDK 34）。
已运行 `./gradlew :app:assembleDebug
./gradlew --no-daemon assembleDebug testDebugUnitTest assembleRelease`：
`BUILD SUCCESSFUL`；JUnit 6 tests，0 failures，0 errors，0 skipped。
已核对合并后的 manifest：debug 允许明文、release 禁止明文，二者均禁用备份；
生成的 BuildConfig：debug 默认 mock，release 强制真实 API。
`python3 scripts/check-invariants.py`：0 errors / 0 warnings。
未运行模拟器/真机、Keystore instrumentation 或连接实际服务的端到端测试。
