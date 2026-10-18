"""docker-compose 各服务的启动校验（threads7 P1 / INV-9 校验分层）。

本机无 Docker：用 PyYAML 解析 server/docker-compose.yml，以 .env.example
为底、按 README 记载的生产必填项代入合法值，机械复现 compose 的变量
代入后，按每个服务的 command 跑它真实会跑的启动校验：

- migrate（`python -m app.cli init-db`）只过 **DbSettings**（数据库层）
  ——沿用 .env 本地默认（MEMORY_BACKEND=inmemory）也不被生产校验挡住；
- api（uvicorn）过完整 **Settings**（其 compose environment 已把
  MEMORY_BACKEND 覆盖为 disabled）；
- worker 过完整 Settings **+ worker 拓扑检查**（check_memory_backend_shareable）。
"""
import pathlib
import re
import secrets

import pytest
import yaml
from pydantic import ValidationError

from app.config import DbSettings, Settings

SERVER_DIR = pathlib.Path(__file__).resolve().parent.parent
COMPOSE = SERVER_DIR / "docker-compose.yml"
ENV_EXAMPLE = SERVER_DIR / ".env.example"


def _env_example_values() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


def _production_env() -> dict[str, str]:
    """compose 部署形态的 .env：模板为底，按 README「生产必填」换成合法
    生产值——JWT_SECRET 随机、LLM 三元组齐备、POSTGRES_PASSWORD（模板里
    是注释占位）。"""
    env = _env_example_values()
    env.update(
        {
            "JWT_SECRET": secrets.token_urlsafe(48),
            "LLM_PROVIDER": "openai",
            "LLM_API_KEY": "sk-test-only",
            "LLM_BASE_URL": "https://llm.example.com/v1",
            "LLM_MODEL": "test-model",
            "POSTGRES_PASSWORD": "pg-local-test-only",
            # 部署形态不会设置测试放行标志；显式置 false 隔离 PG 测试
            # 套件 conftest 设置的环境变量（否则用例行为随运行方式漂移）
            "ALLOW_INMEMORY_IN_TESTS": "false",
            "ALLOW_MOCK_LLM_IN_PROD": "false",
        }
    )
    return env


def _interpolate(value: str, env: dict[str, str]) -> str:
    """compose 的 ${VAR} / ${VAR:-default} 代入（本文件只用到这两种）。"""

    def sub(match: re.Match) -> str:
        var, default = match.group(1), match.group(2)
        if var in env:
            return env[var]
        return default if default is not None else ""

    return re.sub(r"\$\{([A-Z_][A-Z0-9_]*)(?::-([^}]*))?\}", sub, value)


def _service_env(service: dict, base: dict[str, str]) -> dict[str, str]:
    """env_file 的底 + service.environment 的覆盖，均完成变量代入。"""
    env = dict(base)
    environment = service.get("environment") or {}
    items = environment.items() if isinstance(environment, dict) else environment
    for key, value in items:
        env[key] = _interpolate(str(value), env)
    return env


def _settings_kwargs(env: dict[str, str]) -> dict:
    """环境变量名 → Settings 字段名（小写）；多余键由 extra="ignore" 丢弃。"""
    return {key.lower(): value for key, value in env.items()}


def test_compose_services_pass_startup_validation():
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    services = compose["services"]
    assert {"migrate", "api", "worker"} <= set(services), "compose 拆分后三服务仍在"

    base = _production_env()

    # migrate：init-db 只做数据库层校验——.env 沿用本地默认
    # （MEMORY_BACKEND=inmemory）也不能在建表一步被生产校验挡住
    migrate_env = _service_env(services["migrate"], base)
    db_url = DbSettings(**_settings_kwargs(migrate_env)).database_url
    assert db_url.startswith("postgresql"), "migrate 的 DATABASE_URL 指向 compose 的 PG"

    # api：完整 Settings（environment 已覆盖 MEMORY_BACKEND=disabled，
    # 否则 .env 的 inmemory 默认 + PG 会被生产拓扑校验拒绝）
    api_env = _service_env(services["api"], base)
    assert api_env["MEMORY_BACKEND"] == "disabled"
    Settings(**_settings_kwargs(api_env))  # 不抛即通过

    # worker：完整 Settings + worker 拓扑检查（disabled 不受限）
    from app.jobs.worker import check_memory_backend_shareable

    worker_env = _service_env(services["worker"], base)
    assert worker_env["MEMORY_BACKEND"] == "disabled"
    check_memory_backend_shareable(Settings(**_settings_kwargs(worker_env)))


def test_compose_migrate_would_reject_full_settings_layering_is_real():
    """反证分层是必要的：若 migrate 走完整 Settings，沿用 .env 本地默认
    （inmemory）+ PG 的组合会被生产校验拒绝——这正是 threads7 P1 要修的
    「全新部署停在建表一步」。"""
    migrate_env = _service_env(
        yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]["migrate"],
        _production_env(),
    )
    kwargs = _settings_kwargs(migrate_env)
    with pytest.raises(ValidationError, match="MEMORY_BACKEND"):
        Settings(**kwargs)
    # 而数据库层校验对同一环境照常通过
    assert DbSettings(**kwargs).database_url.startswith("postgresql")


async def test_init_db_does_not_load_full_settings(monkeypatch):
    """INV-9 校验分层（毒化验证）：init-db 只加载 DbSettings——把完整
    Settings 的入口毒化后 init_db 照常完成，证明建表路径不经过完整校验，
    也不会被 LLM/记忆后端的配置问题挡住。"""
    from app import db as db_module
    from app.config import get_db_settings

    def boom():
        raise AssertionError("init-db 不得加载完整 Settings（只走 DbSettings）")

    monkeypatch.setattr(db_module, "get_settings", boom)
    monkeypatch.setattr("app.config.get_settings", boom)
    # DbSettings 入口保持可用：init_db 应经由它拿到 DATABASE_URL
    assert get_db_settings().database_url
    await db_module.init_db()  # 当前测试库（SQLite / PG）建表成功


def test_uvicorn_commands_disable_access_log():
    """threads9 P1（docker-compose.yml:44 / Dockerfile CMD）/ INV-14：
    uvicorn 默认开 access log，请求行含完整 query——`GET /v1/search?q=
    <笔记正文>` 整行进 stdout（compose 日志收集面）。每条 uvicorn 命令
    （compose 各服务的 command 与 Dockerfile 的 CMD）必须带
    --no-access-log；进程内请求级日志由 AccessLogMiddleware 承担。"""
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    uvicorn_commands: list[tuple[str, str]] = []
    for name, service in compose["services"].items():
        command = service.get("command") or ""
        if "uvicorn" in command:
            uvicorn_commands.append((name, command))
    assert uvicorn_commands, "compose 必须至少有一个 uvicorn 服务（api）"
    for name, command in uvicorn_commands:
        assert "--no-access-log" in command, f"compose 服务 {name} 的 uvicorn 命令缺 --no-access-log"

    dockerfile = (SERVER_DIR / "Dockerfile").read_text(encoding="utf-8")
    cmd_lines = [ln for ln in dockerfile.splitlines() if ln.strip().upper().startswith("CMD")]
    assert cmd_lines, "Dockerfile 必须有 CMD"
    for line in cmd_lines:
        assert "uvicorn" in line
        assert "--no-access-log" in line, f"Dockerfile CMD 缺 --no-access-log：{line}"


def test_readme_documented_uvicorn_command_disables_access_log():
    """threads10 P1（README.md:52）/ INV-14：文档里的启动示例与部署清单
    同口径——照 README 起步的本地开发会把 `GET /v1/search?q=<笔记正文>`
    整行打进终端/开发日志。解析 README 全部 fenced code block，每条含
    uvicorn 的命令行都必须带 --no-access-log。"""
    readme = SERVER_DIR / "README.md"
    uvicorn_lines: list[str] = []
    in_block = False
    for line in readme.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_block = not in_block
            continue
        if in_block and "uvicorn" in line:
            uvicorn_lines.append(line)
    assert uvicorn_lines, "README 必须至少记载一条 uvicorn 启动命令（本地开发）"
    for line in uvicorn_lines:
        assert "--no-access-log" in line, f"README 文档示例的 uvicorn 命令缺 --no-access-log：{line}"


def test_dockerignore_excludes_secrets_and_local_data():
    """threads9 P1（docker-compose.yml:21）/ INV-14：compose 三个服务都
    build: .，无 .dockerignore 时 server/.env（JWT/LLM/PG 密钥）与本地
    SQLite 笔记库（app.sqlite3、tests/_test.sqlite3）会整体发给 builder
    ——Dockerfile 不 COPY 也拦不住上下文传输。锁定必要模式与镜像 COPY
    白名单（tests 不进镜像）。"""
    dockerignore = SERVER_DIR / ".dockerignore"
    assert dockerignore.exists(), "server/.dockerignore 必须存在（构建上下文收口）"
    patterns = {
        line.strip()
        for line in dockerignore.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    for required in (
        ".env",  # 生产密钥（README 生产部署在 server/ 创建）
        ".env.*",
        "!.env.example",  # 模板保留进上下文（threads10）；真实 .env 仍被上面两条排除
        "*.db",
        "*.sqlite",  # 本地 SQLite 笔记库（含 tests/_test.sqlite3）
        "*.sqlite-*",
        # threads11 C 类（INV-14）：README 本地库形态是 app.sqlite3 ——
        # 只有 *.sqlite / *.db 挡不住它及其 -wal/-shm/-journal sidecar
        "*.sqlite3",
        "*.sqlite3-*",
        ".venv/",
        "__pycache__/",
        "tests/",
        ".git/",
    ):
        assert required in patterns, f".dockerignore 缺少必要模式：{required}"

    dockerfile = (SERVER_DIR / "Dockerfile").read_text(encoding="utf-8")
    copy_sources = re.findall(r"(?im)^COPY\s+(\S+)", dockerfile)
    assert copy_sources, "Dockerfile 必须显式 COPY（不允许 COPY . 全量进镜像）"
    for source in copy_sources:
        assert source not in (".", "./"), "镜像不得 COPY 整个上下文"
        assert "tests" not in source, f"tests 不得进镜像：{source}"
        assert source in ("requirements-app.txt", "app"), f"镜像 COPY 超出白名单：{source}"


def test_gitignore_excludes_sqlite_and_sidecar_files():
    """threads12 P1（.gitignore:8）/ INV-14：README 记载的本地默认库是
    app.sqlite3，但 *.sqlite3 匹配不到 -wal/-shm/-journal sidecar（崩溃
    残留/事务进行中，含完整或部分笔记页）——.gitignore 必须带上与
    .dockerignore 对齐的全部 sqlite/db 模式，否则一次 git add 就能把
    旁路文件提交进仓库（本地库进 git 与进构建上下文是同一泄漏类的
    两个面）。"""
    gitignore = SERVER_DIR / ".gitignore"
    assert gitignore.exists(), "server/.gitignore 必须存在"
    patterns = {
        line.strip()
        for line in gitignore.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    for required in ("*.sqlite", "*.sqlite-*", "*.sqlite3", "*.sqlite3-*", "*.db", "*.db-*"):
        assert required in patterns, f".gitignore 缺少必要模式：{required}"

    # 与 .dockerignore 的 sqlite/db 模式集一致（两边不允许漂移）
    dockerignore = (SERVER_DIR / ".dockerignore").read_text(encoding="utf-8").splitlines()
    docker_sqlite = {
        line.strip()
        for line in dockerignore
        if line.strip().startswith(("*.", "!"))
        and ("sqlite" in line or ".db" in line)
    }
    git_sqlite = {p for p in patterns if "sqlite" in p or p.endswith(".db") or ".db-" in p}
    assert git_sqlite == docker_sqlite, f"gitignore/dockerignore 的 sqlite 模式漂移：{git_sqlite} vs {docker_sqlite}"
