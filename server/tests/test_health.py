"""/healthz：数据库可达返回 200，不可达降级 503。"""


async def test_healthz_ok(client):
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["db"] is True


async def test_healthz_503_when_db_down(client, monkeypatch):
    import app.db as db_module

    def boom():
        raise RuntimeError("db unreachable")

    monkeypatch.setattr(db_module, "get_engine", boom)
    resp = await client.get("/healthz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["ok"] is False
    assert body["db"] is False
