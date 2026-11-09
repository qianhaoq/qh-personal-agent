# 最终实现验证与交接（协调者记录）

## 结果边界

当前 Android 统一个人流已经由真实 box Codex 实现；Kimi 使用显式 `--model k3.1-pro` 做了实际设计、修订、代码级 UI 反向评审与复审。旧的 07/09/10/11 是各轮历史快照，保留原结论；最终独立结论以 12 为准：source P0/P1=0，构建/测试 P1=0，三组 P2 与设备/视觉缺口保留。本文件不是代写 Kimi 的批准意见。

没有看到设备或界面截图；不声称视觉、TalkBack 或真机验收。没有合并、部署、付费、平台迁移、routine 或外发通知。本轮只发布供用户检查的 Draft PR stack，禁止自动合入。

## 实际功能

统一个人流、显式记下/提问模式和语音/文字输入，来源引用、手动答案重播、详情编辑删除、搜索、完整提醒状态及确认/完成/取消/贪睡、通知与 boot 入口保留。网络串行，忙时可编辑下一草稿，不自动排队；仅持久化草稿与创建恢复，不新增服务器 agent 协议、持久聊天或业务离线缓存。

关键约束：session mutex 内先复核 metadata owner/epoch 再读凭证；同步持久关闭先于登出清理；不可变请求 A 与编辑稿 B 分离，客户端/服务端 ID 不同，未知结果先对账、明确原样重试、404/tombstone 与 409 不吞 B，live A 下可用新 ID 另存 B；服务端确认与真实 exact/inexact/unknown/failed/expired 本机提交分轴，失败不重复 confirm。

## 真实完整构建（不是 compile stub）

在真实 JDK17/Android SDK、项目 Gradle8.7 wrapper 上执行：

```sh
JAVA_HOME=/home/box/.local/jdk/current \
ANDROID_HOME=/home/box/.local/android-sdk \
GRADLE_USER_HOME=/home/box/.gradle \
./android/gradlew -p android :app:assembleDebug :app:build :data:build \
:core-security:build testDebugUnitTest --continue --no-daemon --console=plain
```

三层都独立完整执行并 `BUILD SUCCESSFUL`，真实 Room kapt、APK打包、lint及单测包含在内：

| 快照 | app debug | app release | data | failures/errors/skipped |
|---|---:|---:|---:|---|
| stack01 会话/排程基础 | 90 | 90 | 52 | 全部0 |
| stack02 恢复/共享UI基础（含日期对话框修复） | 94 | 94 | 60 | 全部0 |
| 完整统一体验 | 111 | 111 | 60 | 全部0 |

精确数字来自真实 JUnit XML，而非辅助 runner。完整日志及 XML 汇总位于 `/workspace/qh-unified-evidence/implementation/`：`stack-01-full-gradle.log`、`stack-02-verified-full-gradle.log`、`final-full-gradle-green-candidate.log`、`verified-gradle-test-summary.json`。最后完整构建后生产源码 SHA256 与 `final-production-source-hashes.json` 一致。

`python3 scripts/check-invariants.py` 也通过（0 errors/0 warnings）。生产/测试 diff 空白检查通过；Kimi 原始 Markdown 文档的双空格硬换行按原样保留，不冒充整份新增文档都无该格式。

## 真实修复与测试纪律

此前发现的测试红灯均保留日志，没有作为通过：修复 owner 默认参数导致的既有输入校验回归，原两条断言未改；纠正真实测试 API/跨模块编译问题；response-loss 模拟改为路径分派和按 client ID 幂等的真实服务器状态，增强 A/B/C 身份与请求断言，区分物理重传与逻辑重复。

Robolectric 的 390dp 窗口与原本实际 320dp 内容不一致触发公开 issue8460，harness 已让 rule 管理真实 recomposer/生命周期，并统一 320dp 窗口与内容、增加实测尺寸断言；未降低48dp阈值或跳过用例。这不证明390dp或真实手机矩阵通过。

真实 host UI 测试随后捕获 DatePicker action 高度0dp：实际 Material 容器被不带 weight 的正文耗尽。Codex修复滚动正文与动作空间分配；真实确认/取消/滚动/选择时间/外层确认在 320dp、fontScale2、含640dp短屏执行通过。原旧390dp卡住运行被定点取消 exit143，有 thread dump，不算通过。

## PR拆分与当前分支

基线 main `3600c3ab4a1c499f099e39be6b8084b3351dd7b7`，远端再次只读核实一致。原工作树与 S39 工作树不变。

| 层 | 范围 | 生产 added+removed（不含docs/tests） | 计划head/base |
|---|---|---:|---|
| 1 | owner/epoch、凭证前栅栏、真实排程结果 | 441 | feat/voice-agent-unified-foundations → main |
| 2 | 草稿/恢复、共享可达UI、真实日期确认对话框 | 563 | feat/voice-agent-unified-recovery → 上层 |
| 3 | 完整个人流、语音文字入口、model/audio/projection整合 | 799 | feat/voice-agent-unified-interaction → 上层 |

三层不是把巨型PR拆commit冒充限额；每个PR有独立base和真实完整构建。前两层保留旧可用界面，最后一层形成整体端到端体验。禁止把最终分支直接作为1803行生产diff的单PR发到main；按stack顺序由用户后续检查与合入。

## 未覆盖/未决

- 三组P2：输入坞在流末尾需滚动；部分已提交/过期提醒仍提供无效的本机重试；rejected/deleted移除恢复项确认可更精准。保留独立评审说明，未冒充已修。
- 390dp/真实手机完整字体矩阵、视觉截图、键盘焦点回还/TalkBack/对比度/减少动画未验收。
- 真实 ASR/TTS、麦克风拒绝、通知权限/频道、exact撤回、实际系统提醒投递、boot与旧版升级需真机QA；host测试不替代。
- GitHub五门禁、专业QA标签与平台reviews是发布后的独立流程；本地测试与Kimi交叉review不冒充远端门禁或qa-passed。Draft PR本轮仅供检查，不自动合并。

## 可续做证据

`/workspace/qh-unified-evidence/implementation/` 保存实际 Codex各轮日志、Kimi code-ui-review/rereview/final-review stream JSONL、模型元数据、测试汇总及生产hash。根 `/workspace/qh-unified-evidence/` 保留最初Kimi设计、Codex设计评审与一手来源快照。无凭证被读取或输出。
