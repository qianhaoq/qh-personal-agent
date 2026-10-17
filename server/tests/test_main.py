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
