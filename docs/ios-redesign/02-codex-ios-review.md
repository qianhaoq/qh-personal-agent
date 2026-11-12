# Codex 独立 iOS Review

评审基线：`756b9c23d98016589cd5fe761b9859c9b0296c6c`，对照 `git show 756b9c2`；范围为 `ios/PersonalAgent/**`、设计文档与 `ios/README.md`。工程配置及服务端返回契约仅作只读核对。

## 结论（可否进入模拟器验收）

**原 commit 不宜直接进入功能验收：发现 1 个 P1，0 个 P0。** 含待办笔记会使笔记解码失败，阻断真实个人流。最小修复见 `03-codex-ios-review-fixes.md`；修复后可以进入 Mac 构建及模拟器验收，不代表已经编译或验收通过。

当前 Linux 环境无 `swift`、`xcodebuild` 或 iOS SDK，未执行 Swift 类型检查、模拟器运行、真机签名或现网请求。未发现其他明确的语法/API、入口或 plist 阻断；动态布局、键盘及辅助功能仍需实际验收。

## P0 列表

无。

## P1 列表

1. **待办返回类型错误使整批笔记读取失败。** `ios/PersonalAgent/APIClient.swift`（原提交第 19 行，`todos: [String]`），`ios/PersonalAgent/ContentView.swift`（原第 396–397 行），`ios/PersonalAgent/AppModel.swift`（Preview 样例）。服务端 `server/app/llm/base.py`、`server/app/workers/extract_impl.py::_coerce_extraction` 明确定义并存储 `[{"text":"…","due":"…"}]`，`server/app/schemas.py::note_to_dict` 原样返回数组。一旦最近 50 条中有非空待办，`JSONDecoder` 无法把对象解成 String，整个列表读取失败；创建后 GET 同样受影响。Preview 使用字符串数组掩盖了契约差异。应按对象解码并绑定其 `text`，允许忽略本版不展示的 `due`。已最小修复，未把错误降级为空待办来掩盖数据。

## P2

1. **长期凭证仍以 UserDefaults 明文结构存储。** `ios/PersonalAgent/AppModel.swift:61`、`:67`、`:98`；`ios/README.md`「本机存储与当前限制」。access/refresh token 均未使用 Keychain，缺少凭证专用存储保护。这是前一版已存在且 README 明示的验证壳限制，不认定为此次新增的模拟器验收阻断；正式发布前应迁移 Keychain 并清除旧 defaults 项。本次未扩大存储改造范围。
2. **长笔记与来源摘录无法完整阅读。** `ios/PersonalAgent/ContentView.swift:388`、`:443`；`docs/ios-redesign/01-kimi-ios-design.md` 第 2、7 节明确不提供详情入口。正文限制五行、引用限制四行，且没有展开操作，用户无法阅读被截断内容。建议后续卡内展开，保持单一个人流；本次保留设计。
3. **取回被拒绝的记事会覆盖正在写的新稿。** `ios/PersonalAgent/AppModel.swift:159`（`takeBackPendingNote`）；`ios/PersonalAgent/ContentView.swift` 的输入坞。复现：提交被 422 拒绝的原文，继续输入新稿，再点「取回编辑」；`draft = pending.text` 无条件覆盖新稿且无法撤销。与设计第 3.2 节允许在途编辑下一稿的预期有恢复衔接缺口。建议新稿非空时先保留或确认替换；不是本轮核心流程的普遍阻断，留待后续处理。

## 非问题

- `ios/PersonalAgent/ContentView.swift` 的语音按钮实际 `.disabled(true)`，有未接入说明；提醒/闹钟仅在能力列表标「未接入」，无排程或权限成功假象。`ios/PersonalAgent/Info.plist` 无麦克风权限与 ATS 放宽配置。
- `ios/PersonalAgent/AppModel.swift` 的示例笔记、回答和引用仅由 DEBUG Preview 工厂生成，正常初始化不调用；`ContentView.swift` 对示例显式标记。没有发现捏造数据进入真实个人流。
- `ios/PersonalAgent/APIClient.swift` 无硬编码密钥或邀请码；固定 HTTPS 域名属于 API 配置。`AppModel.swift` 邀请码不持久化，未发现 token/正文日志；Apple team ID 和 bundle ID 不是认证密钥。
- `ios/PersonalAgent/AppModel.swift` 的保存未知状态保留同一 `client_note_id`，拿到 `note_id` 后重试仅 GET；错误不伪装成功。空态以成功读取为前提，问答失败保留问题，忙碌状态禁止重复发送。
- `ios/PersonalAgent/AppModel.swift` 明确 401 清会话，登出网络失败仍提示服务端未成功；异步内容写回检查会话代次。自动 refresh 未实现与设计及 README 一致，本次不作为缺陷。
- `ios/PersonalAgent/PersonalAgentApp.swift` 有唯一 `@main`；工程包含全部四个 Swift 文件。iOS 17 / Xcode 15 基线与使用的 NavigationStack、纵向 TextField、Preview 等一致。`Info.plist` 为有效 XML，启动项齐备；静态核对不能替代 Xcode 构建。
- `ios/PersonalAgent/ContentView.swift` 的登录/单一个人流/设置 Sheet、浅色纸面配色及普通 UI 移除 API/SHA 与 `docs/ios-redesign/01-kimi-ios-design.md` 一致，未发现需要重写信息架构的 P0/P1。

验证：`python3 scripts/check-invariants.py` → `0 error(s), 0 warning(s)`；`git diff --check` 通过；Python `plistlib` 校验 Info.plist 并核对工程文件引用、单一入口及权限边界通过。未将静态核对描述为 Swift 编译测试。
