# 随记 iOS · 以对话为首页的本机助手

> ⚠️ **重大产品缺口：无邀请码的本机模式没有任何 AI 回答，也没有同步。** 本机对话只做记录、存任务/记忆和逐字查找，不是完整的 AI 助手。AI 问答只在可选的云端账户里，云端账户仍**必须用邀请码**登录。本版本**不含语音听写**。

SwiftUI 客户端。打开即是一条**本机对话**：无需邀请码、不联网，内容只保存在这台设备、未同步。任何一条消息都可显式「存为任务 / 存为记忆」，在「任务与记忆」里编辑、完成、删除/忘记，并逐字查找。**云端账户**是左上角的可选 Sheet，沿用既有邀请码登录、云端笔记与提问，且不会上传本机内容。

- 设计与能力矩阵：[`docs/ios-redesign/public-a-design.md`](../docs/ios-redesign/public-a-design.md)
- 独立评审与修复：[`docs/ios-redesign/public-a-review.md`](../docs/ios-redesign/public-a-review.md)
- 验证证据与待办：[`docs/ios-redesign/public-a-validation.md`](../docs/ios-redesign/public-a-validation.md)

旧版纸面设计 `01-kimi-ios-design.md` 已被取代。

## 文件

| 文件 | 职责 |
| --- | --- |
| `PersonalAgentApp.swift` | `@main`，启动 `RootView`（不强制浅色） |
| `HomeView.swift` | `RootView`、本机对话首页与输入栏 |
| `LibraryView.swift` | 任务与记忆：新建、编辑、完成、删除/忘记、逐字查找 |
| `LocalModel.swift` | 本机状态，`@MainActor`，只读写本机文件 |
| `LocalStore.swift` | 版本化 JSON 原子存储，仅依赖 Foundation |
| `ContentView.swift` | `CloudAccountView`：既有云端登录 / 笔记 / 提问 / 设置 |
| `AppModel.swift`、`APIClient.swift` | 既有云端会话与接口，行为未改 |

## 能力边界

| 能力 | 状态 |
| --- | --- |
| 本机对话、任务、记忆（含编辑/完成/删除/忘记） | 可用，仅本机，未同步 |
| 逐字查找 | 可用，不是语义检索 |
| AI 回复（本机） | **无** |
| 语音听写 | **本版本不含** |
| 本机内容上传 / 同步 | **无** |
| 云端登录、云端笔记、提问及重试 | 可用（可选，需邀请码） |
| 云端语音、提醒、闹钟 | 未接入（仅文字说明，无假按钮） |

云端只连接 `POST /v1/auth/login`、`POST /v1/auth/logout`、`GET /v1/notes?limit=50`、`POST /v1/notes`、`GET /v1/notes/{id}`、`POST /v1/ask`，API base 固定 `https://agent.pinhaoyun.com/`，系统默认 ATS。冷启动不发请求，打开云端 Sheet 才读取笔记。

## 本机存储

`Application Support/LocalAssistant/local-store.json`，`version: 1`，原子写入并请求 `completeFileProtectionUntilFirstUserAuthentication`。

- 只有读取错误明确是「文件不存在」才视为首次使用；权限、文件保护、父目录不可访问及其他读取错误，以及损坏或来自更新版本的文件，都进入只读，**绝不覆盖或删除**，须用户点「重新读取」成功后才可再写。
- 写失败回滚并提示；草稿防抖保存，离开前台立即保存。
- DEBUG 启动参数 `-PAFailLocalWrites` 可模拟写失败。

**已知 P2（延后）**：云端 access/refresh token 仍以 Codable 数据存于 UserDefaults（沿用最小壳方案），未迁移 Keychain。

## 检查与自测

在完整仓库中运行（文本级检查，不是 Swift 编译）：

```sh
python3 scripts/check-ios-static.py
python3 -m unittest scripts/test_check_ios_static.py
python3 scripts/check-invariants.py
```

存储自测（需 Swift 工具链，**以非 root 普通用户运行**；P1-01 回归依赖真实的目录权限拒绝，root/ACL 下报 `UNSUPPORTED` 并失败）：

```sh
swiftc ios/PersonalAgent/LocalStore.swift ios/Tests/LocalStoreSelfTest/main.swift -o /tmp/lsst && /tmp/lsst
```

## Mac 构建

需要 Xcode 15+、iOS 17+ SDK 与模拟器运行时。

```sh
cd ios
xcodebuild -project PersonalAgent.xcodeproj -scheme PersonalAgent -sdk iphonesimulator -configuration Debug -derivedDataPath /tmp/pa-sim -destination 'generic/platform=iOS Simulator' CODE_SIGNING_ALLOWED=NO build
```

## 真机 archive / export

在 Xcode Settings → Accounts 登录有 `3KWH9L7X2T` 团队权限的 Apple ID；自动签名 `Apple Development`，Deployment Target 17.0。产物放 `/tmp`，不提交：

```sh
xcodebuild -project ios/PersonalAgent.xcodeproj -scheme PersonalAgent -configuration Release -destination 'generic/platform=iOS' -archivePath /tmp/PersonalAgent.xcarchive -allowProvisioningUpdates archive
xcodebuild -exportArchive -archivePath /tmp/PersonalAgent.xcarchive -exportPath /tmp/pa-development -exportOptionsPlist ios/ExportOptions-development.plist -allowProvisioningUpdates
```

## 手工验收

见 [`public-a-validation.md`](../docs/ios-redesign/public-a-validation.md) 的待办清单。Preview 不替代模拟器与真机验收。
