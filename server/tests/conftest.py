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
import secrets

# 必须在导入 app 之前设置环境
_DB_FILE = pathlib.Path(__file__).parent / "_test.sqlite3"
_DB_FILE.unlink(missing_ok=True)
_TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")
if _TEST_DB_URL:
    os.environ["DATABASE_URL"] = _TEST_DB_URL
    # PG（生产形态）下 config 会拒绝 MEMORY_BACKEND=inmemory：测试套件
    # 显式放行（生产环境不允许，见 config._reject_inmemory_memory_backend_
    # in_production；仅限单进程 pytest，独立 worker 进程不放行），记忆后端
    # 仍统一用进程内假实现
    os.environ["ALLOW_INMEMORY_IN_TESTS"] = "true"
    # 同理：LLM_PROVIDER=mock 在生产形态下默认拒绝（见
    # config._reject_misconfigured_llm_in_production），测试套件显式放行
    os.environ["ALLOW_MOCK_LLM_IN_PROD"] = "true"
else:
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB_FILE}"
# 随机生成的合法密钥形态（64 位 hex = 32 字节）：PG 测试运行（生产形态
# 校验）下也能通过；同时不含 secret/password 等被拒字样
os.environ["JWT_SECRET"] = secrets.token_hex(32)
os.environ["LLM_PROVIDER"] = "mock"
os.environ["MEMORY_BACKEND"] = "inmemory"

import pytest
from httpx import ASGITransport, AsyncClient

from app.db import Base, create_all, dispose_engine, get_engine
from app.memory.base import set_memory_store
from app.memory.inmemory import InMemoryStore
from app.models import InviteCode

# 是否在 PostgreSQL 下运行同一套测试
IS_POSTGRES = bool(_TEST_DB_URL and _TEST_DB_URL.startswith("postgresql"))

# 仅 PG 运行的测试统一挂这个标记：这些竞态依赖 PG 的语句级多版本快照与
# 行锁语义；SQLite 单写者模型下并发事务天然串行，构造不出对应窗口
# （读事务持有共享锁，读之间注入写会阻塞），所以在 SQLite 下跳过。
requires_postgres = pytest.mark.skipif(
    not IS_POSTGRES,
    reason="竞态依赖 PostgreSQL 的多版本快照/行锁语义；SQLite 单写者模型下事务天然串行，无法复现",
)


@pytest.fixture(autouse=True)
async def _fresh_env():
    """每个测试：先 drop 再建表 + 干净的记忆存储 + 结束后释放引擎。

    SQLite 同样 drop：import 时删库文件只清一次，同一进程内残留的
    job/note 行会跨测试泄漏（并发测试尤其依赖空队列起步）。
    """
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
