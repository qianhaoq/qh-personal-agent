"""测试夹具：SQLite + MockLLM + InMemoryStore。

- SQLite 而不是 testcontainers：本机无 Docker。差异（SKIP LOCKED、pgvector）
  在 README 里说明。
- 设置环境变量 TEST_DATABASE_URL（postgresql+asyncpg://...）时改用 PG
  运行同一套测试：SQLite 是单写者模型，并发请求实际被串行化，真实的
  读-写竞态（条件更新、行锁）只有在 PG 下才能验证（见 test_concurrency.py）。
- 每个测试重置引擎：aiosqlite/asyncpg 连接绑定事件循环，pytest-asyncio
  每个测试一个新循环。
"""
import os
import pathlib

# 必须在导入 app 之前设置环境
_DB_FILE = pathlib.Path(__file__).parent / "_test.sqlite3"
_DB_FILE.unlink(missing_ok=True)
_TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")
if _TEST_DB_URL:
    os.environ["DATABASE_URL"] = _TEST_DB_URL
else:
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB_FILE}"
# 明显的占位符（非真实密钥），长度 ≥32 字节以满足 HS256 建议
os.environ["JWT_SECRET"] = "test-only-not-a-secret-0123456789abcdef"
os.environ["LLM_PROVIDER"] = "mock"
os.environ["MEMORY_BACKEND"] = "inmemory"

import pytest
from httpx import ASGITransport, AsyncClient

from app.db import Base, create_all, dispose_engine, get_engine
from app.memory.base import set_memory_store
from app.memory.inmemory import InMemoryStore
from app.models import InviteCode


@pytest.fixture(autouse=True)
async def _fresh_env():
    """每个测试：建表 + 干净的记忆存储 + 结束后释放引擎。"""
    if _TEST_DB_URL:
        # PG：每个测试先 drop 再建，保证隔离（SQLite 靠 import 时删库文件 + 随机 ID）
        engine = get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
    await create_all()
    set_memory_store(InMemoryStore())
    yield
    await dispose_engine()


@pytest.fixture
async def client():
    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def make_invite(session_maker, uses: int = 5) -> str:
    import secrets

    code = secrets.token_hex(8)
    async with session_maker() as session:
        session.add(InviteCode(code=code, max_uses=uses))
        await session.commit()
    return code


@pytest.fixture
async def invite_code():
    from app.db import get_sessionmaker

    return await make_invite(get_sessionmaker())


@pytest.fixture
async def user(client, invite_code):
    """注册一个用户并返回 (access_token, refresh_token, user_id)。"""
    import uuid

    device_id = f"dev_{uuid.uuid4().hex[:12]}"
    resp = await client.post(
        "/v1/auth/login",
        json={"invite_code": invite_code, "device_id": device_id, "device_name": "pytest"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    return {
        "access_token": data["access_token"],
        "refresh_token": data["refresh_token"],
        "user_id": data["user_id"],
        "device_id": device_id,
    }


def auth_headers(user: dict) -> dict:
    return {"authorization": f"Bearer {user['access_token']}"}


@pytest.fixture
def run_jobs():
    from app.pipeline.runner import run_pending_jobs

    async def _run():
        # 失败重试会推迟 run_after，这里跑到没有可执行的为止
        for _ in range(10):
            n = await run_pending_jobs()
            if n == 0:
                break

    return _run
