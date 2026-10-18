"""中间件：NUL 拒绝（INV-7）、未处理异常兜底与模板级访问日志（INV-14）。

对每个请求的 URL path 与 query string **解码后**检查 \\x00，命中直接
返回 422，不进入路由——覆盖所有路由的 path/query 参数（`/v1/notes/n%00x`
这类编码 NUL 若直达 PostgreSQL 会触发编码错误变 500，修 C8）。

第二道防线是 schemas 层的 NoNulStr/reject_nul（请求体字段与显式声明的
path/query 参数注解，见 app/schemas.py）；本中间件是第一道、也是覆盖
面兜底的那一道。header 当前无任何值入库（authorization 只做 JWT 解析），
故不检查；未来任何从 header 取值入库的字段必须走 NoNulStr。
"""
import logging
import time
from urllib.parse import unquote

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.errors import error_body, safe_error_text

logger = logging.getLogger(__name__)
access_logger = logging.getLogger("access")


def _contains_nul(raw: str) -> bool:
    return "\x00" in unquote(raw)


class NulRejectMiddleware:
    """纯 ASGI 中间件：注册于 create_app（app.add_middleware）。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # path 已由服务器解码（uvicorn/httpx ASGITransport 均如此）；再对
        # raw_path / query_string 做一次解码检查，兼容不解码 path 的服务器
        # 与编码在任何位置的 NUL（键、值、路径段）
        path = scope.get("path") or ""
        raw_path = (scope.get("raw_path") or b"").decode("utf-8", "replace")
        query = (scope.get("query_string") or b"").decode("utf-8", "replace")
        if "\x00" in path or _contains_nul(raw_path) or _contains_nul(query):
            response = JSONResponse(
                status_code=422,
                content=error_body(
                    "validation_error", "path/query 参数不允许包含 NUL 字符（\\x00）"
                ),
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


class AccessLogMiddleware:
    """模板级访问日志（INV-14 框架层，threads9 / docker-compose.yml:44）。

    uvicorn 的 access log 已在 compose 与 Dockerfile 用 --no-access-log
    关闭：默认请求行含完整 query——`GET /v1/search?q=<笔记正文>` 整行进
    stdout（compose 日志收集面）。进程内保留一个请求级排障入口：只记
    method、**路由模板**、status、耗时——模板取 scope["route"].path
    （如 /v1/notes/{note_id}；未匹配路由记 (unmatched)），构造上不可能
    带 query 或路径参数原值。挂最外层（SafeErrorFallback 之外）：兜底
    500 的响应也经过本层的 send 包装，status/耗时照常记录。
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        start = time.perf_counter()
        status: list[int | None] = [None]

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status[0] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            route = scope.get("route")
            template = getattr(route, "path", None) or "(unmatched)"
            access_logger.info(
                "%s %s -> %s %.1fms",
                scope.get("method", "?"),
                template,
                status[0] if status[0] is not None else 500,
                (time.perf_counter() - start) * 1000.0,
            )


class SafeErrorFallbackMiddleware:
    """未处理异常的最后防线（INV-14 框架层，threads9 P1 / main.py:56）。

    `add_exception_handler(Exception, …)` 只消毒**响应体**；Starlette 的
    ServerErrorMiddleware 调完 handler 后总是 re-raise，原始异常与
    traceback 进 uvicorn.error 日志（SQLAlchemy StatementError 的 str
    内嵌 SQL 绑定参数 = 用户内容，`safe_error_summary` 的边界被框架层
    绕过）。本中间件挂在 ServerErrorMiddleware 内侧的最外层用户位置，
    捕获一切 Exception：记一行 `safe_error_text`（类型名+错误码）、响应
    未开始则回固定 500 JSON，**永不 re-raise**——uvicorn 收不到异常，
    traceback 无从落日志。响应已开始（流式半途；本仓无流式端点）同样
    不 re-raise：re-raise 只会换回「uvicorn 打整段 traceback」这条被禁
    路径，记录后返回，由服务器以连接中断收尾。CancelledError 等
    BaseException 不拦（取消照常传播）。
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def send_wrapper(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:  # noqa: BLE001  # INV-14：见类 docstring——这里就是要把一切未处理异常拦下
            logger.error("unhandled exception: %s", safe_error_text(exc))
            if not started:
                response = JSONResponse(
                    status_code=500,
                    content=error_body("internal_error", "internal server error"),
                )
                await response(scope, receive, send)
