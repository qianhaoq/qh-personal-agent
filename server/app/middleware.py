"""NUL 拒绝中间件（INV-7，docs/server-invariants.md）。

对每个请求的 URL path 与 query string **解码后**检查 \\x00，命中直接
返回 422，不进入路由——覆盖所有路由的 path/query 参数（`/v1/notes/n%00x`
这类编码 NUL 若直达 PostgreSQL 会触发编码错误变 500，修 C8）。

第二道防线是 schemas 层的 NoNulStr/reject_nul（请求体字段与显式声明的
path/query 参数注解，见 app/schemas.py）；本中间件是第一道、也是覆盖
面兜底的那一道。header 当前无任何值入库（authorization 只做 JWT 解析），
故不检查；未来任何从 header 取值入库的字段必须走 NoNulStr。
"""
from urllib.parse import unquote

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.errors import error_body


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
