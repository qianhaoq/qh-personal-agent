"""FastAPI 入口。"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import get_settings
from app.db import dispose_engine, startup_create_all
from app.errors import (
    ApiError,
    api_error_handler,
    http_exception_handler,
    recursion_error_handler,
    unhandled_exception_handler,
    validation_exception_handler,
)
from app.middleware import NulRejectMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI):
    # PG 下不在启动时建表（多进程并发 create_all 会撞 DDL，见 INV-9）：
    # 先跑 python -m app.cli init-db；表缺失时首个查询快速失败，暴露
    # 部署错误。SQLite（本地开发/测试）保留启动建表
    await startup_create_all()
    yield
    await dispose_engine()


def create_app() -> FastAPI:
    """按当前配置构建应用。

    生产默认值（P2 审查）：
    - ENABLE_DOCS=false：/docs、/redoc、/openapi.json 不暴露（避免把
      全部接口面（含鉴权细节）公开给扫描器）；本地开发设 true 查看；
    - CORS_ALLOW_ORIGINS 为空时不挂 CORS 中间件：原生 App 不走浏览器
      跨域，默认多开一个来源就多一分配置出错的面。
    """
    settings = get_settings()
    docs_enabled = settings.enable_docs
    app = FastAPI(
        title="personal-agent",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
    )

    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    # 超深嵌套 JSON 解析期的 RecursionError → 422（见 app.errors）
    app.add_exception_handler(RecursionError, recursion_error_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)

    # NUL 拒绝中间件（INV-7）：path/query 解码后含 \x00 → 422，不进路由
    app.add_middleware(NulRejectMiddleware)

    if settings.cors_allow_origins:
        from fastapi.middleware.cors import CORSMiddleware

        origins = [o.strip() for o in settings.cors_allow_origins.split(",") if o.strip()]
        if origins:
            app.add_middleware(
                CORSMiddleware,
                allow_origins=origins,
                allow_credentials=True,
                allow_methods=["*"],
                allow_headers=["*"],
            )

    # 路由注册（放在 app 创建之后，避免路由 handlers 早于 app 定义）
    from app.api import ask, auth, notes, sync

    app.include_router(auth.router)
    app.include_router(notes.router)
    app.include_router(sync.router)
    app.include_router(ask.router)
    return app


app = create_app()


@app.get("/healthz", tags=["health"])
async def healthz():
    from fastapi.responses import JSONResponse
    from sqlalchemy import text

    from app.db import get_engine

    db_ok = True
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001 健康检查必须兜住一切异常，返回降级状态而不是 500
        db_ok = False
    body = {"ok": db_ok, "db": db_ok, "llm": _llm_status()}
    if not db_ok:
        # 依赖数据库健康检查的负载均衡器据此摘除实例
        return JSONResponse(status_code=503, content=body)
    return body


def _llm_status() -> str:
    # config 已用 Literal 限制取值且生产下校验过（mock/openai 配错启动即拒），
    # 如实回报即可；这里不再做“缺 key 就报 mock”的猜测
    from app.config import get_settings

    return get_settings().llm_provider
