"""统一错误格式：{"error":{"code","message"}} + 异常安全摘要（INV-14）。"""
import json

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import exc as sa_exc
from starlette.exceptions import HTTPException as StarletteHTTPException


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        self.status_code = status_code
        self.code = code
        self.message = message
        super().__init__(message)


def error_body(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


def error_code(exc: BaseException) -> str:
    """异常 → 粗粒度错误码（INV-14）。子类判定必须排在父类前面
    （IntegrityError/OperationalError 都是 StatementError 的子类）。"""
    if isinstance(exc, ApiError):
        return exc.code
    if isinstance(exc, sa_exc.IntegrityError):
        return "db_integrity"
    if isinstance(exc, sa_exc.OperationalError):
        return "db_operational"
    if isinstance(exc, sa_exc.StatementError):
        return "db_statement"
    if isinstance(exc, json.JSONDecodeError):
        return "json_parse"
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, (ConnectionError, OSError)):
        return "network"
    return "internal"


def safe_error_summary(exc: BaseException) -> dict:
    """异常的安全摘要（INV-14，threads8 P1）：**只有**类型名 + 粗粒度错误码。

    有意不保留任何消息文本（连截断/脱敏的都不留）：SQLAlchemy
    StatementError 的 str(exc) 默认内嵌 SQL 绑定参数——参数就是笔记正文/
    标题/待办这类自然语言用户内容，数字串/邮箱/URL 式脱敏挡不住自由文本，
    任何长度的摘要都会把用户内容带进日志与 jobs.last_error。类型名+错误码
    足以定位失败类别，具体细节靠可复现测试或本地复跑获取。"""
    return {"type": type(exc).__name__, "code": error_code(exc)}


def safe_error_text(exc: BaseException) -> str:
    """safe_error_summary 的单行形态（日志与 last_error 用）。"""
    summary = safe_error_summary(exc)
    return f"{summary['type']}/{summary['code']}"


async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=error_body(exc.code, exc.message))


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = {
        400: "bad_request",
        401: "unauthorized",
        403: "forbidden",
        404: "not_found",
        409: "conflict",
        422: "validation_error",
        429: "rate_limited",
        500: "internal_error",
    }.get(exc.status_code, "error")
    return JSONResponse(status_code=exc.status_code, content=error_body(code, str(exc.detail)))


async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """422 只回 pydantic 的 type/loc/msg（INV-14，threads8 P1）：errors()
    里的 input/ctx 是请求原值（正文、refresh_token、asr……）的整段回显，
    不落响应。msg 来自固定模板（自定义校验消息可能引用该请求自己提交的
    单个字段值，如 timezone 名——客户端修正请求所需，保留该边界）。"""
    safe_errors = [
        {k: err[k] for k in ("type", "loc", "msg") if k in err} for err in exc.errors()
    ]
    return JSONResponse(
        status_code=422, content=error_body("validation_error", str(safe_errors))
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=500, content=error_body("internal_error", "internal server error"))


async def recursion_error_handler(request: Request, exc: RecursionError) -> JSONResponse:
    """超深嵌套的 JSON 在解析阶段就炸掉解释器递归栈（json 的 C scanner
    递归下降，schema 层的深度限制根本没机会看到）：拦成 422 而不是 500
    （4c1c9be P2-4）。常规深度由 schemas 的 _MAX_ASR_DEPTH 拒绝，这里是
    恶意超深体的兜底。"""
    return JSONResponse(
        status_code=422, content=error_body("validation_error", "请求体嵌套过深")
    )
