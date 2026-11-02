# Super-agent 概念映射（qh-personal-agent）

> 状态：**落地第一刀**（分支 `feat/superagent-rewrite-s1`；领域骨架 `server/app/{domain,policy,sources,workers}` + notes API 钩子 + `server/tests/test_domain_engine.py` 已落地，见 §11 落地记录）  
> 日期：2026-11-02（Asia/Shanghai）  
> 产品定位：**钱浩个人 agent**（语音优先、Android 先），**不是**团队 GitHub coding bot  
> 双参考：  
> 1. [deepseek-ai/dsh-superagent-prototype](https://github.com/deepseek-ai/dsh-superagent-prototype)（MIT，研究原型）  
> 2. 微信文《Super-agent 设计探索：迈向更自主的 Agent 系统》https://mp.weixin.qq.com/s/zoS7GlReWPhfCjENGxXCdg（已可读）  
> 目标仓现状：`qianhaoq/qh-personal-agent` main（Android Kotlin S1 mock + FastAPI pipeline + mem0）

---

## 0. 阅读指引与成功标准对齐

**本文**：Super-agent 概念到个人 agent 的权威映射（全文以此为准）。  
**第一刀已交付**：文档（本文 + `docs/architecture-superagent.md`）+ 领域骨架代码与测试（对齐 AGENTS.md 单 PR 体量，业务代码 ≈500 行）。  
**明确不做**：整仓 fork dsh、把 GitHub/Feishu coding worker 搬进个人 agent、密钥入仓、删旧 pipeline。

复用原则：**借思想与边界，不借场景实现**。dsh 是「单仓 proactive coding agent」；qh 是「语音记事 / 提醒 / 记忆问答」。二者共享 Super-agent 的双层编排、事件驱动、Policy 门禁、异步执行与可观测性，**不共享** GitHub PR / Feishu 频道 / DSH Session 目录执行环境。

---

## 1. 参考源摘要

### 1.1 微信文：四个特征 + 五个支撑模块

| 特征 | 含义（对个人 agent 的读法） |
| --- | --- |
| 连续 | 用户感觉始终是同一个助手；跨入口（主页 PTT / 小组件 / 后续 IM）共享上下文，Session 是实现细节 |
| 高效 | 可并行处理多条语音任务；前台可继续说话，不等待上一条整理完成 |
| 主动 | 由环境事件驱动（闹钟到点、整理完成、记忆待确认），不全是「用户开 Session 再打字」 |
| 持久 | 云端常驻编排；端侧可离线；异常可自愈（Watchdog 方向） |

五个模块（文中标号）：

1. **双层架构**：Router（全局连续交互与分派）+ Worker（隔离并发执行）  
2. **事件驱动**：Trigger、两级过滤（程序 Fast Path / 语义 Slow Path）、订阅直通、Inline & Spill / 链式召回、投机执行  
3. **面向人的交互**：异步 ReAct（`start/stop` 不阻塞对话）、按信道带宽裁剪输出（IM 短、文档/卡片结构化）  
4. **计算 / 状态 / 环境分离**：Agent Loop 无状态计算 + 持久会话状态；重执行环境留在端侧 daemon（个人机 BYOM）——个人 agent 第一刀可简化为「云端编排 + Android 本地提醒/ASR」  
5. **Watchdog**：外部 Control Loop，盯基础设施与任务进展（原型未完整实现，属演进项）

文中明确：**个人助手与研发协作若都要「持续在线、并发响应环境事件」，底层会采用相近架构**。这正是本映射的合法性来源。

### 1.2 dsh-superagent-prototype：可复用核心

**定位（README）**：研究原型；DeepSeek Harness bundle；监听 GitHub（可选飞书）；允许的人请求后开 Ticket，在独立 Executor Session/目录干活，再回原对话（如 PR）；Panel 只展示需人处理的事。哲学：**尽可能少占人注意力，尽可能多给价值**。

**文档索引（应按序读）**：`docs/000-philosophy` → `001-domain` → `002-authorization` → `003-execution` → `004-prompts` → `005-panel` → `006-deployment` → `007-architecture` → `008-cookbook`。

**领域数据流（001）**：

```text
Source ──Trigger──► Thread
                      ├─(未订阅)──► Eval(gate+assessment) ──accept+Mandate──► Ticket
                      └─(已订阅)──► Subscription fan-out ──────────────────► Ticket
Ticket ──Assignment──► Executor(Worker Session)
Ticket ──Channel──► Source（回复原对话）
Policy 变更 ──notice──► 相关 Ticket（不立刻撤销 Mandate）
```

**核心概念一览**：

| 概念 | 作用 | 关键规则 |
| --- | --- | --- |
| Source | 事件流（kind + instance） | 维护读位点；过滤自己的消息；Trigger→Thread/Channel 翻译 |
| Trigger | 不可变事件 | 幂等/审计单元；可属多 Thread |
| Thread | 时间有序事件串 | 同一 Thread 上 Eval 串行；持有 Subscriptions |
| Eval | Gate（确定性）+ Assessment（模型） | 产出 Decision：accept / stop / ignore / unavailable |
| Mandate | 授权快照 | 不可变；含 Policy version、scope、strength |
| Ticket | 授权工作单元 | 仅 Eval 创建；自有状态，不跟 Executor Session 走 |
| Assignment | Ticket↔Executor 绑定 | pending→running→ended |
| Subscription | Ticket↔Thread 多对多 | 后续事件直通，不经 Eval；人不 stop 则 ended Ticket 可被消息 resume |
| Policy | 版本化规则 + 各 Source 准入 | accept mode：`off` / **`observe`**（默认可观察不接单）/ `auto` |
| Channel | Thread 写侧 | 确认、回复、结果、attention |
| Executor | 域外端口 | 一 Ticket 一 Session+目录（dsh 场景）；经 Assignment 回报 |
| Outbox / Effects / Passes | 可靠副作用与调度 | 事务内 enqueue，提交后执行；评估/启动/投递/中继/回复 |

**实现模块边界（007 / src）**：

- `src/core/`：Engine（唯一写 Triggers/Threads/Decisions/Tickets/Assignments）、model、ports、tickets、evals、assignments、intake、replies  
- `src/policy.ts`：gate / revalidate / accept mode（含 **observe**）  
- `src/sources/`：SourceKind 插件（github、feishu）+ registry  
- `src/executor/`：DshExecutor  
- `src/app/`：Runtime、Store(SQLite)、Effects、Outbox、Passes、Sessions、assessment、prompts、panel HTTP  
- `src/client/`：管理 Panel（需人决策优先）  
- `prompts/`：orchestrator / worker / admission / subscriptions …

**与文中 Router/Worker 的代码对应**：

| 文中说法 | 原型实现名 |
| --- | --- |
| Router 层 | Main Session / `orchestrator`（评估、分派、统一对外回复） |
| Worker 层 | Executor Session / `worker`（单 Ticket 执行，不直出信道） |
| observe | **不是独立模块**；是 Policy 的 accept mode：研判「本可接单」只记录 would-accept，不真正 accept |
| Watchdog | 文中方向；原型侧重 Effects 重试 + health/panel，**无独立 Watchdog Session** |

### 1.3 qh-personal-agent 当前 main（只读）

| 区域 | 现状 |
| --- | --- |
| 产品 | 语音随手记 + 自动整理 + 建议提醒 + 记忆问答；三阶段 S1→S2 工具→S3 常驻 |
| `server/` | FastAPI：`api/{auth,notes,sync,ask}`、`pipeline/` 一次 LLM 抽取、`jobs/` PG/SQLite 队列、`memory/{inmemory,disabled,mem0}`、`llm/{mock,openai_compat}` |
| `android/` | S1 脚手架：`:app` / `:data` / `:core-security`；**debug 默认 `USE_MOCK_API`**；录音按钮占位；无 Room outbox / ASR / 提醒调度实装 |
| `docs/` | research/product/design/tech-arch/ai-native/MERGE_GATES/server-invariants 等 |
| 门禁 | CI Build&Test、Secret Scan、Codex Review、QA Gate；密钥仅本地 `.env` |

两条现有主链路：  
1) `POST /v1/notes` 202 → worker 整理 → `GET /v1/sync`  
2) `POST /v1/ask` 同步检索+回答

---

## 2. 概念映射表（dsh / 文 → 个人 agent）

| dsh / Super-agent 概念 | 个人 agent 对应物 | 说明 |
| --- | --- | --- |
| Source（GitHub/Feishu） | **VoiceIngress**（Android PTT/小组件）、**HttpApiSource**（已转写文本 POST）、远期 **ReminderClockSource**、**DigestCronSource** | 每种入口实现 SourceKind：产出 Trigger、维护读位点/幂等键 |
| Trigger | `UtteranceReceived` / `NoteUploaded` / `AskRequested` / `ReminderDue` / `OrganizeFailed` / `UserConfirmedReminder` | 不可变；`client_note_id` 等作幂等 |
| Thread | **ConversationThread**（同一语音会话/同一条笔记修订链）或 **TopicThread**（「关于某提醒/某人物」的后续确认） | 个人场景 Thread 粒度更细：一条笔记的整理+用户确认可成一 Thread |
| Main Session / Router | **PersonalRouter**（常驻编排）：意图分流、是否开 Ticket、统一生成对用户的短回复/actions[] | 第一刀可用「规则快路径 + 轻量 LLM」，不必上完整 DSH Session |
| Eval gate | **Admission**：仅本人设备 JWT；速率限制；明确「对助手说话」vs 纯记事 | 个人单用户：allowed list ≈ 本 user_id；strength：explicit（按住问/「提醒我」）vs inferred（纯旁白记事可只整理不回话） |
| Eval assessment | **IntentAssessment**：记事 / 提醒 / 问答 / 混合 / 忽略 | observe 模式：主动行为只记「本会做什么」 |
| Mandate | **WorkMandate**：允许写笔记、建议提醒、检索记忆、（远期）调工具；绑定 Policy 版本 | 提醒真正落闹钟需用户确认 → Mandate 默认不含「静默改日历」 |
| Ticket | **WorkItem**：`organize_note` / `answer_ask` / `propose_reminder` / `daily_digest` | 替代「匿名 pipeline job」为可观测、可停止、可恢复的工作单元 |
| Assignment + Executor/Worker | **Worker 进程/协程**：整理 Worker、问答 Worker、提醒提案 Worker | 上下文隔离；不直推 UI，结果经 Router→Channel |
| Subscription + 直通 | 用户对「建议提醒」的确认/修改消息直通原 WorkItem；整理失败重试同 Thread | 避免每条确认都重新 Eval 开单 |
| Channel | **Android 同步通道**（`/v1/sync` 增量）、**本地通知**、TTS 播报文案、（远期）推送 | 输出匹配带宽：通知一行；时间线卡片结构化；TTS ≤短句 |
| Policy | **PersonalPolicy**：自动整理开/关、主动提醒强度、`observe|auto`、敏感类过滤、记忆保留天数 | 版本化；变更 notice 到进行中 WorkItem |
| Outbox / Effects | 服务端可靠队列（沿用 jobs 表思想）+ Android Room outbox（端侧上传） | 双 Outbox：端→云、云→端 |
| Panel（需人决策） | Android：**待确认提醒**、整理失败重试、记忆编辑；可选简易 Web **ops 页**（开发者自用） | 「健康状态不占屏」 |
| Inline & Spill / 链式召回 | 问答上下文：先注 ID+摘要，按需拉 note/memory；避免整库塞 prompt | 替换「一次塞满 mem0 命中」的粗糙做法 |
| 投机执行 | 收到疑似日程语句时后台先提案提醒草稿，用户确认才 schedule | 安静失败 |
| 存算分离 / 端 daemon | S1：云端 API+Worker；端侧 ASR/AlarmManager；S3 再考虑常驻与多设备 | 不照搬「云沙箱克隆开发机」 |
| Watchdog | 后续：定时检查卡住 jobs、重复失败、Worker 无进展 | 第一刀用现有 job 重试+指标即可 |
| accept mode `observe` | 默认：**只整理与建议，不自动建系统闹钟/不自动外发** | 与哲学「少打扰」一致 |

### 2.1 刻意不映射 / 拒绝照搬

| dsh 物 | 处理 |
| --- | --- |
| GitHub Source / PR Channel / review threads | **不进个人 agent 运行时** |
| Feishu Source | 远期可选 IM Source；非 MVP |
| DSH Harness Session 目录、coding tools、prepare git checkout | **废弃于本产品**；Worker 是领域任务执行器 |
| 团队 allowed list / bot 列表 | 收敛为单用户鉴权 + 设备吊销 |
| 管理 Panel 全套 React | 可选精简；主 UX 在 Android |

---

## 3. 建议的新目录 / 模块边界

保持 monorepo，**服务端按 Super-agent 边界重切**；Android 仍按 feature 演进。目标树（第一刀不必一次建齐）：

```text
qh-personal-agent/
  docs/
    superagent-concept-mapping.md    # 本文
    architecture-superagent.md       # 第一刀落地的架构说明
  server/
    app/
      main.py / config.py / db.py    # 保留入口与配置风格
      api/                           # 薄适配：HTTP → Source.receive / 查询
        auth.py notes.py sync.py ask.py
      domain/                        # ★ 第一刀已建：纯领域（对标 src/core）
        model.py                     # Trigger Thread Ticket(WorkItem) Mandate Decision …
        engine.py                    # 唯一写入口（内存实现）
        ports.py                     # Worker 协议
      policy/                        # ★ 第一刀已建
        personal_policy.py           # gate / observe|auto / 规则
      sources/                       # ★ 第一刀已建
        base.py                      # Source 基类 + Trigger 构造
        voice_http.py                # HttpApiSource / VoiceIngress + API 钩子入口
        registry.py
      workers/                       # ★ 第一刀已建（取代「匿名 pipeline 名」）
        organize.py                  # facade 调现有 pipeline（不删 pipeline）
        answer.py                    # 后续刀
        reminder_propose.py          # 后续刀
      runtime/                       # 后续刀：effects.py / outbox.py / passes.py / store_pg.py
      memory/                        # 保留接口；实现可换
      llm/                           # 保留
      auth/                          # 保留
  android/                           # 仍为入口 Source 的客户端
    app/ data/ core-security/
    feature-capture/ feature-asr/ …  # 按 tech-arch 逐步实装
  scripts/ .github/                  # 门禁保留
```

**边界规则**：

1. `api/*` 不得直接调 LLM 整理；只 `engine.receive` / 读模型。  
2. `workers/*` 不得直接推送通知；只 `report` → Engine → Channel/Outbox。  
3. `policy/*` 是唯一准入与主动强度来源。  
4. `memory/*` 是 Worker 的工具端口，不是编排中心。  
5. Android 不实现 Router；只做 Source（采集）+ Channel 消费者（sync/TTS/Alarm）。

---

## 4. 第一刀 PR 范围 vs 后续

### 4.1 第一刀（已落地，分支 `feat/superagent-rewrite-s1`）

**目标**：在不打断现有 S1 API 契约的前提下，引入 Super-agent 领域骨架与一条「记事→整理」旁路/并行路径，证明映射可跑。

**落地清单**：

1. ✅ 文档：本文、短文 `docs/architecture-superagent.md`（数据流 + 本地跑法）。  
2. ✅ `server/app/domain/`：Trigger/Thread/Mandate/Decision/WorkItem 最小模型 + `Engine.receive`（内存实现，无新表/迁移）。  
3. ✅ `sources/voice_http.py`：`POST /v1/notes` 写库成功后 `submit_note_uploaded` → `engine.receive(NoteUploaded)`（纯内存同步、异常不上抛）。  
4. ✅ Eval 快路径：规则「笔记上传 → accept organize_note Ticket」（strength=inferred）。  
5. ✅ Worker：`workers/organize.py` facade 调现有 `pipeline/extract`（行为一致；单测驱动验证）。**生产 notes 路径不因 Ticket 再 enqueue 一次 extract（不双跑）**。  
6. ✅ Policy：硬编码版本 `v0` + `accept_mode=observe`（Mandate 只授被动面：整理 + 建议提醒；不含静默建闹钟/外发）。  
7. ✅ 测试：`server/tests/test_domain_engine.py`（幂等、生命周期、facade、契约不变、旁路故障不影响 202）。  
8. ✅ README 增加「Super-agent 重写进行中」指针；**不删**旧 pipeline 直到旁路稳定。

**不包含（后续刀）**：

| 后续 | 内容 |
| --- | --- |
| PR-2 | `/v1/ask` 走 Ticket+answer Worker；Inline/Spill 检索 |
| PR-3 | Subscription：提醒确认直通；Channel 统一出 `actions[]` |
| PR-4 | Android：去 mock 默认路径文档化、Room outbox、真 ASR、AlarmManager 消费 Channel |
| PR-5 | Effects 通用化、Watchdog、digest cron Source |
| PR-6 | 废弃旧 pipeline 入口、收敛 mem0 或替换记忆后端、Panel/ops；WorkItem 落 PG（`work_items`/`triggers` 表） |
| 远期 | IM Source、工具 Registry/MCP、多设备 daemon |

**体量控制**：对齐 AGENTS.md，单 PR 业务代码 ≤ ~800 行（不含 docs/tests）；第一刀实际 ≈500 行。

---

## 5. 本地怎么跑（无密钥入仓）

与现仓一致，密钥只在本地 `.env`（模板见 `server/.env.example`；默认 `LLM_PROVIDER=mock`、`MEMORY_BACKEND=inmemory`、SQLite，零外部密钥可跑）。

### 5.1 服务端（SQLite + MockLLM）

```bash
cd server
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env
# 默认：LLM_PROVIDER=mock，MEMORY_BACKEND=inmemory，sqlite
.venv/bin/python -m app.cli create-invite --uses 5
.venv/bin/uvicorn app.main:app --reload --no-access-log
# 另一终端：
.venv/bin/python -m app.jobs.worker
.venv/bin/python -m pytest
```

第一刀新增的领域单测：

```bash
.venv/bin/python -m pytest tests/test_domain_engine.py
```

生产记忆 `MEMORY_BACKEND=mem0` 需 `requirements-mem0.txt` + Postgres；**第一刀本地可不启 mem0**。

### 5.2 Android（仍可 mock）

```bash
cd android
./gradlew --no-daemon assembleDebug testDebugUnitTest :data:test
# 接真机 API 时在被 gitignore 的 local.properties：
# api.baseUrl=http://10.0.2.2:8000/
# api.useMock=false
```

### 5.3 仓库根

```bash
python3 scripts/check-invariants.py
bash scripts/ci-test.sh
```

---

## 6. 明确保留 vs 废弃

### 6.1 保留（第一刀及过渡期）

| 项 | 理由 |
| --- | --- |
| FastAPI + 现有 `/v1/auth|notes|sync|ask` 契约 | Android S1 已依赖；重写应适配器化而非断崖 |
| JWT 邀请码设备模型、refresh 轮换 | 个人安全基线 |
| `llm/` Mock + OpenAI 兼容 | 无密钥可测 |
| `memory/` 抽象接口 | Worker 端口；实现可换 |
| `jobs/` 队列思想 | 可演进为 Effects |
| Android `:app`/`:data`/`:core-security` 模块划分 | 继续长 |
| `.github` 门禁、`server-invariants`、密钥不进仓 | 硬约束 |
| 产品三功能：记事 / 提醒 / 记忆问答 | 定位不变 |

### 6.2 废弃或降级（重写完成后）

| 项 | 处理 |
| --- | --- |
| 「pipeline 即架构中心」 | **废弃为编排中心**；逻辑迁入 `workers/organize`，由 Engine 调度 |
| 匿名 job 无 Ticket/Mandate | **废弃**；改为 WorkItem 可观测 |
| Android S1 **长期以 mock 为默认真路径** | **废弃作唯一验收路径**；mock 仅留作 UI 开发，CI 保留契约测试 |
| 录音按钮永久占位 | **废弃**；按 tech-arch 实装 capture/asr（可多 PR） |
| 把 mem0 当作「Agent 大脑/路由器」 | **废弃该用法**；mem0 最多作 MemoryStore 适配器之一 |
| 照搬 dsh GitHub/Feishu/DSH coding Executor | **永不引入运行时** |
| 单 Session 同步阻塞整理+问答 | **废弃**；Router 异步分派 |

### 6.3 过渡策略

- 旧 `pipeline/extract` 在第一刀被 Worker **调用**（facade），避免大爆炸改测试。  
- 待 Engine 路径与 invariants 全绿后，再删 facade、更新 `docs/server-invariants.md`。  
- mem0：保留 `MEMORY_BACKEND=mem0` 开关；若成本/复杂度过高，允许 `disabled` + 业务表检索作过渡，但需文档声明能力差。

---

## 7. 数据流对照（目标态）

```text
Android PTT → ASR 文本 → Room outbox → POST /v1/notes
                                    ↓
                         VoiceHttpSource.receive(Trigger)
                                    ↓
              Intake → 订阅直通? → 否 → Eval(gate+intent)
                                    ↓ accept + Mandate
                                 Ticket(organize_note)
                                    ↓ Assignment
                           Worker.organize（原 pipeline）
                                    ↓ report ready
                    Router/Outbox → sync 增量 / 建议提醒卡片
用户点「确认提醒」→ Trigger 直通原 Ticket → Worker 标 scheduled
                                            → Channel → 端上 AlarmManager
```

问答：

```text
POST /v1/ask → Trigger(AskRequested) → Ticket(answer_ask)
→ Worker：Inline 摘要 → search MemoryStore → LLM → citations
→ Channel：TTS 文案 + 时间线可选存问句
```

第一刀的实际落地形态（双路径并行）见 `docs/architecture-superagent.md`。

---

## 8. 与旧文档关系

| 旧文档 | 关系 |
| --- | --- |
| `design-v2` / `product-prototype-v2` | **产品范围仍有效**；本映射改「编排与运行时」 |
| `tech-arch-v1` S1 模块表 | Android 模块规划仍有效；服务端目录以本映射为准修订 |
| `ai-native-arch.md` | 工具/MCP/端侧 daemon 仍属 S2/S3；与文中存算分离对齐但延期 |
| `server-invariants.md` | 重写时逐条迁移到 domain/workers；不降门禁（第一刀未触碰其 INV 面：钩子在事务外、纯内存） |

---

## 9. 风险与简化声明

1. **不引入 DeepSeek Harness 运行时依赖**——第一刀用 Python 实现领域引擎即可。  
2. Router「单个常驻 LLM Session」在个人单用户下可先用**无状态函数 + 短上下文**，等 S3 再常驻。  
3. 文中 Watchdog / A2A / 云端攻破端侧 等安全议题：个人单租户先做鉴权隔离与最小工具面，**不在第一刀展开**。  
4. 原型许可 MIT，可阅读思想与结构；**不要整文件复制**进本仓而不标注；更推荐按映射重写。  
5. **第一刀的已知限制**（详见 architecture 文）：Engine 为进程内内存态——多 worker 进程间不可见、重启即空；生产执行仍走 jobs 队列，Ticket 状态与 job 状态暂不同步（`pending` 停留在内存）。

---

## 10. 文档路径

- 主文档：`docs/superagent-concept-mapping.md`（本文）  
- 架构说明：`docs/architecture-superagent.md`  
- README「设计文档」节含上述链接

---

## 11. 落地记录（第一刀实际改动）

| 路径 | 内容 |
| --- | --- |
| `docs/superagent-concept-mapping.md` | 本文 |
| `docs/architecture-superagent.md` | 架构短文：数据流 + 模块边界 + 本地跑法 |
| `server/app/domain/{__init__,model,engine,ports}.py` | 领域模型 + Engine（唯一写入口，内存实现）+ Worker 协议 |
| `server/app/policy/{__init__,personal_policy}.py` | v0 PersonalPolicy：`accept_mode=observe`、被动/主动动作面 |
| `server/app/sources/{__init__,base,voice_http,registry}.py` | HttpApiSource / VoiceIngress 占位 + `submit_note_uploaded`（API 钩子，异常不上抛） |
| `server/app/workers/{__init__,organize}.py` | organize_note Worker facade（调 `pipeline/extract`，不删 pipeline） |
| `server/app/api/notes.py` | create 成功路径（commit 后）加一行 `submit_note_uploaded`；`/v1` 契约与 enqueue("extract") 不变 |
| `server/tests/test_domain_engine.py` | 9 条测试：开单/幂等/Policy/钩子不双跑/旁路故障不影响 202/facade 成败两路 |
| `README.md` | 设计文档节加链接，注明 Super-agent 重写进行中 |

---

## 附录 A：调研方法与挡点

| 源 | 方法 | 结果 |
| --- | --- | --- |
| dsh README/docs/src 树 | `gh api` / `cursor-github get_file_contents` / `curl raw` | 完整可读；**未**整仓 clone 到长期目录 |
| 微信文 | WebFetch | **成功**全文；要点已纳入 §1.1 |
| qh-personal-agent | `gh api` recursive tree + raw README/design | main 结构已核对 |

## 附录 B：dsh 术语中英（摘自 001 glossary，便于对照）

Source 来源 · Trigger 触发 · Thread 线程 · Eval 评估 · Gate 准入 · Assessment 研判 · Decision 决定 · Mandate 授权 · Ticket 工单 · Assignment 派工 · Subscription 订阅 · Executor 执行器 · Policy 策略 · Channel 渠道 · observe（accept mode）观察接单。
