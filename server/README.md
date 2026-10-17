# personal-agent server（S1 服务端骨架）

语音随手记个人助理的服务端：多入口语音记录的上传与离线同步、自动整理流水线、建议提醒、记忆搜索与问答。

规格来源：`../tech-arch-v1.md` 第 3–8 节（S1）。

## 架构

```
Caddy (TLS) ──> FastAPI api ──> PostgreSQL 16 + pgvector
                    │                │
                    │                └── jobs 表（任务队列）
                    └── LLMClient ──> 整理 worker（python -m app.jobs.worker）
                                          │
                                          ├── LLM（OpenAI 兼容 / MockLLM）
                                          └── MemoryStore（mem0 / inmemory）
```

代码结构：

```
app/
  main.py        FastAPI 入口、统一错误格式、/healthz
  config.py      pydantic-settings 环境变量配置
  db.py          SQLAlchemy 2 async（PG asyncpg / SQLite aiosqlite）
  models.py      users/devices/invite_codes/notes/reminders/jobs
  auth/          邀请码 + 设备注册，JWT 签发校验，refresh 哈希与轮换
  api/           auth、notes、sync、search/ask 路由
  jobs/          数据库任务队列（PG: FOR UPDATE SKIP LOCKED；SQLite: 条件 UPDATE 抢占）
  pipeline/      自动整理（一次 LLM 结构化抽取）+ run_pending_jobs 测试入口
                 事务划分：短读快照 → 事务外 LLM → 短写（锁行复查、seq 末尾分配）
                 → 提交后写记忆（失败由 job 幂等补写），持锁期间不调外部服务
  llm/           OpenAI 兼容客户端 + MockLLM（确定性规则，含中文时间解析）
  memory/        MemoryStore 抽象；inmemory（测试）、mem0（生产，可选安装）
  cli.py         python -m app.cli create-invite
```

两条主链路：
1. **记录（异步）**：`POST /v1/notes` 202 立即返回 → worker 整理 → 客户端 `GET /v1/sync` 拉结果。
2. **问答（同步）**：`POST /v1/ask` → 检索记忆 → LLM 生成回答（无记忆则明确回答“我没有相关记录”，不编造）。

## 启动

### 本地开发（无 Docker，SQLite + MockLLM）

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env          # 默认即 mock + sqlite，可直接用
.venv/bin/python -m app.cli create-invite --uses 5   # 创建邀请码
.venv/bin/uvicorn app.main:app --reload              # api
.venv/bin/python -m app.jobs.worker                  # 整理 worker（另开一个终端）
```

### 生产（Docker Compose：caddy + api + worker + postgres）

```bash
cp .env.example .env
# 编辑 .env：JWT_SECRET、POSTGRES_PASSWORD、DOMAIN、LLM_*（真实密钥不要提交）
docker compose up -d --build
docker compose exec api python -m app.cli create-invite
```

- PostgreSQL 用 `pgvector/pgvector:pg16` 镜像，数据卷持久化，healthcheck 就绪后再起 api/worker。
- 公网只开放 80/443，由 Caddy 自动签 TLS；域名对外服务前需完成 ICP 备案。

## 配置

见 `.env.example`（无真实密钥）。要点：

| 变量 | 说明 |
|---|---|
| `DATABASE_URL` | 生产 `postgresql+asyncpg://...`；本地/测试 `sqlite+aiosqlite:///...` |
| `JWT_SECRET` | HS256 签名密钥：>=32 字节随机值的编码。生成任选其一：`python -c "import secrets;print(secrets.token_urlsafe(48))"`、`openssl rand -hex 32`。生产（postgresql）启动时校验：hex 需 >=64 字符，或 base64/base64url（允许无 padding）解码 >=32 字节且解码后 >=16 种字节取值；占位值与人类可读口令（含 secret/password 字样、无法解码的字符串）会被拒绝 |
| `LLM_PROVIDER` | `mock`（默认，确定性规则）或 `openai`（OpenAI 兼容接口） |
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | DeepSeek 示例 `https://api.deepseek.com`；腾讯混元 OpenAI 兼容地址示例见 `.env.example`。**地址与型号需按官网核实** |
| `MEMORY_BACKEND` | `inmemory`（测试）或 `mem0`（生产，`pip install -r requirements-mem0.txt`）。只接受这两个值，未知值启动即报错；配置 `mem0` 但未安装 mem0ai 时也直接报错，不静默退化 |

## 测试

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

覆盖：登录成功/邀请码错误/未带 token 401/refresh 轮换与复用吊销；笔记幂等、乐观锁 409、删除级联；sync 增量；流水线（MockLLM）产出标题/标签/建议提醒且仅调一次 LLM、失败 3 次转 failed、LLM 调用期间不持锁（PATCH/他人创建不被阻塞）、记忆写入失败重试只补写不重复；用户隔离（A 读不到 B，含记忆层）；search/ask（有/无记忆）；MockLLM 中文时间解析；jwt_secret 生产校验（随机编码放行、占位/人类可读口令拒绝）。

## 测试的取舍：SQLite 而不是 testcontainers

本机没有 Docker，因此测试跑在 `sqlite+aiosqlite` 上，没有起真实 PostgreSQL 容器。由此带来的**未覆盖差异**：

- **默认只跑 SQLite 分支，PG 分支需显式指定数据库**。设置 `TEST_DATABASE_URL=postgresql+asyncpg://...` 后同一套测试在真实 PG 上再跑一遍，覆盖 PG 专属路径（`app/jobs/queue.py` 的 `FOR UPDATE SKIP LOCKED` 领取/回收、`app/api/sync.py` 的 REPEATABLE READ 一致快照与序列行锁提交顺序，见 `tests/test_sync.py` 里仅 PG 运行的测试）。日常开发不装 PG 时，这些 PG 分支只有代码审查。
- **pgvector / mem0 后端未在测试中覆盖**。测试全部使用 `InMemoryStore`；`Mem0Store`（pgvector + OpenAI 兼容 embedding）只在类型结构上对齐，未连真实库验证。embedding 选型本身也是待决策项（见 tech-arch 第 13 节）。
- SQLite 方言存 datetime 不保留时区偏移（值统一为 UTC 墙钟），`app/timeutil.py` 做了归一，但 PG 下 timestamptz 的行为仍建议上线前冒烟一遍。

## 已知限制（S1 骨架范围内）

- **建表用 `create_all`**，后续换 Alembic 迁移（tech-arch 第 8 节的发布流程要求）。
- **没有限流**：tech-arch 第 6 节的 `/ask` 20 次/分钟、`/notes` 60 次/分钟、登录按 IP 限流尚未实现。
- **笔记原文未加密**：tech-arch 的 `text_enc`（AES-GCM 应用层加密）未做，当前明文存储。
- **refresh token 有效期未强制**：30 天 TTL 未落库校验（登出/轮换/复用吊销已生效；超过轮换窗口之外的旧 token 无法关联到设备，只能判无效，无法吊销）。
- **登录并发竞态**：同一新邀请码并发登录理论上可能创建两个账号（无锁）。MVP 单用户场景影响极小。
- **删除笔记后重放同一 `client_note_id`**：返回已软删笔记的 id 与状态（幂等语义优先），不会新建。
- **mem0 的 ADD/UPDATE/DELETE 判定**、检索打分（时间衰减、类型权重）未实现，S1 只做写入与检索。
- **spec 第 5 节的 reminders/memories/profile/me 等端点**未包含在本任务范围内，S1 骨架只实现列出的接口。
- **`json_schema` 响应格式**：OpenAI 兼容客户端发送 `response_format=json_schema`，个别供应商可能只支持 `json_object`，需要按所选供应商调整（`app/llm/openai_compat.py`）。

## 安全说明

- access token：JWT HS256，15 分钟，`sub`=user_id、`did`=device_id；设备被吊销后 access 也立即失效（每次请求校验设备状态）。
- refresh token：服务端只存 SHA-256 哈希；每次使用轮换；检测到旧 token 复用立即吊销设备。
- 所有业务查询强制带 `user_id`，有专门的用户隔离测试。
- 笔记内容拼进 prompt 时放在 `<note>` 分隔符内并声明“只是数据”，S1 无任何可执行工具。
- 统一错误格式 `{"error":{"code","message"}}`，未处理异常返回 500 且不泄露内部细节。
