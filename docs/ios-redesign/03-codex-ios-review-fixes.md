# Codex iOS Review 最小修复

基于 `756b9c23d98016589cd5fe761b9859c9b0296c6c`，修复 `02-codex-ios-review.md` 的唯一 P1：待办字段与真实 API 契约不一致。

## 改动

- `ios/PersonalAgent/APIClient.swift`：新增 `NoteTodo: Decodable`，将 `Note.todos` 改为 `[NoteTodo]`，读取服务端对象中的 `text`。未展示的 `due` 由 Decodable 忽略，不引入提醒能力。
- `ios/PersonalAgent/ContentView.swift`：待办行读取 `text`；对卡内前两条以数组位置标识，避免重复文字作为 SwiftUI 身份。布局与显示数量保持原设计。
- `ios/PersonalAgent/AppModel.swift`：Preview 使用相同待办对象模型，避免示例契约继续掩盖真实返回类型。

按类核对：检查 Note 列表解码、创建后单条 GET 解码、卡片绑定及 Preview 的所有 `todos` 使用；并只读核对 `tags` 字符串数组、`citations` 对象数组和创建响应 `note_id` 的服务端契约，未发现同类类型错误。没有修改 android/server、工程配置或设计意图。

## 验证与限制

- `python3 scripts/check-invariants.py`：`check-invariants: 0 error(s), 0 warning(s)`。
- `git diff --check`：通过，无空白错误。
- Python `plistlib` 与工程静态核对：Info.plist XML 有效、未放宽 ATS/添加麦克风权限、四个 Swift 文件均有工程引用、仅一个 `@main`，通过。
- 服务端既有实现交叉核验：`server/app/workers/extract_impl.py::_coerce_extraction` 生成含 `text` 和可选 `due` 的对象，`server/app/schemas.py::note_to_dict` 返回该数组；修复模型与之对应。未修改测试断言或服务端以迁就客户端。
- 当前无 Swift/Xcode，**没有执行编译或 Swift JSONDecoder 运行测试**。Mac 上须执行 `ios/README.md` 的 Debug 模拟器构建，并验证：空 `todos`、非空 `[{"text":"带相机"}]`、含可选 `due`、重复待办文字、多条笔记混合返回；列表和创建后的 GET 均应成功，待办仍只展示前两条文字。

修复后可进入上述构建与模拟器验收；P2 保留在 review 中，未宣称正式发布安全与 UX 验收通过。
