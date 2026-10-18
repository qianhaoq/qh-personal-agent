"""应用配置：全部来自环境变量（.env 亦可）。"""
import base64
import binascii
import importlib.util
import os
import string
from collections.abc import Mapping
from functools import lru_cache
from typing import Literal
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 明显的占位符，只允许配合 SQLite 开发/测试使用
DEFAULT_JWT_SECRET = "dev-insecure-secret-change-me"
# .env.example 里的占位值（测试会读取 .env.example 校验与此常量一致）
ENV_EXAMPLE_JWT_PLACEHOLDER = "change-me-to-a-long-random-secret"
# 明显的占位/人类可读口令字样，出现在生产密钥里即拒绝（大小写不敏感）。
# secret/password 用来挡人类可读口令（如 my-super-secret-...）：随机编码里
# 出现连续 6/8 个字母单词的概率约 1e-9（hex 编码里不可能出现）
_INSECURE_SUBSTRINGS = ("change-me", "changeme", "example", "placeholder", "secret", "password")
# 生产密钥的结构要求：>=32 字节随机值的编码（hex 或 base64/base64url）
_MIN_SECRET_BYTES = 32
_MIN_HEX_CHARS = 64  # 32 字节 = 64 个 hex 字符
# 解码后不同字节取值数的下限（防 "00"*32 这类低熵值只靠长度过关）
_MIN_SECRET_DISTINCT_BYTES = 16
_HEX_DIGITS = frozenset(string.hexdigits)
# base64url 的 -_ 换成标准 base64 的 +/ 后统一走严格的标准解码
_URLSAFE_TO_STD = str.maketrans("-_", "+/")

# 数值配置的有限上界（threads13 P2 / INV-10.7）：+inf/NaN/超大整数过了
# 「必须为正」这一关，却让下游 timedelta(...)/httpx.Timeout/循环在运行期
# OverflowError 或失真——如 ACCESS_TOKEN_TTL_SECONDS=10**30 让每次 login
# 在 `now + timedelta(...)` 处 OverflowError、超大 JOB_LOCK_TIMEOUT_SECONDS
# 让死任务几乎不被回收。上界按各字段下游计算的合理量级设定（列全字段，
# 新增数值配置必须同步本表与 _require_positive_numeric_settings）。
_NUMERIC_UPPER_BOUNDS = {
    # timedelta(seconds=) + datetime exp：access 短期凭证，30 天封顶
    "ACCESS_TOKEN_TTL_SECONDS": 30 * 86400,
    # timedelta(days=)：refresh 可长，10 年封顶（datetime.max 溢出界约 7973 年，留足余量）
    "REFRESH_TOKEN_TTL_DAYS": 3650,
    # httpx.Timeout：单次 LLM 调用 1 小时封顶（更大只会占死 worker 循环）
    "LLM_TIMEOUT_SECONDS": 3600.0,
    # 每次失败都全额退避重试，50 次封顶
    "JOB_MAX_ATTEMPTS": 50,
    # running 回收窗口：1 天封顶（更大的窗口=死任务一天不被回收）
    "JOB_LOCK_TIMEOUT_SECONDS": 86400,
    # 轮询睡眠间隔：1 小时封顶（更大=worker 近乎停摆）
    "WORKER_POLL_INTERVAL_SECONDS": 3600.0,
    # 单轮批量：1000 封顶（更大长时间占住循环）
    "WORKER_BATCH_SIZE": 1000,
    # /v1/ask 检索条数：100 封顶（更大无意义且拖慢问答）
    "ASK_TOP_K": 100,
}


def _decode_secret(secret: str) -> bytes | None:
    """把 JWT_SECRET 解码成它编码的随机字节；不是合法编码返回 None。

    - 全部字符都是 hex 数字时按 hex 处理：需 >=64 个字符（解码 >=32 字节）
      且长度为偶数；不满足直接拒绝，不回退按 base64 解释——否则 31 字节
      的 hex（62 个字符）会被当成 46 字节的 base64 放行，绕过长度要求
    - 其余按 base64 / base64url 处理（允许无 padding），严格校验字符集
    """
    if all(c in _HEX_DIGITS for c in secret):
        if len(secret) < _MIN_HEX_CHARS or len(secret) % 2:
            return None
        return bytes.fromhex(secret)
    padded = secret + "=" * (-len(secret) % 4)
    for candidate in (padded, padded.translate(_URLSAFE_TO_STD)):
        try:
            return base64.b64decode(candidate, validate=True)
        except (binascii.Error, ValueError):
            continue
    return None


def _has_usable_http_authority(url) -> bool:
    """threads14 P2（config.py:236）/ INV-10.1：urlsplit 结果有非空 netloc
    不代表有**可用的 HTTP authority**——`http://user@:80`（无 hostname）、
    `https://example.com:notaport`（端口非数字）/`:99999`（越界）都能过
    「netloc 非空」检查，/healthz 照常报 provider，而每次 extract/ask 都在
    OpenAICompatClient 构造/发送请求时才失败。url.port 的属性访问会完成
    端口校验（非法/越界端口在此抛 ValueError，不求值就放过了它），
    hostname 单独判空。threads15 P2 补：显式端口还须 ∈ 1..65535——
    `:0` 是保留端口（urlsplit 对 0 放行），显式写它同属部署错误。"""
    try:
        port = url.port
    except ValueError:
        return False
    return bool(url.hostname) and (port is None or 1 <= port <= 65535)


def _mem0ai_installed() -> bool:
    """探测 mem0ai 是否可 import（INV-10.3）。find_spec 对损坏安装/命名空
    包可能抛错，一律按未安装处理——启动报错好过运行期 ModuleNotFoundError。"""
    try:
        return importlib.util.find_spec("mem0") is not None
    except (ImportError, ValueError):  # pragma: no cover - 损坏安装路径
        return False



class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # 基础
    app_name: str = "personal-agent"
    debug: bool = False
    # 生产默认关闭交互文档（/docs、/redoc、/openapi.json）：本地开发设 true
    enable_docs: bool = False
    # 逗号分隔的 CORS 允许来源；空 = 不启用 CORS（原生 App 无需浏览器跨域）
    cors_allow_origins: str = ""
    # 日志级别（worker 的 basicConfig 与 uvicorn --log-level 用）
    log_level: str = "info"

    # 数据库：生产 postgresql+asyncpg，测试 sqlite+aiosqlite
    database_url: str = "sqlite+aiosqlite:///./app.sqlite3"

    # 鉴权
    jwt_secret: str = DEFAULT_JWT_SECRET
    access_token_ttl_seconds: int = 15 * 60
    refresh_token_ttl_days: int = 30

    # LLM：只接受这两个值，拼错启动即报错（而不是静默当 mock 用）
    llm_provider: Literal["mock", "openai"] = "mock"
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_timeout_seconds: float = 15.0
    # 仅测试用：生产（postgresql）+ mock 默认拒绝启动（假整理/假问答），
    # 跑 PG 测试套件的 CI/本地环境显式置 true 放行（生产环境不要设置）
    allow_mock_llm_in_prod: bool = False

    # 记忆层：inmemory（单进程开发）/ disabled（显式空）/ mem0（生产 pgvector）
    memory_backend: Literal["inmemory", "disabled", "mem0"] = "inmemory"
    # 仅测试用：生产（postgresql）+ inmemory 默认拒绝启动，跑 PG 测试套件
    # 的 CI/本地环境显式置 true 放行（生产环境不要设置；独立 worker 进程
    # 无论该标志都拒绝，见 app.jobs.worker.check_memory_backend_shareable）
    allow_inmemory_in_tests: bool = False
    # mem0 的 embedding 单独配置（可以和 LLM 不同源）
    mem0_embedding_base_url: str = ""
    mem0_embedding_model: str = ""
    # 缺省回落 LLM_API_KEY（共用 OpenAI 兼容源时不用重复填）
    mem0_embedding_api_key: str = ""

    # 流水线
    default_timezone: str = "Asia/Shanghai"
    job_max_attempts: int = 3
    # running 状态超过该秒数视为 worker 中断，任务可被回收重跑
    job_lock_timeout_seconds: int = 300

    # 整理 worker
    worker_poll_interval_seconds: float = 2.0
    worker_batch_size: int = 10

    # /v1/ask 检索条数
    ask_top_k: int = 5

    @model_validator(mode="before")
    @classmethod
    def _compose_pg_url_from_env(cls, data):
        # INV-16（threads12 P1）：分项 PG* 变量 → quote(safe="") 拼接（见
        # _apply_pg_env_database_url）；显式 database_url 永远优先
        return _apply_pg_env_database_url(data)

    @model_validator(mode="after")
    def _reject_insecure_jwt_secret_in_production(self) -> "Settings":
        """生产（postgresql）下 JWT_SECRET 必须是 >=32 字节随机值的编码；
        SQLite 开发测试不受影响。"""
        if not self.database_url.startswith("postgresql"):
            return self
        secret = self.jwt_secret
        problems: list[str] = []
        if secret in (DEFAULT_JWT_SECRET, ENV_EXAMPLE_JWT_PLACEHOLDER):
            problems.append("使用了默认/模板占位值")
        lowered = secret.lower()
        if any(word in lowered for word in _INSECURE_SUBSTRINGS):
            problems.append("包含 change-me/secret/password 等占位或人类可读口令字样")
        raw = _decode_secret(secret)
        if raw is None:
            problems.append(
                "不是合法的 hex/base64 编码（hex 需 >=64 位偶数长，"
                "或 base64/base64url 解码 >=32 字节）"
            )
        else:
            if len(raw) < _MIN_SECRET_BYTES:
                problems.append(f"解码后只有 {len(raw)} 字节（< {_MIN_SECRET_BYTES}）")
            if len(set(raw)) < _MIN_SECRET_DISTINCT_BYTES:
                problems.append(
                    f"解码后不同字节取值数不足（{len(set(raw))} < {_MIN_SECRET_DISTINCT_BYTES}）"
                )
        if problems:
            raise ValueError(
                "生产环境（DATABASE_URL 以 postgresql 开头）的 JWT_SECRET 不安全："
                + "；".join(problems)
                + "。请用随机值生成，如 `python -c \"import secrets;"
                  "print(secrets.token_urlsafe(48))\"` 或 `openssl rand -hex 32`"
            )
        return self

    @model_validator(mode="after")
    def _reject_inmemory_memory_backend_in_production(self) -> "Settings":
        """P1：生产（postgresql）下 MEMORY_BACKEND=inmemory 会静默丢记忆
        ——uvicorn 多 worker 与独立 worker 进程各持一份进程内状态，互相
        不可见且重启即失忆。启动即拒绝；仅在单进程跑 PG 测试套件的
        CI/本地环境可用 ALLOW_INMEMORY_IN_TESTS=true 显式放行（独立
        worker 进程不放行，见 app.jobs.worker）。disabled（显式空后端）
        在任何环境都允许；生产请设 MEMORY_BACKEND=mem0。
        """
        if (
            self.database_url.startswith("postgresql")
            and self.memory_backend == "inmemory"
            and not self.allow_inmemory_in_tests
        ):
            raise ValueError(
                "生产环境（DATABASE_URL 以 postgresql 开头）不允许 "
                "MEMORY_BACKEND=inmemory：inmemory 不能跨 api/worker 进程共享，"
                "多进程/重启即丢记忆。生产请设 MEMORY_BACKEND=mem0（与业务库"
                "同一 PostgreSQL/pgvector）；也可显式设 MEMORY_BACKEND=disabled"
                "（关闭记忆功能，任何环境允许）。仅在单进程跑 PG 测试套件的 "
                "CI/本地环境可设 ALLOW_INMEMORY_IN_TESTS=true 放行（独立 worker "
                "进程无论该标志都拒绝）"
            )
        return self

    @model_validator(mode="after")
    def _require_memory_backend_installed(self) -> "Settings":
        """INV-10.3：MEMORY_BACKEND=mem0 时 mem0ai 必须已安装。记忆 store
        是懒构造——不查的话 api 照常起，首个 /search 或 memory_sync 才
        ModuleNotFoundError。"""
        if self.memory_backend == "mem0" and not _mem0ai_installed():
            raise ValueError(
                "MEMORY_BACKEND=mem0 但未安装 mem0ai：请执行 "
                "pip install -r requirements-mem0.txt（Docker 镜像已装），"
                "然后重启"
            )
        return self

    @model_validator(mode="after")
    def _require_embedding_key_when_customized(self) -> "Settings":
        """INV-10.5：自定义 embedding 源（MEM0_EMBEDDING_BASE_URL /
        MEM0_EMBEDDING_API_KEY 任一设置）时，解析出的凭据
        （MEM0_EMBEDDING_API_KEY，缺省回落 LLM_API_KEY）必须非空。"""
        if not (self.mem0_embedding_base_url or self.mem0_embedding_api_key):
            return self
        if not (self.mem0_embedding_api_key or self.llm_api_key):
            raise ValueError(
                "MEM0_EMBEDDING_BASE_URL/MEM0_EMBEDDING_API_KEY 已设置但缺少"
                " embedding 凭据：请设置 MEM0_EMBEDDING_API_KEY，或与 LLM 共源"
                "（不设置 MEM0_EMBEDDING_* 即回落 LLM_API_KEY）"
            )
        return self

    @model_validator(mode="after")
    def _require_complete_openai_config(self) -> "Settings":
        """INV-10.1（修 C3，任何环境）：LLM_PROVIDER=openai 时
        LLM_API_KEY / LLM_BASE_URL / LLM_MODEL 三者必须齐备——缺任何一项
        启动即 ValueError，而不是运行期拼出相对 /chat/completions URL 或
        空 model，让每次 extract/ask 都失败。threads15 P2 补：**纯空白值
        同样算缺**（`"   "` 能过非空检查，落到 httpx/请求体里才暴露），
        且 LLM_API_KEY 拒绝 HTTP header 非法控制字符（C0 0x00-0x1F 与
        DEL 0x7F——该值原样进 Authorization 头，含控制字符的每次请求
        才失败）。错误文案只报名不报值（INV-14）。"""
        if self.llm_provider != "openai":
            return self
        missing = [
            name
            for name, value in (
                ("LLM_API_KEY", self.llm_api_key),
                ("LLM_BASE_URL", self.llm_base_url),
                ("LLM_MODEL", self.llm_model),
            )
            if not value.strip()
        ]
        if missing:
            raise ValueError(
                "LLM_PROVIDER=openai 必须同时设置 " + "、".join(missing) + "（缺任一项启动即失败，不会静默回落）"
            )
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in self.llm_api_key):
            raise ValueError(
                "LLM_API_KEY 含 HTTP header 非法控制字符（C0/DEL）——该值"
                "会原样进 Authorization 头，要到每次请求发送时才失败，"
                "不会在启动时报出来"
            )
        return self

    @model_validator(mode="after")
    def _require_absolute_http_llm_base_url(self) -> "Settings":
        """threads13 P2（config.py:188）/ INV-10.1：LLM_PROVIDER=openai 时
        LLM_BASE_URL 必须是**绝对的 http/https URL**。此前「三元组齐备」
        只查非空——裸主机名（llm.internal）、含空白的值、ftp:// 这类非
        http(s) scheme 都能过启动校验，/healthz 照常报 provider，而每次
        extract/ask 都在 httpx 构造请求时才失败（确定性部署错误耗尽全部
        抽取任务才暴露）。任何环境生效；错误文案不含配置原值（INV-14）。
        threads14 P2 补：netloc 非空不等于有可用的 HTTP authority——
        无 hostname（http://user@:80）或端口非法（:notaport）的值同样
        要到 OpenAICompatClient 构造/发送请求时才失败，见
        _has_usable_http_authority。"""
        if self.llm_provider != "openai":
            return self
        raw = self.llm_base_url
        url = urlsplit(raw)
        if (
            any(ch.isspace() for ch in raw)
            or url.scheme not in ("http", "https")
            or not url.netloc
            or not _has_usable_http_authority(url)
        ):
            raise ValueError(
                "LLM_BASE_URL 必须是绝对的 http/https URL（如 "
                "https://llm.example.com/v1）：当前值是裸主机名/相对路径、"
                "含空白字符、scheme 不是 http(s)、缺 hostname（如 "
                "http://user@:80），或端口非法/越界/保留端口（如 "
                ":notaport、:99999、:0）——这类值要到运行期 httpx 构造/"
                "发送请求时才失败，不会在启动时报出来"
            )
        return self

    @model_validator(mode="after")
    def _validate_default_timezone(self) -> "Settings":
        """INV-10.2（修 C7，任何环境）：DEFAULT_TIMEZONE 必须是合法 IANA
        时区名。请求省略 timezone 时该值会落到每条新笔记上，拼错的名字
        会让 naive recorded_at 静默按 UTC 解释、提醒解析回退别的时区。"""
        try:
            ZoneInfo(self.default_timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(
                f"DEFAULT_TIMEZONE 必须是合法 IANA 时区名：{self.default_timezone}"
            ) from exc
        return self

    @model_validator(mode="after")
    def _reject_misconfigured_llm_in_production(self) -> "Settings":
        """P2：生产（postgresql）下 LLM 配置错误必须启动报错，而不是静默
        回落 MockLLM（整理/问答全变假实现，问题被掩盖到用户侧）。

        - LLM_PROVIDER=mock：拒绝（假实现），仅跑 PG 测试套件的 CI/本地
          环境可设 ALLOW_MOCK_LLM_IN_PROD=true 放行。
        - openai 缺 key/base_url/model 的拒绝在任何环境生效
          （_require_complete_openai_config）。
        provider 拼错由 Literal 校验直接报错（任何环境）。
        """
        if not self.database_url.startswith("postgresql"):
            return self
        if self.llm_provider == "mock" and not self.allow_mock_llm_in_prod:
            raise ValueError(
                "生产环境（DATABASE_URL 以 postgresql 开头）不允许 LLM_PROVIDER=mock"
                "（整理/问答会是确定性假实现）：请配置 openai。"
                "仅在跑 PG 测试套件的 CI/本地环境可设 ALLOW_MOCK_LLM_IN_PROD=true 放行"
            )
        return self

    @model_validator(mode="after")
    def _require_positive_numeric_settings(self) -> "Settings":
        """INV-10.7（threads8 P2，config.py:99）：数值配置必须为正。0/负值
        会让相应循环或调用静默失效（WORKER_BATCH_SIZE=0 时 worker 日志一切
        正常但循环体永不执行、任何笔记都不被整理）——启动时一次性列出
        **全部**非正字段。threads13 P2 补有限上界：+inf/超大整数过了「为正」
        这关，却让下游 timedelta/httpx.Timeout/循环在运行期 OverflowError
        （如 ACCESS_TOKEN_TTL_SECONDS 过大让 login 的 `now + timedelta(...)`
        抛 OverflowError）。新增数值型配置必须加进下表与 _NUMERIC_UPPER_BOUNDS
        并同步 .env.example（docs/server-invariants.md 自查清单第 15 条）。"""
        checks = {
            "ACCESS_TOKEN_TTL_SECONDS": self.access_token_ttl_seconds,
            "REFRESH_TOKEN_TTL_DAYS": self.refresh_token_ttl_days,
            "LLM_TIMEOUT_SECONDS": self.llm_timeout_seconds,
            "JOB_MAX_ATTEMPTS": self.job_max_attempts,
            "JOB_LOCK_TIMEOUT_SECONDS": self.job_lock_timeout_seconds,
            "WORKER_POLL_INTERVAL_SECONDS": self.worker_poll_interval_seconds,
            "WORKER_BATCH_SIZE": self.worker_batch_size,
            "ASK_TOP_K": self.ask_top_k,
        }
        # not (v > 0) 连 NaN 也拦下（NaN <= 0 为 False）
        bad = {name: value for name, value in checks.items() if not (value > 0)}
        if bad:
            raise ValueError(
                "数值配置必须为正（0/负值会让相应循环或调用静默失效）："
                + "、".join(f"{NAME}={value!r}" for NAME, value in bad.items())
            )
        # 上界判定同时拦下 +inf（inf > 任何有限上界）；NaN 已被上一条拦住
        oversized = {
            name: value
            for name, value in checks.items()
            if value > _NUMERIC_UPPER_BOUNDS[name]
        }
        if oversized:
            raise ValueError(
                "数值配置超出有限上界（+inf/超大整数会让下游 timedelta/"
                "httpx.Timeout/循环在运行期 OverflowError 或失真，如 login "
                "的 now+timedelta）："
                + "、".join(
                    f"{name}={value!r}（上界 {_NUMERIC_UPPER_BOUNDS[name]!r}）"
                    for name, value in oversized.items()
                )
            )
        return self

    @model_validator(mode="after")
    def _reject_sqlalchemy_echo(self) -> "Settings":
        # INV-14（threads9）：见 _reject_echo_in_url——DbSettings 同款校验
        _reject_echo_in_url(self.database_url)
        return self


# libpq 惯例的分项连接变量（INV-16，threads12 P1）：compose 注入这些而
# 不是把密码原样拼进 DATABASE_URL——密码含 URI 分隔符（@ : / # % 空格等）
# 时原样拼接会让 URL 解析器拆错组件（主机/库名被吃进密码），连接失败且
# 报错不指向真实原因
_PG_ENV_KEYS = ("PGHOST", "PGPORT", "PGUSER", "PGPASSWORD", "PGDATABASE")
# 触发拼接的最小意图信号：PGPASSWORD 或 PGHOST 才表明「真的要连 PG」，
# 其余变量单独出现（PGPORT 之类）不劫持本地 SQLite 默认
_PG_TRIGGER_KEYS = ("PGPASSWORD", "PGHOST")
_PG_DEFAULTS = {"PGUSER": "personal", "PGHOST": "postgres", "PGPORT": "5432", "PGDATABASE": "personal_agent"}


def postgres_url_from_env(env: Mapping[str, str]) -> str | None:
    """从分项变量拼 PG 连接 URL（INV-16，threads12 P1；全仓唯一拼接点）。

    用户名/密码必须 percent-encode 后才能进 URL userinfo——SQLAlchemy 的
    make_url 会按 URI 规则拆组件并百分号解码，密码含 @:/#% 等字符时
    原样拼接会拆错位置、认证用错凭据。有意用 ``quote(safe="")`` 而不是
    ``quote_plus``：后者把空格编成 ``+``，而 make_url 不把 ``+`` 还原成
    空格（userinfo 里 ``+`` 就是字面加号），含空格的密码会轮换失败。
    无 PGPASSWORD/PGHOST 时返回 None（调用方沿用 DATABASE_URL /
    默认值）。密码绝不进日志/错误消息（INV-14）。

    threads14 P2（config.py:357）：PGHOST 在而 PGPASSWORD 缺失时抛受控
    ConfigError，不再 KeyError（_PG_DEFAULTS 只兜底 PGUSER/PGHOST/
    PGPORT/PGDATABASE，没有密码默认值可兜）。有意选「明确报缺」而不是
    默认空密码：静默把「忘配密码」当 trust 放行会掩盖部署错误（与
    INV-16「密钥不静默进错位置」同向）；确需无密码的 trust 连接时显式
    设 PGPASSWORD=""（空串合法，编码后 userinfo 密码段为空）。

    threads15 P2（config.py:395）：PGHOST 是 IPv6 字面量时必须加方括号
    再进 URL authority——`2001:db8::10` 不加括号，make_url 会把地址里
    的冒号当 host:port 分隔符拆组件，解析直接 ValueError（部署错误拖
    到建连/启动校验才炸且报错不知所云）。主机名不可能含冒号，含冒号
    即按 IPv6 处理；用户已带括号（`[2001:db8::10]`）不重复加。
    """
    if not any(key in env for key in _PG_TRIGGER_KEYS):
        return None
    if "PGPASSWORD" not in env:
        raise ConfigError(
            "配置校验失败：\n"
            "PGHOST: 触发了分项 PG 变量拼接（PGHOST/PGPASSWORD）但缺少 "
            "PGPASSWORD——分项变量须成套提供（PGUSER/PGHOST/PGPORT/"
            "PGDATABASE 有缺省回落，密码没有）。确需无密码（trust）连接"
            "请显式设置 PGPASSWORD 为空串"
        )
    merged = {**_PG_DEFAULTS, **{k: str(v) for k, v in env.items() if k in _PG_ENV_KEYS}}
    userinfo = f"{quote(merged['PGUSER'], safe='')}:{quote(merged['PGPASSWORD'], safe='')}"
    # IPv6 字面量加方括号（threads15 P2，见上）；主机名不含冒号，
    # 含冒号即 IPv6，已带括号不重复加
    host = merged["PGHOST"]
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"postgresql+asyncpg://{userinfo}@{host}:{merged['PGPORT']}/{merged['PGDATABASE']}"


def _apply_pg_env_database_url(data: object) -> object:
    """Settings/DbSettings 的 mode="before" 钩子：database_url 未提供且存在
    分项 PG* 变量时拼出 PG URL（INV-16）。

    变量两路来源、构造 kwargs 优先：init kwargs（测试 / compose 解析路径
    注入，键大小写不敏感——pydantic 字段名是小写、extra=ignore 不妨碍
    before-validator 读到原始键）与真实环境变量（compose 的 environment
    注入）。显式 database_url（含 env_file .env 的本地默认）永远优先——
    compose 因此把 DATABASE_URL 置空串压过 .env 的 SQLite 默认值，空串
    在这里视为「未显式配置」。
    """
    if not isinstance(data, dict) or data.get("database_url"):
        return data
    env = {
        **{k: v for k, v in os.environ.items() if k in _PG_ENV_KEYS},
        **{k.upper(): v for k, v in data.items() if k.upper() in _PG_ENV_KEYS},
    }
    url = postgres_url_from_env(env)
    if url:
        data["database_url"] = url
    return data


class ConfigError(RuntimeError):
    """配置校验失败（INV-14，threads10 P1，config.py:68）：消息只含字段
    位置（loc）与校验文案（msg/type），不含任何配置原值——pydantic
    ValidationError 的 `input_value` 截断 repr 会露出**尾部**字段的值
    （jwt_secret / database_url 里的密码，实测复现），而 get_settings 跑
    在一切中间件之前（api 模块级 create_app、worker 模块级 basicConfig），
    traceback 会整段落 uvicorn/进程日志。有意不用 SecretStr：只护 repr、
    不覆盖 database_url 的 password 部分、且要改所有取值点与既有测试；
    边界收口一次覆盖全部入口（api import / worker / cli），评估见
    docs/server-invariants.md 第 2 节表。"""


def _sanitized_config_error(exc: ValidationError) -> ConfigError:
    """ValidationError → 只含 loc/msg/type 的 ConfigError（strip input/ctx）。

    msg 的来源是本仓校验器的固定文案或 pydantic 内置模板，均不含输入值；
    有意保留的两处非密钥插值（DEFAULT_TIMEZONE、数值配置值）只出现在
    操作者自己的控制台，不是跨用户回显面（INV-14 边界，文档载明）。"""
    lines = []
    for err in exc.errors():
        loc = ".".join(str(part) for part in err.get("loc") or ()) or "<settings>"
        lines.append(f"{loc}: {err.get('msg') or err.get('type')}")
    return ConfigError("配置校验失败：\n" + "\n".join(lines))


@lru_cache
def get_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as exc:
        # from None 抑制隐式 __context__：否则原 ValidationError 仍随
        # 「During handling of…」链式 traceback 打出，脱敏白做
        raise _sanitized_config_error(exc) from None


def _reject_echo_in_url(database_url: str) -> None:
    """INV-14（threads9）：DATABASE_URL 的 URL query 会原样传给
    create_async_engine 当引擎参数——`?echo=true` 静默开 echo，SQL 与
    绑定参数（笔记正文等用户内容）整体进 sqlalchemy.engine 日志。任何
    环境启动即拒；排障需要 SQL 时本地临时改代码，不开配置口。"""
    from sqlalchemy.engine import make_url

    try:
        query = make_url(database_url).query
    except ValueError:
        # URL 本身非法：交给真正创建引擎的路径报错，这里不抢报
        return
    echo = query.get("echo")
    if echo is not None and str(echo).strip().lower() not in ("", "0", "false", "no", "off"):
        raise ValueError(
            "DATABASE_URL 不得通过 URL query 开 SQLAlchemy echo（如 ?echo=true）："
            "echo 会把 SQL 与绑定参数（用户内容）打进 sqlalchemy.engine 日志"
            "（INV-14）。排障需要 SQL 时本地临时改代码。"
        )


class DbSettings(BaseSettings):
    """只含数据库层的配置（threads7 P1 / INV-9 校验分层）。

    init-db（compose 的 one-shot migrate 服务）只做建表：让它加载完整
    Settings 会把 LLM/记忆后端/JWT 的生产校验也拖进建表路径——migrate
    沿用 .env 的本地默认（MEMORY_BACKEND=inmemory、LLM_PROVIDER=mock）
    叠加 compose 的 PG 覆盖时，会撞上生产拓扑校验直接退出，全新部署停在
    建表一步。建表只依赖 DATABASE_URL；api/worker 仍按各自 command 过
    完整 Settings 的启动校验（有测试机械验证，见 tests/test_compose.py）。
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "sqlite+aiosqlite:///./app.sqlite3"

    @model_validator(mode="before")
    @classmethod
    def _compose_pg_url_from_env(cls, data):
        # 同 Settings（INV-16，threads12）：migrate 服务同样注入分项 PG* 变量
        return _apply_pg_env_database_url(data)

    @model_validator(mode="after")
    def _reject_sqlalchemy_echo(self) -> "DbSettings":
        # 同 Settings（INV-14，threads9）：init-db 的引擎同样吃 URL query
        _reject_echo_in_url(self.database_url)
        return self


@lru_cache
def get_db_settings() -> DbSettings:
    # 同 get_settings 的脱敏边界（database_url 内嵌 PG 密码）
    try:
        return DbSettings()
    except ValidationError as exc:
        raise _sanitized_config_error(exc) from None
