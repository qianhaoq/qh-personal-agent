"""应用构建（create_app）：生产默认关闭 /docs 与 /openapi.json，CORS 可选。

P2（审查）：FastAPI 默认把交互文档与完整 OpenAPI schema 暴露在公网，
等于把全部接口面（含鉴权细节）公开给扫描器；默认关、本地开发显式开。
"""
from httpx import ASGITransport, AsyncClient

from app.config import get_settings
from app.main import create_app


async def test_docs_and_openapi_disabled_by_default(client):
    assert (await client.get("/docs")).status_code == 404
    assert (await client.get("/redoc")).status_code == 404
    assert (await client.get("/openapi.json")).status_code == 404


async def test_docs_enabled_via_flag(monkeypatch):
    monkeypatch.setattr(get_settings(), "enable_docs", True)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        assert (await c.get("/docs")).status_code == 200
        assert (await c.get("/redoc")).status_code == 200
        assert (await c.get("/openapi.json")).status_code == 200


async def test_cors_not_enabled_by_default(client):
    """CORS_ALLOW_ORIGINS 为空时不挂中间件：跨域预检拿不到放行头。"""
    resp = await client.options(
        "/v1/notes",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"},
    )
    assert "access-control-allow-origin" not in resp.headers


async def test_cors_enabled_when_origins_configured(monkeypatch):
    monkeypatch.setattr(get_settings(), "cors_allow_origins", "https://notes.example.com")
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.options(
            "/v1/notes",
            headers={
                "Origin": "https://notes.example.com",
                "Access-Control-Request-Method": "GET",
            },
        )
    assert resp.headers.get("access-control-allow-origin") == "https://notes.example.com"


# ---------------------------------------------------------------------------
# threads13 P1 / INV-17（main.py:106）：/healthz 曾挂在模块级 singleton 上
# （`app = create_app()` 之后的装饰器）——工厂返回后装饰器才执行，
# uvicorn --factory / 测试用 create_app() 另建的 app 对 /healthz 是 404。
# ---------------------------------------------------------------------------


async def test_healthz_registered_on_factory_created_app():
    """工厂另建的 app 必须自带探针：GET /healthz → 200（DB 可达时）。"""
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/healthz")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["db"] is True


def test_factory_and_module_apps_expose_identical_routes():
    """类扫（threads13）：模块级 app 与工厂另建 app 的路由面必须完全一致
    ——未来任何「挂在模块级 singleton」上的路由/探针都会在这里掉出来。"""
    from app import main

    def route_face(a) -> list:
        # FastAPI >= 0.143 的 include_router 保留为 _IncludedRouter 包装
        # （顶层 routes 不再平铺），effective_candidates() 展开成带最终
        # path/methods 的路由；旧版平铺形态走 fallback
        def expand(routes):
            for r in routes:
                candidates = getattr(r, "effective_candidates", None)
                if callable(candidates):
                    yield from expand(candidates())
                else:
                    yield (r.path, tuple(sorted(getattr(r, "methods", None) or ())))

        return sorted(expand(a.routes))

    module_face = route_face(main.app)
    assert any(path == "/healthz" for path, _ in module_face), "模块级 app 必须有 /healthz"
    assert route_face(create_app()) == module_face
