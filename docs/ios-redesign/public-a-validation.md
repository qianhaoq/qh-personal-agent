# 验证证据与待办（公开整理）

## 1. 来源说明

本改版最初在私有分支上开发。为公开发布，同一份源码被重新打包成一个新的公开提交（相对基线 `93729ff` 只有一个提交）。下文的证据针对的是**原始源码提交 `d57fdda`**，只作为源码来源证明：

- 公开提交中的 `ios/PersonalAgent/`、`ios/PersonalAgent.xcodeproj/`、`ios/Tests/`、`scripts/check-ios-static.py`、`scripts/test_check_ios_static.py` 与 `d57fdda` 逐字节相同（git tree / blob 哈希一致）；只有 `ios/README.md` 与 `docs/` 不同。
- **源码相同不等于新提交已验证。新提交的 Mac 构建与存储自测仍待完成，尚未通过 QA。**

## 2. 原始源码提交 `d57fdda` 的证据

### 2.1 独立源码复审

P1-01 在源码复审层面清零，详见 [`public-a-review.md`](public-a-review.md)。

### 2.2 存储自测（Mac，非 root 普通用户）

```sh
swiftc ios/PersonalAgent/LocalStore.swift ios/Tests/LocalStoreSelfTest/main.swift -o /tmp/lsst
/tmp/lsst
```

日志全文（来自 owner 提供、协调方核对过的日志）：

```text
COMPILE_EXIT=0
ok   missing file loads empty and writable
ok   loading does not create a file
ok   text is trimmed
ok   round trip preserves items, done state and draft
ok   failed write throws writeFailed
ok   failed write leaves in-memory document unchanged
ok   failed write leaves file unchanged
ok   corrupt file enters read-only
ok   corrupt store refuses writes
ok   corrupt file bytes are never overwritten
ok   future version enters read-only
ok   future store refuses writes
ok   future-version bytes are never overwritten
ok   unreadable existing path enters read-only
ok   whitespace-only text rejected
ok   over-limit text rejected
ok   memories cannot be completed
ok   editing a missing item fails
ok   saving the same message twice as task fails
ok   deleting a message keeps derived memory
ok   search is case-insensitive
ok   search trims query
ok   blank query matches nothing
ok   search is literal, not semantic
ok   untraversable parent enters read-only, not first launch
ok   read-only store exposes no fabricated data
ok   draft write before explicit reread is refused
ok   original bytes unchanged after refused write
ok   explicit reread after access restored clears read-only
ok   explicit reread recovers the old items and draft
ok   writes after reread keep old items
ALL PASSED
SELFTEST_EXIT=0
```

最后 7 项即 P1-01 回归：父目录失去访问权限 → 只读、拒写且原字节不变 → 显式重读恢复。

### 2.3 Mac Debug 模拟器构建

```sh
cd ios
xcodebuild -project PersonalAgent.xcodeproj -scheme PersonalAgent -sdk iphonesimulator -configuration Debug -derivedDataPath <DERIVED_DATA> -destination "generic/platform=iOS Simulator" CODE_SIGNING_ALLOWED=NO build
```

结果：日志以 `** BUILD SUCCEEDED **` 结束；唯一警告是 AppIntents 元数据提取跳过（工程没有 AppIntents 依赖）。

### 2.4 安装与启动

只有 owner 的文字说明和一张截图，报告应用已在模拟器上安装并启动；**没有单独的安装命令日志**，因此安装命令未经独立核验。

### 2.5 原始产物 SHA-256（来源证明）

| 产物 | SHA-256 |
| --- | --- |
| 存储自测日志 | `e6926635203f91daed49b9241f49ddda077b6fc62a38b066a544653bad611d60` |
| 模拟器构建日志 | `f2f5932f5fce397fa638ccbf5e008d12224774868542ded47877de486425e5b1` |
| 首页截图 | `8ac9feaf7dc3fc7cc90fb011d11853d44a44abeb1b656cbae867027f18e5f96a` |
| 模拟器 .app 压缩包（未发布） | `e6b02824f500252bc0bc56c746a78abb44595da623cd55fca0f4ca78f9f8d75f` |

原始文件未随仓库发布。

## 3. 本公开提交仍待完成

- [ ] Mac 上对**本公开提交**重新运行存储自测（非 root），期望 `ALL PASSED`。
- [ ] Mac 上对本公开提交重新做 Debug 模拟器构建。
- [ ] QA 验收（未通过，未打 `qa-passed`）。

## 4. 尚未验证（任何提交都没有证据）

- `LocalModel` 运行时行为：0.6 秒防抖、离开前台立即保存、只读期间输入后重读的草稿合并。
- 真机上的 iOS 文件保护（首次解锁前读取失败与恢复）。
- 云端双账户隔离：新装首启无网络请求、登录不上传本机内容、账户切换、登出不影响本机库（应使用测试账户）。
- UI 与可访问性：深浅色、最大字号、VoiceOver、横屏/iPad、键盘遮挡、点击区域。
- `-PAFailLocalWrites` 下各操作的失败提示；损坏与未来版本文件的字节保持（App 层）。
