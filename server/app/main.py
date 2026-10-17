"""FastAPI 入口。"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.db import create_all, dispose_engine
from app.errors import (
    ApiError,
    api_error_handler,
    http_exception_handler,
    unhandled_exception_handler,
    validation_exception_handler,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await create_all()  # S1 启动时建表，后续换 Alembic（见 README）
    yield
    await dispose_engine()


app = FastAPI(title="personal-agent", version="0.1.0", lifespan=lifespan)

app.add_exception_handler(ApiError, api_error_handler)
app.add_exception_handler(StarletteHTTPException, http_exception_handler)
app.add_exception_handler(RequestValidationError, validation_exception_handler)
app.add_exception_handler(Exception, unhandled_exception_handler)


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
    from app.config import get_settings

    settings = get_settings()
    if settings.llm_provider == "openai" and settings.llm_api_key:
        return "openai"
    return "mock"


# 路由注册（放在 app 创建之后，避免路由 handlers 早于 app 定义）
from app.api import ask, auth, notes, sync

app.include_router(auth.router)
app.include_router(notes.router)
app.include_router(sync.router)
app.include_router(ask.router)
