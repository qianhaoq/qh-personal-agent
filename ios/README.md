# 随记 iOS 验证壳 · d5d2860

基于 main@d5d2860，供钱浩本机验证。只含邀请码登录、登出、最近最多 50 条笔记与文本提问；非完整 Android 移植。不含 ASR、闹钟、通知、FCM 或完整个人流。

API base 固定为 `https://agent.pinhaoyun.com/`（保留结尾 `/`），所有请求路径为相对路径 `v1/...`。HTTPS 使用系统默认 ATS，无放宽配置。无第三方依赖，无需 XcodeGen。

## Mac 构建

需要完整 Xcode 15+、iOS 17+ SDK 及对应模拟器运行时。若当前命令行工具未指向 Xcode，先执行 `sudo xcode-select -s /Applications/Xcode.app/Contents/Developer`，并完成 Xcode 首次启动配置。

在仓库根目录执行：

```sh
xcodebuild -project ios/PersonalAgent.xcodeproj -scheme PersonalAgent -sdk iphonesimulator -configuration Debug -derivedDataPath /tmp/pa-sim build
```

可用 Xcode 打开 `ios/PersonalAgent.xcodeproj`，选择模拟器并 Run；或启动一个 iOS 17+ 模拟器后执行：

```sh
xcrun simctl install booted /tmp/pa-sim/Build/Products/Debug-iphonesimulator/PersonalAgent.app
xcrun simctl launch booted ai.qianhao.personalagent
```

## 真机 archive / export

在 Xcode Settings → Accounts 登录有 `3KWH9L7X2T` 团队权限的 Apple ID；确保该团队可注册 `ai.qianhao.personalagent`、开发证书与设备。真机开启开发者模式，连接并信任 Mac，在 Xcode Devices 中登记设备。自动签名：`Apple Development`，Deployment Target：17.0。

在仓库根目录执行（产物放 `/tmp`，不提交）：

```sh
xcodebuild -project ios/PersonalAgent.xcodeproj -scheme PersonalAgent -configuration Release -destination 'generic/platform=iOS' -archivePath /tmp/PersonalAgent.xcarchive -allowProvisioningUpdates archive
xcodebuild -exportArchive -archivePath /tmp/PersonalAgent.xcarchive -exportPath /tmp/pa-development -exportOptionsPlist ios/ExportOptions-development.plist -allowProvisioningUpdates
```

导出 plist 使用要求的 `method=development`、`signingStyle=automatic`、`teamID=3KWH9L7X2T`。较新 Xcode 可能提示 development 已更名为 debugging；该模板保留指定值。开发包仅可安装到相应开发描述文件包含的设备，也可直接用 Xcode Run 到真机。

## 手工验证

1. 顶部确认 API base 和「随记 iOS 验证壳 · d5d2860」。
2. 输入无效邀请码，确认显示服务端 `error.message`；输入自行持有的有效邀请码登录。
3. 确认自动获取笔记，显示 title（无标题时正文前 120 字）及 status；可点「刷新笔记」。空列表显示「暂无笔记」。
4. 输入问题并提问，确认展示 answer。请求包含 ISO8601 `client_time` 与 `Asia/Shanghai`。
5. 关闭重开 App，确认会话保留并重新拉取笔记。登出后确认回到登录页，能再次登录。

access/refresh token 作为一份 Codable 数据保存在本机 UserDefaults（验证壳取简单方案，不适用于正式发布的凭证存储）；device_id 为持久 UUID。邀请码不持久化。此壳不自动刷新 token，过期后通过「登出」重新登录；登出调用服务端 `POST v1/auth/logout`，即使网络失败也清除本机 token 并提示。服务端登出会撤销当前设备的 refresh token。请求串行，执行中禁用操作，避免旧请求覆盖新会话。无凭证或响应正文日志。

本仓创建环境为 Linux，无 Xcode/iOS SDK；工程结构与 plist 可静态检查，模拟器编译、真机签名及现网登录仍须在 Mac 按上述步骤验证。
