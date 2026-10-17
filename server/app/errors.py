"""统一错误格式：{"error":{"code","message"}}。"""
from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        self.status_code = status_code
        self.code = code
        self.message = message
        super().__init__(message)


def error_body(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


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
    return JSONResponse(status_code=422, content=error_body("validation_error", str(exc.errors())))


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
