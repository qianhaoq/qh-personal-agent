# 一手依据与产品边界（协调者调研，不冒充 Kimi 设计）

基线：main `3600c3ab4a1c499f099e39be6b8084b3351dd7b7`。当前阶段只研究、设计、交叉评审；不部署、不恢复 routine、不换平台。

## 实际读到的一手来源

1. DeepSeek 微信官方技术长文：《Super-agent 设计探索：迈向更自主的 Agent 系统》
   https://mp.weixin.qq.com/s/zoS7GlReWPhfCjENGxXCdg
   已获取并阅读实际全文（不是仅二手摘要）。开头明确：面向工程实践的架构思路探讨，非 Harness 现有产品功能介绍。结尾署名 Yujia Qiao 实现与写作、团队审阅。本文不是本次已核实的同行评审学术论文；未发现 DOI/arXiv 论文链接，不编造论文标题或正式论文身份。
2. 官方组织原型 README
   https://github.com/deepseek-ai/dsh-superagent-prototype
   https://raw.githubusercontent.com/deepseek-ai/dsh-superagent-prototype/main/README.md
   明确声明仅研究参考、不是 DeepSeek 正式产品；公开代码和内部实现/文章不完全相同，一些设想尚未实现。原型面向单 GitHub 仓库，可选 Feishu，是 coding agent，不能照搬成本产品。
3. 官方原型哲学
   https://github.com/deepseek-ai/dsh-superagent-prototype/blob/main/docs/000-philosophy.md
   原句：Take as little human attention as possible, and give as much value as possible。只在被请求或书面规则授权时行动；仅在用户需要决定/能使用结果时说话；少概念、少配置；面板突出待人工事项；如实报告未完成和失败。
4. 官方领域、授权、执行文档（实际已读）
   https://github.com/deepseek-ai/dsh-superagent-prototype/blob/main/docs/001-domain.md
   https://github.com/deepseek-ai/dsh-superagent-prototype/blob/main/docs/002-authorization.md
   https://github.com/deepseek-ai/dsh-superagent-prototype/blob/main/docs/003-execution.md
   Ticket 和 Assignment 独立；结构化 attention，不能靠自由文本猜测阻塞；输入上下文不是授权；observe 默认只观察；持久状态与执行解耦，重启先核对再执行，不重复外部副作用。
5. Harness 官网：https://www.deepseek.com/en/harness/ （仅背景，不把官网展示功能等同于本项目已有能力）。

## 从官方长文可落实的原则（不是本项目已实现能力的承诺）

- 连续：用户面对一个个人 agent，不管理底层 Sessions；记事、记忆问答和提醒结果沿来源回到统一交互。任务/对象保持稳定身份，重新打开可追溯。
- Router/Worker 分工：前台沟通与后台工作分离；UI 不展示 token/工具流水、Worker/Thread/Mandate 等开发术语，也不要求手机后台永驻。
- 安静的异步：后台整理可继续，前台可接新输入；只展示真实可观测状态，不假造进度百分比或未接入的任务查询/停止能力。长文的完整异步与多设备设计是方向，不能声称当前客户端已具备。
- 任务不是一次会话：重试要绑定原对象和修订，失败不能冒充成功，返回前台从已保存数据恢复。当前 note 上传/整理、memory_sync、问答同步 HTTP 的边界必须由真实代码确认。
- 记忆有来源：摘要先呈现，引用可展开/回到原记事。长文也明确长期记忆尚是扩展瓶颈，不声称 DeepSeek 已解决所有长期记忆问题。
- 确认是授权边界：提醒建议不等于已设系统闹钟；先呈现内容/时间，明确确认后沿既有安全回调执行；网络完成/本地闹钟权限要分开表示。上下文不扩权。
- 中断与恢复诚实：停止听写、停止播报、取消请求、撤销服务器任务是不同概念，不能把一个按钮伪装成全局停止。
- 优先用户注意力：少打断、只问一个具体问题，待确认事项突出；其余结果可安静留在流中，不增加常驻盯控 routine。

## 不能泛化的实验结论

长文第7节使用合成消息流和简化环境（约300消息/20请求、耗时工具替代真实编码、各方案5次），并说明只作初步参考。不能把其中12.4/15.7分钟等数值当作本产品真实能力、用户收益或模型质量基准。

## 本项目事实核查入口

- AGENTS.md；docs/architecture-superagent.md；docs/superagent-concept-mapping.md（部分历史“未做”描述已过时，必须以 main 代码确认）。
- Android Kotlin/Compose + FastAPI；已有记事/提醒/记忆问答、SpeechRecognizer、TTS、AlarmManager 和确认动作。保留已合 S30-S39 行为。
- 用户买 iPhone 不等于要求 iOS 迁移；本轮仍为当前 Android 工程，不新建 iOS 仓。
- 不照抄 Grok Bot/Cue UI：只参考低概念负担与连续交互，设计与视觉由真实 Kimi 调用产出。
