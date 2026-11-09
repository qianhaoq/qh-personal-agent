# 随记 iOS · 单一个人流改版

暖白纸面、青绿主操作的 SwiftUI 客户端。登录后只有一个个人流：真实服务端笔记与当前进程内的问答按时间排列，底部固定输入坞可显式选择「记下 / 提问」。普通 UI 不显示 API 地址、调试 SHA、token 或“验证壳”信息。

设计与状态契约见 [`docs/ios-redesign/01-kimi-ios-design.md`](../docs/ios-redesign/01-kimi-ios-design.md)。

## 能力边界

| 能力 | 状态 | 说明 |
| --- | --- | --- |
| 邀请码登录 / 退出登录 | 可用 | 退出需二次确认；服务端退出失败时仍会清本机会话并明确提示 |
| 最近笔记 | 可用 | 自动或手动读取最近接口返回的最多 50 条，按记录时间倒序展示 |
| 文字记下 | 可用 | 1–5000 字；创建后 GET 单条真实笔记，再进入个人流 |
| 整理状态 | 可用 | `uploaded / organized / failed` 映射为中文，不提供虚构进度或“重试整理” |
| 文字提问 | 可用 | 1–2000 字；显示答案及服务端返回的来源摘录 |
| 失败重试 | 可用 | 列表读取、未完成创建、单次问答分别重试；不自动重试问答 |
| 语音输入 | **禁用** | 按钮不可点击并常驻说明；无 ASR、无麦克风权限请求 |
| 提醒 / 闹钟 / 通知 | **未接入** | 不提供确认、排程、权限或成功状态 |
| 笔记搜索 / 详情 / 编辑 / 删除 | **未接入** | 不提供无效入口 |
| TTS / 持久多轮聊天 | **未接入** | 回答仅供阅读，问答仅存当前进程 |
| 自动 refresh token | **未接入** | access token 过期后重新登录 |
| 完整离线缓存 / 跨进程恢复 | **未接入** | 草稿、问答和未完成创建恢复项均不持久化 |

仅连接以下现有接口：

- `POST /v1/auth/login`
- `POST /v1/auth/logout`
- `GET /v1/notes?limit=50`
- `POST /v1/notes`
- `GET /v1/notes/{id}`
- `POST /v1/ask`

API base 固定为 `https://agent.pinhaoyun.com/`（保留结尾 `/`），请求使用相对路径 `v1/...`。HTTPS 使用系统默认 ATS，无放宽配置。无第三方依赖，无需 XcodeGen。`client_time`、笔记 `recorded_at` 使用 ISO8601，时区使用设备当前 IANA identifier。

## 诚实状态与重试

- 请求串行；忙碌时仍可编辑下一份文字，但发送、刷新、重试和登出禁用，不自动排队。
- 创建原文先进入“原文保存”卡，不提前伪装成服务器笔记。
- 创建网络结果未知时明确“服务器可能已接收”，手动重试沿用同一 `client_note_id`。
- 创建返回 202 但单条 GET 失败时显示“服务端已接收原文”，重试只 GET，不重复 POST。
- 明确 4xx 拒绝可“取回编辑”；问答失败保留原问题，由用户明确重新提问。
- 读取失败不转换成空列表；只有成功读取且无内容、无错误、无未完成创建时才显示空态。
- 问答、未完成创建与未发送草稿只保留在当前进程，进程终止后不会恢复。

## 视觉核验与示例数据

Xcode Preview 提供：

1. `登录`：无调试信息的新登录页。
2. `空个人流`：纸张插画、两个文字入口、固定输入坞；空态节点带 `personal-flow-empty` accessibility identifier。
3. `示例内容`：带笔记、回答和来源摘录的个人流。

示例仅由 `#if DEBUG` Preview 工厂创建，顶部和每张卡片均标「示例」，不会进入生产初始化或真实用户数据流。Preview 是核验入口，不替代模拟器截图和真机测试。

## Mac 构建

需要完整 Xcode 15+、iOS 17+ SDK 及对应模拟器运行时。若命令行工具未指向 Xcode，先执行 `sudo xcode-select -s /Applications/Xcode.app/Contents/Developer`，并完成 Xcode 首次启动配置。

在仓库根目录执行：

```sh
xcodebuild -project ios/PersonalAgent.xcodeproj -scheme PersonalAgent -sdk iphonesimulator -configuration Debug -derivedDataPath /tmp/pa-sim build
```

可用 Xcode 打开 `ios/PersonalAgent.xcodeproj`，选择模拟器并 Run；或启动 iOS 17+ 模拟器后执行：

```sh
xcrun simctl install booted /tmp/pa-sim/Build/Products/Debug-iphonesimulator/PersonalAgent.app
xcrun simctl launch booted ai.qianhao.personalagent
```

## 真机 archive / export

在 Xcode Settings → Accounts 登录有 `3KWH9L7X2T` 团队权限的 Apple ID；确保团队可注册 `ai.qianhao.personalagent`、开发证书与设备。真机开启开发者模式，连接并信任 Mac，在 Xcode Devices 中登记设备。自动签名：`Apple Development`，Deployment Target：17.0。

在仓库根目录执行（产物放 `/tmp`，不提交）：

```sh
xcodebuild -project ios/PersonalAgent.xcodeproj -scheme PersonalAgent -configuration Release -destination 'generic/platform=iOS' -archivePath /tmp/PersonalAgent.xcarchive -allowProvisioningUpdates archive
xcodebuild -exportArchive -archivePath /tmp/PersonalAgent.xcarchive -exportPath /tmp/pa-development -exportOptionsPlist ios/ExportOptions-development.plist -allowProvisioningUpdates
```

导出 plist 使用 `method=development`、`signingStyle=automatic`、`teamID=3KWH9L7X2T`。较新 Xcode 可能提示 development 已更名为 debugging；模板保留指定值。开发包只能安装到开发描述文件包含的设备，也可直接由 Xcode Run 到真机。

## 手工验收

1. 核对登录页只有邀请码登录与能力说明，没有 API、SHA 或调试标记；无效邀请码在页内失败，有效邀请码进入个人流。
2. 成功读取的空账户显示完整空态；断网失败显示读取错误与“重新读取”，不能显示空态。
3. 用文字记下一条，确认先出现保存卡，再显示 GET 返回的真实正文、整理状态、标题、标签与待办。
4. 提问后确认等待卡更新为答案；有 citation 时可展开来源摘录。失败时原问题保留，可手动重新提问。
5. 忙碌期间继续输入下一份文字，确认旧响应不会清新稿，发送在忙碌期间不可用。
6. 语音按钮不可点击并显示原因；设置中语音、提醒与闹钟均为“未接入”，无相关权限弹窗或成功提示。
7. 关闭重开 App，确认登录会话保留并重新读取笔记，旧问答与未完成创建不恢复。
8. 从设置退出，确认二次提示与数据说明；网络失败时本机仍退出，同时显示服务端退出未成功。
9. 补测大字体、VoiceOver、键盘、横屏和实际网络异常；Preview 不代表这些验收已通过。

## 本机存储与当前限制

access/refresh token 作为一份 Codable 数据保存在本机 UserDefaults，沿用最小壳方案，不作为正式发布凭证存储方案；`device_id` 为持久 UUID。邀请码不持久化，无凭证或笔记正文日志。

登出会调用服务端撤销当前设备会话；无论调用成功与否都清本机 token、草稿、问答和未完成创建。所有响应写回带当前会话代次检查，旧会话内容不写回新会话。

本次改版执行环境为 Linux，无 Xcode/iOS SDK；只能做工程、plist 与源码静态检查。模拟器编译、截图、真机签名及现网登录必须按上述步骤在 Mac 验证，未执行前不得声称通过。
