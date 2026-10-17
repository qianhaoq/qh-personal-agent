"""INV-7：NUL 拒绝中间件——path/query 解码后含 \\x00 一律 422。

body/参数注解层的拒绝由既有 test_nul_rejected_in_note_strings_422 /
test_nul_rejected_in_auth_strings_422 / test_nul_rejected_in_ask_and_search_422
覆盖；这里验证第一道（中间件）与它的全路由覆盖面。
"""
from app.main import app
from tests.conftest import auth_headers


async def test_nul_in_path_param_422(client, user):
    """/v1/notes/n%00x 的 GET/PATCH/DELETE：解码后含 NUL → 422，不进路由。"""
    headers = auth_headers(user)
    got = await client.get("/v1/notes/nte_%00x", headers=headers)
    assert got.status_code == 422, got.text
    assert got.json()["error"]["code"] == "validation_error"

    patched = await client.patch(
        "/v1/notes/nte_%00x", json={"text": "x", "rev": 1}, headers=headers
    )
    assert patched.status_code == 422, patched.text

    deleted = await client.delete("/v1/notes/nte_%00x", headers=headers)
    assert deleted.status_code == 422, deleted.text


async def test_nul_in_query_param_422(client, user):
    """query 里任何位置（值/键）的编码 NUL → 422。"""
    headers = auth_headers(user)
    cases = [
        "/v1/search?q=a%00b",
        "/v1/search?q=%00",
        "/v1/notes?cursor=1%00",
        "/v1/notes?limit=%00",
        "/v1/sync?since=%00",
        "/v1/search?%00q=x",
    ]
    for url in cases:
        resp = await client.get(url, headers=headers)
        assert resp.status_code == 422, (url, resp.text)
        assert resp.json()["error"]["code"] == "validation_error"


async def test_nul_middleware_covers_all_routes(client, user):
    """遍历全部路由：含参路径注入 %00、无参路径注入 ?%00= 查询串，
    一律得到 422 而不是 500（未鉴权也先被中间件挡下，不进路由）。"""
    from fastapi.routing import APIRoute

    headers = auth_headers(user)
    paths = set()
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        path = route.path
        if "{" in path:  # 把每个路径参数换成 %00
            import re

            path = re.sub(r"\{[^}]+\}", "%00", path)
            paths.add(path)
        else:
            paths.add(f"{path}?%00=1")
    assert paths, "app 应有已注册路由"

    for path in paths:
        resp = await client.get(path, headers=headers)
        assert resp.status_code == 422, (path, resp.status_code, resp.text)
        assert resp.json()["error"]["code"] == "validation_error"
