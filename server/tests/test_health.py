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


async def test_healthz_503_when_db_probe_stalls(client, monkeypatch):
    """threads14 P2（main.py:118）：探测**卡住**（连接/查询挂起而非立即
    抛错）也要有应用层超时兜底——上界须短于 compose 健康检查的 5s，到点
    按 db_ok=False 降级 503，不等驱动的长超时（挂起请求跨探针轮次累积）。"""
    import asyncio

    import app.db as db_module
    import app.main as main_module

    assert 0 < main_module._HEALTHZ_DB_TIMEOUT_SECONDS < 5, (
        "healthz 的 DB 探测超时必须短于 compose 健康检查超时（5s）"
    )

    class StalledConn:
        async def execute(self, *args, **kwargs):
            await asyncio.sleep(30)  # 远超注入的超时，也远超单测容忍度

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class StalledEngine:
        def connect(self):
            return StalledConn()

    monkeypatch.setattr(db_module, "get_engine", lambda: StalledEngine())
    monkeypatch.setattr(main_module, "_HEALTHZ_DB_TIMEOUT_SECONDS", 0.1)

    resp = await client.get("/healthz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["ok"] is False
    assert body["db"] is False


async def test_healthz_db_probe_timeout_returns_fast_ok_path(client):
    """threads14 P2 正面：探测快于超时上限时 wait_for 不影响 200（上界
    常量 2.5s 对真实 SELECT 1 足够宽，防止误伤正常健康实例）。"""
    import time

    import app.main as main_module

    start = time.monotonic()
    resp = await client.get("/healthz")
    elapsed = time.monotonic() - start
    assert resp.status_code == 200, resp.text
    assert resp.json()["db"] is True
    assert elapsed < main_module._HEALTHZ_DB_TIMEOUT_SECONDS
