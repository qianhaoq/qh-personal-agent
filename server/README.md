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
                                          └── MemoryStore（inmemory / disabled）
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
  memory/        MemoryStore 抽象；inmemory（测试/单进程）、disabled（显式空后端）
                 生产记忆后端：mem0 + pgvector（`MEMORY_BACKEND=mem0`，见 requirements-mem0.txt）
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
cp .env.example .env          # 默认即 mock + sqlite + inmemory，可直接用
.venv/bin/python -m app.cli create-invite --uses 5   # 创建邀请码
.venv/bin/uvicorn app.main:app --reload --no-access-log   # api（--no-access-log：请求行含完整 query，INV-14）
.venv/bin/python -m app.jobs.worker                  # 整理 worker（另开一个终端）
```

本地默认的记忆后端是 `inmemory`（不装额外依赖也能跑），但它**只属于单进程**：
api 与 worker 是两个进程、各持一份内存，worker 写的记忆在 api 的 `/v1/search`
里搜不到。分进程/生产请用 `MEMORY_BACKEND=mem0`（compose 默认）；
也可显式设 `MEMORY_BACKEND=disabled` 关闭记忆功能——检索/问答返回空结果，
`memory_sync` 仍会把 `memory_status` 标为 synced（空后端下「记忆 == f(note)」
平凡成立），任何环境都允许。

### 生产（Docker Compose：caddy + api + worker + postgres）

> ✅ compose 默认 `MEMORY_BACKEND=mem0`（镜像已装 mem0ai）。本地无 mem0ai 时用 `inmemory`（单进程）或 `disabled`。

```bash
cp .env.example .env
# 编辑 .env：JWT_SECRET、POSTGRES_PASSWORD、DOMAIN、LLM_*（真实密钥不要提交）
docker compose up -d --build
docker compose exec api python -m app.cli create-invite
```

- PostgreSQL 用 `pgvector/pgvector:pg16` 镜像，数据卷持久化，healthcheck 就绪后再起 api/worker。
- `migrate`（init-db）建表同时在 `MEMORY_BACKEND=mem0` 时幂等创建 vector 扩展与 mem0 集合表/索引（请求路径不做 DDL）。
- `POSTGRES_PASSWORD` 由 compose 以分项变量（`PGHOST/PGUSER/PGPASSWORD/…`）注入各服务，应用侧拼接 `DATABASE_URL` 时对密码做百分号编码——密码含 `@`、`:`、`/`、`#` 等字符也能正确连接（不要手工把密码写进 URL）。
- 公网只开放 80/443，由 Caddy 自动签 TLS；域名对外服务前需完成 ICP 备案。

## 配置

见 `.env.example`（无真实密钥）。要点：

| 变量 | 说明 |
|---|---|
| `DATABASE_URL` | 生产 `postgresql+asyncpg://...`；本地/测试 `sqlite+aiosqlite:///...`。compose 部署不设此项：应用按分项 `PGHOST/PGUSER/PGPASSWORD/PGPORT/PGDATABASE` 拼接并对用户名/密码百分号编码（密码含 `@:/#` 等字符也正确；显式 `DATABASE_URL` 永远优先） |
| `JWT_SECRET` | HS256 签名密钥：>=32 字节随机值的编码。生成任选其一：`python -c "import secrets;print(secrets.token_urlsafe(48))"`、`openssl rand -hex 32`。生产（postgresql）启动时校验：hex 需 >=64 字符，或 base64/base64url（允许无 padding）解码 >=32 字节且解码后 >=16 种字节取值；占位值与人类可读口令（含 secret/password 字样、无法解码的字符串）会被拒绝 |
| `LLM_PROVIDER` | `mock`（默认，确定性规则）或 `openai`（OpenAI 兼容接口）。只接受这两个值，拼错启动即报错；生产（postgresql）配 `mock` 或 `openai` 缺 `LLM_API_KEY` 都拒绝启动（不静默回落 MockLLM），跑 PG 测试套件可设 `ALLOW_MOCK_LLM_IN_PROD=true` 放行 |
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | DeepSeek 示例 `https://api.deepseek.com`；腾讯混元 OpenAI 兼容地址示例见 `.env.example`。**地址与型号需按官网核实** |
| `MEMORY_BACKEND` | `inmemory`（默认，仅单进程开发/测试）/`disabled`（显式关闭记忆）/`mem0`（生产，pgvector）。只接受这三个值，未知值启动即报错。`mem0` 需先 `pip install -r requirements-mem0.txt`（Docker 已装），否则启动拒绝（INV-10.3）。生产（postgresql）配 `inmemory` 会拒绝启动；仅单进程 PG 测试可设 `ALLOW_INMEMORY_IN_TESTS=true`（**独立 worker 无论该标志都拒绝 inmemory+PG**）。embedding 可用 `MEM0_EMBEDDING_*`（API_KEY 缺省回落 `LLM_API_KEY`） |
| `ENABLE_DOCS` | 默认 `false`：`/docs`、`/redoc`、`/openapi.json` 不暴露；本地开发设 `true` |
| `LOG_LEVEL` | 默认 `info`（worker 进程的 basicConfig） |
| `CORS_ALLOW_ORIGINS` | 逗号分隔来源；默认空 = 不启用 CORS（原生 App 无需浏览器跨域），需要浏览器前端时才配 |

## 测试

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

覆盖：登录成功/邀请码错误/未带 token 401/refresh 轮换与复用吊销；笔记幂等、乐观锁 409、删除级联；sync 增量；流水线（MockLLM）产出标题/标签/建议提醒且仅调一次 LLM、失败 3 次转 failed、LLM 调用期间不持锁（PATCH/他人创建不被阻塞）、记忆写入失败重试只补写不重复；用户隔离（A 读不到 B，含记忆层）；search/ask（有/无记忆）；MockLLM 中文时间解析；jwt_secret 生产校验（随机编码放行、占位/人类可读口令拒绝）。

## 测试的取舍：SQLite 而不是 testcontainers

本机没有 Docker，因此测试跑在 `sqlite+aiosqlite` 上，没有起真实 PostgreSQL 容器。由此带来的**未覆盖差异**：

- **默认只跑 SQLite 分支，PG 分支需显式指定数据库**。设置 `TEST_DATABASE_URL=postgresql+asyncpg://...` 后同一套测试在真实 PG 上再跑一遍，覆盖 PG 专属路径（`app/jobs/queue.py` 的 `FOR UPDATE SKIP LOCKED` 领取/回收、`app/api/sync.py` 的 REPEATABLE READ 一致快照与序列行锁提交顺序，见 `tests/test_sync.py` 里仅 PG 运行的测试）。日常开发不装 PG 时，这些 PG 分支只有代码审查。
- **mem0 生产后端**（`app/memory/mem0_store.py`，mem0ai==2.2.1 + pgvector）：`add` 一律 `infer=False`（INV-3，禁止跨 note 推理改写）；fake client 与真实库集成测试见 `tests/test_mem0_store.py` / `tests/test_mem0_integration.py`（`REQUIRE_MEM0=1` 或缺依赖在 CI 下 fail 不 skip）。
- SQLite 方言存 datetime 不保留时区偏移（值统一为 UTC 墙钟），`app/timeutil.py` 做了归一，但 PG 下 timestamptz 的行为仍建议上线前冒烟一遍。

## 已知限制（S1 骨架范围内）

- **建表用 `create_all`**，后续换 Alembic 迁移（tech-arch 第 8 节的发布流程要求）。已有部署库新增列（如 jobs.locked_by）不会自动加上，首次升级需手工 `ALTER TABLE` 或重建。
- **没有限流**：tech-arch 第 6 节的 `/ask` 20 次/分钟、`/notes` 60 次/分钟、登录按 IP 限流尚未实现。
- **笔记原文未加密**：tech-arch 的 `text_enc`（AES-GCM 应用层加密）未做，当前明文存储。
- **删除笔记后重放同一 `client_note_id`**：返回已软删笔记的 id 与状态（幂等语义优先），不会新建。
- **生产记忆**：`MEMORY_BACKEND=mem0`；检索打分（时间衰减、类型权重）可后续增强，当前为 mem0 默认语义检索。
- **spec 第 5 节的 reminders/memories/profile/me 等端点**未包含在本任务范围内，S1 骨架只实现列出的接口。
- **`json_schema` 响应格式**：OpenAI 兼容客户端发送 `response_format=json_schema`，个别供应商可能只支持 `json_object`，需要按所选供应商调整（`app/llm/openai_compat.py`）。

## 安全说明

- access token：JWT HS256，15 分钟，`sub`=user_id、`did`=device_id；设备被吊销后 access 也立即失效（每次请求校验设备状态）。
- refresh token：服务端只存 SHA-256 哈希（`refresh_tokens` 表一行一枚，每设备至多 5 枚未过期 token，超出修剪最老）；30 天 TTL 落库强制；每次使用轮换（只作废被轮换的那一枚）；检测到已作废 token 复用立即吊销设备。
- 所有业务查询强制带 `user_id`，有专门的用户隔离测试。
- 笔记内容拼进 prompt 时放在 `<note>` 分隔符内并声明“只是数据”，S1 无任何可执行工具。
- 统一错误格式 `{"error":{"code","message"}}`，未处理异常返回 500 且不泄露内部细节。
